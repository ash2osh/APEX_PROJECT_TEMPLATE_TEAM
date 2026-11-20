#Requires -Version 5.1
# Offline readiness check for a template checkout. Run it with Windows PowerShell 5.1
# (powershell.exe) to cover the interpreter the team uses; PowerShell 7 (pwsh) needs
# -AllowPowerShellCore.
#
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\windows_check.ps1
#   powershell.exe -NoProfile -File scripts\windows_check.ps1 -Skip tests,pre-push -ReportPath report.txt
#
# Steps, in order: environment, ascii, parse, env-loader, python-tools, team-offline, tests, pre-push.
# Each step ends with one line "RESULT <step> PASS|FAIL|SKIP <detail>", the contract the test reads.
# Exit 0 when no step failed (SKIP is fine), 1 when a step failed, 2 for a usage error.
# -Only and -Skip take step names, separated by commas or given as separate values; they cannot be
# combined. -RepoRoot defaults to the parent of the scripts folder this file is in.
#
# Written to parse and run on Windows PowerShell 5.1: no null-coalescing or null-conditional
# operators, no ternary, no pipeline chain operators, no ForEach-Object -Parallel, no $IsWindows,
# no ProcessStartInfo.ArgumentList, no -AsByteStream. Pure ASCII, LF line endings.

param(
  [string] $RepoRoot,
  [string[]] $Only,
  [string[]] $Skip,
  [switch] $AllowPowerShellCore,
  [string] $ReportPath
)

$ErrorActionPreference = "Stop"

$script:StepNames = @("environment", "ascii", "parse", "env-loader", "python-tools", "team-offline", "tests", "pre-push")
$script:ReportLines = New-Object System.Collections.ArrayList
$script:Results = New-Object System.Collections.ArrayList
$script:StepStatus = $null
$script:StepDetail = ""
$script:HasResolver = $false

# A minimal valid .env for the offline checks: the loader requires the project header, and the
# DEV shorthand trio (DEV_SCHEMA, DEV_SQLCL_CONNECTION, DEV_EXPECTED_USER) is what this script checks.
function New-DevEnvText([string] $AppId, [string] $Schema) {
  return "PROJECT_NAME=DEMO`nDEVELOPER_NAME=ALICE`nDB_ENVIRONMENT=development`nAPEX_APP_ID=" + $AppId + "`nDEV_SCHEMA=" + $Schema + "`nDEV_SQLCL_CONNECTION=conn_one`nDEV_EXPECTED_USER=" + $Schema + "`n"
}

# Numeric application directories under apps/<schema>/<id>, lowest first (empty on a bare template).
function Get-ApplicationIds {
  $appsRoot = Join-Path $RepoRoot "apps"
  if (-not (Test-Path -LiteralPath $appsRoot -PathType Container)) { return @() }
  $ids = New-Object System.Collections.ArrayList
  foreach ($schema in @(Get-ChildItem -LiteralPath $appsRoot -Directory)) {
    foreach ($app in @(Get-ChildItem -LiteralPath $schema.FullName -Directory)) {
      if ($app.Name -match '^[0-9]+$') { [void] $ids.Add($app.Name) }
    }
  }
  return @($ids | Sort-Object { [decimal] $_ })
}

# The parsing schema of application <AppId>: its dev.json descriptor's parsingSchema, else the schema folder it
# sits in. A descriptor names the schema the team's app really parses as, so the temporary .env must use it:
# a fixed name would make verify-local refuse every real descriptor. A bare template uses SCHEMA1.
function Get-ApplicationSchema([string] $AppId) {
  $appsRoot = Join-Path $RepoRoot "apps"
  if (-not (Test-Path -LiteralPath $appsRoot -PathType Container)) { return "SCHEMA1" }
  foreach ($schema in @(Get-ChildItem -LiteralPath $appsRoot -Directory)) {
    $appDir = Join-Path $schema.FullName $AppId
    if (-not (Test-Path -LiteralPath $appDir -PathType Container)) { continue }
    $descriptor = Join-Path $appDir "deployments\dev.json"
    if (Test-Path -LiteralPath $descriptor -PathType Leaf) {
      try {
        # Read as UTF-8 explicitly: Windows PowerShell 5.1 would read a file without a byte-order mark as ANSI.
        $descriptorText = [System.IO.File]::ReadAllText($descriptor, (New-Object System.Text.UTF8Encoding($false)))
        $parsed = $descriptorText | ConvertFrom-Json
        $parsing = [string] $parsed.app.databaseSession.parsingSchema
        if (-not [string]::IsNullOrWhiteSpace($parsing)) { return $parsing }
      } catch {
        # An unreadable descriptor falls back to the folder; verify-local reports the descriptor itself.
      }
    }
    return $schema.Name
  }
  return "SCHEMA1"
}

function Test-OnWindows {
  return ([System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT)
}

function Add-Line([string] $Text) {
  Write-Host $Text
}

function Add-Banner([string] $Text) {
  Write-Host $Text
  [void] $script:ReportLines.Add($Text)
}

function Add-Tail([string[]] $Lines, [int] $Count) {
  $items = @($Lines)
  $start = [Math]::Max(0, $items.Count - $Count)
  for ($index = $start; $index -lt $items.Count; $index++) {
    Add-Line ("  | " + $items[$index])
  }
}

function Set-Outcome([string] $Status, [string] $Detail) {
  $script:StepStatus = $Status
  $script:StepDetail = $Detail
}

function ConvertTo-StepNames([string[]] $Values) {
  $names = New-Object System.Collections.ArrayList
  foreach ($value in @($Values)) {
    if ($null -eq $value) { continue }
    foreach ($part in ($value -split ",")) {
      $trimmed = $part.Trim().ToLower()
      if ($trimmed.Length -gt 0) { [void] $names.Add($trimmed) }
    }
  }
  return $names.ToArray()
}

# Runs a native program with stderr merged into its output and returns the exit code and the
# text lines. Windows PowerShell 5.1 turns native stderr into a terminating error under "Stop",
# so the preference is relaxed for this call only; the exit code comes from $LASTEXITCODE.
function Invoke-Native([string] $Program, [string[]] $Arguments) {
  $previous = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  $code = -1
  $lines = @()
  try {
    $global:LASTEXITCODE = 0
    $lines = @(& $Program @Arguments 2>&1 | ForEach-Object { if ($_ -is [System.Management.Automation.ErrorRecord]) { [string] $_.Exception.Message } else { "$_" } })
    $code = $global:LASTEXITCODE
  } finally {
    $ErrorActionPreference = $previous
  }
  return [pscustomobject] @{ Code = $code; Lines = $lines }
}

# The running engine, so that a child process runs under the same interpreter as this script.
function Get-EnginePath {
  $path = $null
  try { $path = (Get-Process -Id $PID).Path } catch { $path = $null }
  if ([string]::IsNullOrEmpty($path)) {
    if ($PSVersionTable.PSEdition -eq "Core") {
      $path = Join-Path $PSHOME "pwsh"
    } else {
      $path = Join-Path $PSHOME "powershell.exe"
    }
  }
  return $path
}

function Get-RepoPython {
  if (-not $script:HasResolver) { return $null }
  return (Resolve-TeamPython)
}

function Get-ScratchDirectory {
  $name = "windows-check-" + [Guid]::NewGuid().ToString("N")
  $directory = Join-Path ([System.IO.Path]::GetTempPath()) $name
  [System.IO.Directory]::CreateDirectory($directory) | Out-Null
  return $directory
}

# Removes only a directory this script created (its name carries the marker).
function Remove-ScratchDirectory([string] $Directory) {
  if ([string]::IsNullOrEmpty($Directory)) { return }
  if (-not $Directory.Contains("windows-check-")) { return }
  if ([System.IO.Directory]::Exists($Directory)) {
    try {
      [System.IO.Directory]::Delete($Directory, $true)
    } catch {
      Add-Line ("could not remove scratch directory: " + $_.Exception.Message)
    }
  }
}

function ConvertTo-AsciiBytes([string] $Text) {
  return ,([System.Text.Encoding]::ASCII.GetBytes($Text))
}

function Join-Bytes([object[]] $Parts) {
  $list = New-Object 'System.Collections.Generic.List[byte]'
  foreach ($part in $Parts) {
    $list.AddRange([byte[]] $part)
  }
  return ,($list.ToArray())
}

function Test-Utf8Bom([byte[]] $Bytes) {
  return (($Bytes.Length -ge 3) -and ($Bytes[0] -eq 0xEF) -and ($Bytes[1] -eq 0xBB) -and ($Bytes[2] -eq 0xBF))
}

function Get-RepoScriptsDirectory {
  return (Join-Path $RepoRoot "scripts")
}

function Invoke-CheckEnvironment {
  $version = $PSVersionTable.PSVersion
  $edition = [string] $PSVersionTable.PSEdition
  if ([string]::IsNullOrEmpty($edition)) { $edition = "Desktop" }
  Add-Banner ("Engine: PowerShell " + $version.ToString() + " (" + $edition + ")")
  Add-Banner ("Engine path: " + (Get-EnginePath))
  Add-Banner ("OS version: " + [System.Environment]::OSVersion.VersionString)
  Add-Banner ("64-bit process: " + [System.Environment]::Is64BitProcess)

  $python = Get-RepoPython
  if ($null -eq $python) {
    Add-Banner "Python: not found (python3, python or py -3 with Python 3.10 or newer)"
  } else {
    Add-Banner ("Python: " + ($python.Path + " " + ($python.Prefix -join " ")).Trim())
  }

  $gitCommands = @(Get-Command git -CommandType Application -ErrorAction SilentlyContinue)
  if ($gitCommands.Count -eq 0) {
    Add-Banner "git: not found"
  } else {
    $gitRun = Invoke-Native $gitCommands[0].Source @("--version")
    $gitText = "(no output)"
    if ($gitRun.Lines.Count -gt 0) { $gitText = [string] $gitRun.Lines[0] }
    Add-Banner ("git: " + $gitText)
  }

  $policy = "unknown"
  try { $policy = [string] (Get-ExecutionPolicy) } catch { $policy = "unknown" }
  Add-Banner ("Execution policy: " + $policy)

  if (Test-OnWindows) {
    $longPaths = "not set"
    try {
      $longPaths = [string] (Get-ItemProperty -LiteralPath "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name LongPathsEnabled -ErrorAction Stop).LongPathsEnabled
    } catch {
      $longPaths = "not set"
    }
    Add-Banner ("Windows long paths (LongPathsEnabled): " + $longPaths)
  } else {
    Add-Banner "Windows long paths: not checked (this is not Windows)"
  }

  Add-Banner ("Repository: " + $RepoRoot)
  Add-Banner ("Repository path length: " + $RepoRoot.Length)

  if (($version.Major -ge 6) -and (-not $AllowPowerShellCore)) {
    Set-Outcome "FAIL" ("run this script with powershell.exe to cover Windows PowerShell 5.1; this engine is PowerShell " + $version.ToString() + " (pass -AllowPowerShellCore to run it here anyway)")
    return
  }
  Set-Outcome "PASS" ("PowerShell " + $version.ToString() + " " + $edition)
}

function Invoke-CheckAscii {
  $directory = Get-RepoScriptsDirectory
  if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
    Set-Outcome "FAIL" ("scripts directory not found: " + $directory)
    return
  }
  $files = @(Get-ChildItem -LiteralPath $directory -Filter "*.ps1" -File | Sort-Object Name)
  $problems = New-Object System.Collections.ArrayList
  foreach ($file in $files) {
    $bytes = [System.IO.File]::ReadAllBytes($file.FullName)
    if (Test-Utf8Bom $bytes) {
      Add-Line ("  BOM      " + $file.Name)
      continue
    }
    $offset = -1
    for ($index = 0; $index -lt $bytes.Length; $index++) {
      if ($bytes[$index] -gt 127) {
        $offset = $index
        break
      }
    }
    if ($offset -lt 0) {
      Add-Line ("  ascii    " + $file.Name)
      continue
    }
    $message = $file.Name + " (first non-ASCII byte at offset " + $offset + ", no UTF-8 BOM)"
    Add-Line ("  NON-ASCII " + $message)
    [void] $problems.Add($message)
  }
  if ($problems.Count -gt 0) {
    Set-Outcome "FAIL" ("non-ASCII script without a UTF-8 BOM: " + ($problems -join "; "))
    return
  }
  Set-Outcome "PASS" ("" + $files.Count + " script(s) are ASCII or start with a UTF-8 BOM")
}

function Invoke-CheckParse {
  $directory = Get-RepoScriptsDirectory
  if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
    Set-Outcome "FAIL" ("scripts directory not found: " + $directory)
    return
  }
  $files = @(Get-ChildItem -LiteralPath $directory -Filter "*.ps1" -File | Sort-Object Name)
  $problems = New-Object System.Collections.ArrayList
  foreach ($file in $files) {
    $tokens = $null
    $parseErrors = $null
    $null = [System.Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref] $tokens, [ref] $parseErrors)
    foreach ($parseError in @($parseErrors)) {
      if ($null -eq $parseError) { continue }
      $message = $file.Name + ":" + $parseError.Extent.StartLineNumber + ": " + $parseError.Message
      Add-Line ("  " + $message)
      [void] $problems.Add($message)
    }
  }
  if ($problems.Count -gt 0) {
    Set-Outcome "FAIL" ("" + $problems.Count + " parse error(s); first: " + $problems[0])
    return
  }
  Set-Outcome "PASS" ("" + $files.Count + " script(s) parsed by the running engine")
}

function New-ProbeText([string] $LoaderPath, [string] $EnvPath) {
  $loaderLiteral = $LoaderPath.Replace("'", "''")
  $envLiteral = $EnvPath.Replace("'", "''")
  # A refusal is printed as one line: PowerShell's error view wraps a long message at the console width.
  $template = @'
try {
  . '__LOADER__' -EnvFile '__ENV__'
} catch {
  Write-Output ('PROBE-ERROR ' + ($_.Exception.Message -replace '\s+', ' '))
  exit 1
}
foreach ($probeName in @('DEV_SCHEMA', 'DEV_SQLCL_CONNECTION', 'DEV_EXPECTED_USER', 'TABLES_SCHEMA', 'TABLES_SQLCL_CONNECTION', 'TABLES_EXPECTED_USER', 'CODE_SCHEMA', 'CODE_SQLCL_CONNECTION', 'CODE_EXPECTED_USER', 'APEX_PARSING_SCHEMA', 'APEX_SQLCL_CONNECTION', 'APEX_EXPECTED_USER')) {
  $probeName + '=' + [Environment]::GetEnvironmentVariable($probeName)
}
'@
  return $template.Replace("__LOADER__", $loaderLiteral).Replace("__ENV__", $envLiteral) + "`r`n"
}

function Invoke-CheckEnvLoader {
  $loader = Join-Path (Get-RepoScriptsDirectory) "load_env.ps1"
  if (-not (Test-Path -LiteralPath $loader -PathType Leaf)) {
    Set-Outcome "FAIL" ("load_env.ps1 not found: " + $loader)
    return
  }
  $engine = Get-EnginePath
  $devLf = New-DevEnvText "100" "SCHEMA1"
  $triple = @("SCHEMA1", "conn_one", "SCHEMA1")
  $probeNames = @("DEV_SCHEMA", "DEV_SQLCL_CONNECTION", "DEV_EXPECTED_USER", "TABLES_SCHEMA", "TABLES_SQLCL_CONNECTION", "TABLES_EXPECTED_USER", "CODE_SCHEMA", "CODE_SQLCL_CONNECTION", "CODE_EXPECTED_USER", "APEX_PARSING_SCHEMA", "APEX_SQLCL_CONNECTION", "APEX_EXPECTED_USER")
  $work = Get-ScratchDirectory
  $failures = New-Object System.Collections.ArrayList
  try {
    $variants = @()
    $variants += [pscustomobject] @{ Name = "LF"; Bytes = (ConvertTo-AsciiBytes $devLf); Refusal = $false }
    $variants += [pscustomobject] @{ Name = "CRLF"; Bytes = (ConvertTo-AsciiBytes ($devLf -replace "`n", "`r`n")); Refusal = $false }
    $variants += [pscustomobject] @{ Name = "BOM"; Bytes = (Join-Bytes @([byte[]] @(0xEF, 0xBB, 0xBF), (ConvertTo-AsciiBytes $devLf))); Refusal = $false }
    # The comment holds the two bytes of U+00E9 in UTF-8 (0xC3 0xA9), written here as bytes so this file stays ASCII.
    $variants += [pscustomobject] @{ Name = "non-ASCII comment"; Bytes = (Join-Bytes @((ConvertTo-AsciiBytes "# caf"), [byte[]] @(0xC3, 0xA9), (ConvertTo-AsciiBytes "`n"), (ConvertTo-AsciiBytes $devLf))); Refusal = $false }
    $utf16Text = [System.Text.Encoding]::Unicode.GetBytes($devLf)
    $variants += [pscustomobject] @{ Name = "UTF-16"; Bytes = (Join-Bytes @([byte[]] @(0xFF, 0xFE), $utf16Text)); Refusal = $true }

    foreach ($variant in $variants) {
      $envPath = Join-Path $work ("variant-" + ($variant.Name -replace "[^A-Za-z0-9]", "") + ".env")
      [System.IO.File]::WriteAllBytes($envPath, [byte[]] $variant.Bytes)
      $probePath = Join-Path $work ("probe-" + ($variant.Name -replace "[^A-Za-z0-9]", "") + ".ps1")
      [System.IO.File]::WriteAllText($probePath, (New-ProbeText $loader $envPath), (New-Object System.Text.UTF8Encoding($true)))
      $run = Invoke-Native $engine @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $probePath)
      $text = $run.Lines -join "`n"

      if ($variant.Refusal) {
        if (($run.Code -ne 0) -and $text.Contains("is UTF-16; save it as UTF-8")) {
          Add-Line ("  " + $variant.Name + ": refused as expected")
        } else {
          Add-Line ("  " + $variant.Name + ": not refused (exit " + $run.Code + ")")
          Add-Tail $run.Lines 5
          [void] $failures.Add($variant.Name)
        }
        continue
      }

      $values = @{}
      foreach ($line in $run.Lines) {
        if ($line -match '^([A-Z_]+)=(.*)$') { $values[$Matches[1]] = $Matches[2] }
      }
      $mismatch = New-Object System.Collections.ArrayList
      for ($index = 0; $index -lt $probeNames.Count; $index++) {
        $name = $probeNames[$index]
        $expected = $triple[$index % 3]
        $actual = $values[$name]
        if (($null -eq $actual) -or ($actual -cne $expected)) {
          [void] $mismatch.Add($name + "='" + $actual + "' (expected '" + $expected + "')")
        }
      }
      if (($run.Code -eq 0) -and ($mismatch.Count -eq 0)) {
        Add-Line ("  " + $variant.Name + ": loaded the DEV shorthand")
      } else {
        Add-Line ("  " + $variant.Name + ": exit " + $run.Code + ", " + $mismatch.Count + " value(s) wrong")
        foreach ($item in $mismatch) { Add-Line ("    " + $item) }
        Add-Tail $run.Lines 5
        [void] $failures.Add($variant.Name)
      }
    }
  } finally {
    Remove-ScratchDirectory $work
  }

  if ($failures.Count -gt 0) {
    Set-Outcome "FAIL" ("loader check failed for: " + ($failures -join ", "))
    return
  }
  Set-Outcome "PASS" "LF, CRLF, BOM and non-ASCII comment load the DEV shorthand; UTF-16 is refused"
}

function Invoke-CheckPythonTools {
  $python = Get-RepoPython
  if ($null -eq $python) {
    Set-Outcome "SKIP" "no Python 3.10 or newer found (python3, python or py -3)"
    return
  }
  $prefix = @($python.Prefix)
  $version = Invoke-Native $python.Path ($prefix + @("-I", "-c", "import sys; print(sys.version_info[:2])"))
  if ($version.Code -ne 0) {
    Set-Outcome "FAIL" ("python -I could not run: " + ($version.Lines -join " "))
    return
  }
  Add-Line ("  interpreter reports " + ($version.Lines -join " ").Trim())

  $code = @'
import os
import py_compile
import sys

scripts = sys.argv[1]
cache = sys.argv[2]
count = 0
errors = []
for folder, dirs, files in os.walk(scripts):
    dirs.sort()
    for name in sorted(files):
        if not name.endswith('.py'):
            continue
        count += 1
        path = os.path.join(folder, name)
        try:
            py_compile.compile(path, cfile=os.path.join(cache, str(count) + '.pyc'), doraise=True)
        except py_compile.PyCompileError as error:
            errors.append(str(error).strip())
for text in errors:
    print('SYNTAX ' + text)
print('compiled ' + str(count) + ' file(s); ' + str(len(errors)) + ' with errors')
sys.exit(1 if errors else 0)
'@

  $work = Get-ScratchDirectory
  try {
    $scriptPath = Join-Path $work "compile_all.py"
    $cachePath = Join-Path $work "cache"
    [System.IO.Directory]::CreateDirectory($cachePath) | Out-Null
    [System.IO.File]::WriteAllText($scriptPath, $code, (New-Object System.Text.UTF8Encoding($false)))
    $run = Invoke-Native $python.Path ($prefix + @("-I", $scriptPath, (Get-RepoScriptsDirectory), $cachePath))
    foreach ($line in $run.Lines) { Add-Line ("  " + $line) }
    if ($run.Code -eq 0) {
      Set-Outcome "PASS" ($run.Lines | Select-Object -Last 1)
    } else {
      Set-Outcome "FAIL" ("python compile check exit " + $run.Code + ": " + (($run.Lines | Select-Object -Last 1) -join " "))
    }
  } finally {
    Remove-ScratchDirectory $work
  }
}

function Invoke-CheckTeamOffline {
  $team = Join-Path (Get-RepoScriptsDirectory) "team.ps1"
  if (-not (Test-Path -LiteralPath $team -PathType Leaf)) {
    Set-Outcome "SKIP" "scripts/team.ps1 is not present"
    return
  }
  $engine = Get-EnginePath
  # A project with applications under apps/ is checked against the lowest one; a bare template uses 100.
  $appIds = @(Get-ApplicationIds)
  $bare = ($appIds.Count -eq 0)
  $appId = "100"
  $schema = "SCHEMA1"
  if (-not $bare) {
    $appId = [string] $appIds[0]
    $schema = Get-ApplicationSchema $appId
  }
  $work = Get-ScratchDirectory
  $previousEnvFile = [Environment]::GetEnvironmentVariable("PROJECT_ENV_FILE")
  try {
    $envPath = Join-Path $work "dev-only.env"
    [System.IO.File]::WriteAllText($envPath, (New-DevEnvText $appId $schema), (New-Object System.Text.UTF8Encoding($false)))
    if ($bare) {
      Add-Line "  temporary .env: APEX_APP_ID=100 DEV_SCHEMA=SCHEMA1 (no application under apps/)"
    } else {
      Add-Line ("  temporary .env: APEX_APP_ID=" + $appId + " DEV_SCHEMA=" + $schema + " (lowest application id under apps/)")
    }
    [Environment]::SetEnvironmentVariable("PROJECT_ENV_FILE", $envPath)

    $targets = Invoke-Native $engine @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $team, "targets")
    $targetsOk = (($targets.Code -eq 0) -and (($targets.Lines -join "`n").Contains($schema)))
    Add-Line ("  targets: exit " + $targets.Code + ", lists " + $schema + ": " + $targetsOk)
    if (-not $targetsOk) { Add-Tail $targets.Lines 20 }

    $verify = Invoke-Native $engine @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $team, "verify-local")
    Add-Line ("  verify-local: exit " + $verify.Code)
    $verifyOk = ($verify.Code -eq 0)
    # On a bare template the only acceptable failures are the missing configured application directory itself.
    $bareExcused = $false
    if ((-not $verifyOk) -and $bare) {
      $failNames = New-Object System.Collections.ArrayList
      foreach ($line in $verify.Lines) {
        if ($line -match '^\[FAIL\] ([A-Za-z0-9_]+):') { [void] $failNames.Add($Matches[1]) }
      }
      $unexpected = @($failNames | Where-Object { $_ -notmatch '^configured_app_[0-9]+$' })
      if (($failNames.Count -gt 0) -and ($unexpected.Count -eq 0)) { $bareExcused = $true }
    }
    if ((-not $verifyOk) -and (-not $bareExcused)) { Add-Tail $verify.Lines 20 }

    if (-not $targetsOk) {
      Set-Outcome "FAIL" ("targets does not list " + $schema + " (exit " + $targets.Code + "); verify-local exit " + $verify.Code)
    } elseif ($verifyOk) {
      Set-Outcome "PASS" ("targets lists " + $schema + " and verify-local exits 0")
    } elseif ($bareExcused) {
      Set-Outcome "PASS" ("bare template: no application under apps/, so configured_app_" + $appId + " is expected to fail")
    } else {
      Set-Outcome "FAIL" ("verify-local exit " + $verify.Code + " (last 20 lines above)")
    }
  } finally {
    [Environment]::SetEnvironmentVariable("PROJECT_ENV_FILE", $previousEnvFile)
    Remove-ScratchDirectory $work
  }
}

function Invoke-CheckTests {
  $testsDirectory = Join-Path $RepoRoot "tests"
  if (-not (Test-Path -LiteralPath $testsDirectory -PathType Container)) {
    Set-Outcome "SKIP" "tests directory is not present"
    return
  }
  $python = Get-RepoPython
  if ($null -eq $python) {
    Set-Outcome "SKIP" "no Python 3.10 or newer found (python3, python or py -3)"
    return
  }

  $gatedFiles = 0
  $gatedCalls = 0
  foreach ($file in @(Get-ChildItem -LiteralPath $testsDirectory -Filter "*.py" -File)) {
    $text = [System.IO.File]::ReadAllText($file.FullName)
    $calls = ([regex]::Matches($text, '(?<!def )working_pwsh\(')).Count
    if ($calls -gt 0) {
      $gatedFiles++
      $gatedCalls += $calls
    }
  }
  $pwshCommands = @(Get-Command pwsh -CommandType Application -ErrorAction SilentlyContinue)
  $pwshNote = "no"
  if ($pwshCommands.Count -gt 0) { $pwshNote = "yes (" + $pwshCommands[0].Source + ")" }
  Add-Line ("NOTE: " + $gatedFiles + " test file(s) with " + $gatedCalls + " working_pwsh( call(s) are gated on PowerShell Core; pwsh on PATH: " + $pwshNote + "; this run's engine is PowerShell " + $PSVersionTable.PSVersion.ToString() + " (" + [string] $PSVersionTable.PSEdition + ")")

  $prefix = @($python.Prefix)
  Add-Line "  running the unit tests; this can take 10 minutes or more and prints nothing until it ends"
  $timer = [System.Diagnostics.Stopwatch]::StartNew()
  Push-Location -LiteralPath $RepoRoot
  try {
    $run = Invoke-Native $python.Path ($prefix + @("-m", "unittest", "discover", "-s", "tests"))
  } finally {
    Pop-Location
  }
  $timer.Stop()
  $seconds = [Math]::Round($timer.Elapsed.TotalSeconds, 1)

  # Stderr is merged into $run.Lines, and a test's own stdout can arrive after unittest's summary, so
  # only lines in unittest's exact summary form count. The last strict match of each wins.
  # The status line is OK or FAILED, then unittest's counters in parentheses when there are any. Only those counters
  # count, so a test's own "OK (fixture initialized)" is not taken as the summary.
  $counter = '(failures|errors|skipped|expected failures|unexpected successes)=\d+'
  $statusPattern = '^(OK|FAILED)( \(' + $counter + '(, ' + $counter + ')*\))?\s*$'
  $ranLine = "(no Ran line)"
  $statusLine = "(no status line)"
  $failingHeaders = New-Object System.Collections.ArrayList
  foreach ($line in $run.Lines) {
    if ($line -cmatch '^Ran \d+ tests? in [0-9.]+s\s*$') { $ranLine = $line }
    if ($line -cmatch $statusPattern) { $statusLine = $line }
    if ($line -cmatch '^(FAIL|ERROR): \S') {
      $header = $line.TrimEnd()
      if (-not $failingHeaders.Contains($header)) { [void] $failingHeaders.Add($header) }
    }
  }
  Add-Line ("  elapsed: " + $seconds + " s")
  Add-Line ("  " + $ranLine)
  Add-Line ("  " + $statusLine)

  if ($run.Code -ne 0) {
    $failCount = $failingHeaders.Count
    $shown = [Math]::Min($failCount, 50)
    if ($failCount -gt 0) {
      Add-Line ("  failing tests (" + $failCount + "):")
      for ($index = 0; $index -lt $shown; $index++) {
        Add-Line ("    " + $failingHeaders[$index])
      }
      if ($failCount -gt $shown) { Add-Line ("  ... and " + ($failCount - $shown) + " more") }
    }
    Add-Line "  last 60 lines of output:"
    Add-Tail $run.Lines 60
    $failDetail = "unittest exit " + $run.Code + " after " + $seconds + " s; " + $ranLine + "; " + $statusLine
    if ($failCount -gt 0) {
      $failDetail = $failDetail + "; " + $failCount + " failing test(s), first: " + $failingHeaders[0]
    }
    Set-Outcome "FAIL" $failDetail
    return
  }
  Set-Outcome "PASS" ("unittest exit 0 after " + $seconds + " s; " + $ranLine + "; " + $statusLine)
}

function Invoke-CheckPrePush {
  $script = Join-Path (Get-RepoScriptsDirectory) "pre_push_check.py"
  if (-not (Test-Path -LiteralPath $script -PathType Leaf)) {
    Set-Outcome "SKIP" "scripts/pre_push_check.py is not present"
    return
  }
  $python = Get-RepoPython
  if ($null -eq $python) {
    Set-Outcome "SKIP" "no Python 3.10 or newer found (python3, python or py -3)"
    return
  }
  $prefix = @($python.Prefix)
  Push-Location -LiteralPath $RepoRoot
  try {
    $run = Invoke-Native $python.Path ($prefix + @("scripts/pre_push_check.py", "--all"))
  } finally {
    Pop-Location
  }
  if ($run.Code -eq 0) {
    Set-Outcome "PASS" "pre_push_check.py --all exits 0"
    return
  }
  Add-Line "  last 20 lines of output:"
  Add-Tail $run.Lines 20
  Set-Outcome "FAIL" ("pre_push_check.py --all exit " + $run.Code + " (last 20 lines above)")
}

function Invoke-Step([string] $Name) {
  $script:StepStatus = $null
  $script:StepDetail = ""
  Write-Host ""
  Write-Host ("== " + $Name + " ==")
  try {
    switch ($Name) {
      "environment"  { Invoke-CheckEnvironment }
      "ascii"        { Invoke-CheckAscii }
      "parse"        { Invoke-CheckParse }
      "env-loader"   { Invoke-CheckEnvLoader }
      "python-tools" { Invoke-CheckPythonTools }
      "team-offline" { Invoke-CheckTeamOffline }
      "tests"        { Invoke-CheckTests }
      "pre-push"     { Invoke-CheckPrePush }
    }
    if ($null -eq $script:StepStatus) { Set-Outcome "FAIL" "step did not report an outcome" }
  } catch {
    Set-Outcome "FAIL" ("exception: " + $_.Exception.Message)
  }
  $detail = ([string] $script:StepDetail) -replace "[\r\n]+", " "
  $detail = $detail.Trim()
  $line = ("RESULT " + $Name + " " + $script:StepStatus + " " + $detail).TrimEnd()
  Write-Host $line
  [void] $script:ReportLines.Add($line)
  [void] $script:Results.Add([pscustomobject] @{ Step = $Name; Status = $script:StepStatus; Detail = $detail })
}

# ---- main ----

$engineVersion = $PSVersionTable.PSVersion.ToString()
$engineEdition = [string] $PSVersionTable.PSEdition
if ([string]::IsNullOrEmpty($engineEdition)) { $engineEdition = "Desktop" }
Add-Banner ("windows_check: PowerShell " + $engineVersion + " (" + $engineEdition + ")")

$usageError = $null
$hasOnly = $PSBoundParameters.ContainsKey("Only")
$hasSkip = $PSBoundParameters.ContainsKey("Skip")
$onlyNames = @(ConvertTo-StepNames $Only)
$skipNames = @(ConvertTo-StepNames $Skip)
$unknown = @($onlyNames + $skipNames | Where-Object { $script:StepNames -notcontains $_ })
if ($hasOnly -and $hasSkip) {
  $usageError = "-Only and -Skip cannot be given together"
} elseif ($unknown.Count -gt 0) {
  $usageError = "unknown step name(s): " + ($unknown -join ", ") + " (steps: " + ($script:StepNames -join ", ") + ")"
} elseif ($hasOnly -and ($onlyNames.Count -eq 0)) {
  $usageError = "-Only names no step"
}

if ([string]::IsNullOrEmpty($RepoRoot)) {
  $RepoRoot = Split-Path -Parent $PSScriptRoot
}
if ($null -eq $usageError) {
  if (-not (Test-Path -LiteralPath $RepoRoot -PathType Container)) {
    $usageError = "repository root not found: " + $RepoRoot
  } else {
    $RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
  }
}
if ($null -ne $usageError) {
  Write-Host ("usage error: " + $usageError)
  exit 2
}

# The Python lookup is shared with the other wrappers; a checkout without it runs the Python steps as SKIP.
$resolverPath = Join-Path (Join-Path $RepoRoot "scripts") "resolve_python.ps1"
if (Test-Path -LiteralPath $resolverPath -PathType Leaf) {
  . $resolverPath
  $script:HasResolver = $true
}

$stepsToRun = @()
foreach ($name in $script:StepNames) {
  if ($hasOnly) {
    if ($onlyNames -contains $name) { $stepsToRun += $name }
  } else {
    if ($skipNames -notcontains $name) { $stepsToRun += $name }
  }
}

foreach ($name in $stepsToRun) {
  Invoke-Step $name
}

$failed = @($script:Results | Where-Object { $_.Status -eq "FAIL" }).Count
$exitCode = 0
if ($failed -gt 0) { $exitCode = 1 }

Write-Host ""
Write-Host "SUMMARY"
Write-Host ("{0,-14} {1,-6} {2}" -f "step", "result", "detail")
foreach ($result in $script:Results) {
  Write-Host ("{0,-14} {1,-6} {2}" -f $result.Step, $result.Status, $result.Detail)
}
Write-Host ("exit code: " + $exitCode)

if (-not [string]::IsNullOrEmpty($ReportPath)) {
  try {
    $target = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($ReportPath)
    [void] $script:ReportLines.Add("exit code: " + $exitCode)
    [System.IO.File]::WriteAllText($target, (($script:ReportLines -join "`r`n") + "`r`n"), (New-Object System.Text.UTF8Encoding($false)))
    Write-Host ("report written: " + $target)
  } catch {
    Write-Host ("could not write report: " + $_.Exception.Message)
    if ($exitCode -eq 0) { $exitCode = 1 }
  }
}

exit $exitCode
