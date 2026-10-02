#Requires -Version 5.1
# Shared Python lookup for the PowerShell wrappers.
#
# Every wrapper needs Python 3.10 or newer, and on Windows the name differs by
# install: python.org and winget installs give python.exe and the py launcher but
# no python3.exe, and the Microsoft Store alias python3.exe is a stub that fails
# when Python came from elsewhere. Each candidate is therefore run before it is
# trusted. Order: python3, python, then `py -3`.
#
# Resolve-TeamPython returns @{ Path; Prefix } (Prefix is @("-3") for py, else
# empty) or $null. Run it as:  & $python.Path @($python.Prefix) <script> <args>
# Get-TeamPythonShim writes a `python3` for Git Bash when Bash cannot find one.

$script:TeamPythonResolved = $null

function Test-TeamPythonCandidate {
  param([string] $Path, [string[]] $Prefix)
  # Windows PowerShell 5.1 turns native stderr into a terminating error under
  # "Stop"; a failing stub must only answer false.
  $previous = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    & $Path @Prefix -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
  } catch {
    return $false
  } finally {
    $ErrorActionPreference = $previous
  }
}

function Resolve-TeamPython {
  if ($null -ne $script:TeamPythonResolved) { return $script:TeamPythonResolved }
  foreach ($name in @("python3", "python", "py")) {
    $prefix = if ($name -eq "py") { @("-3") } else { @() }
    foreach ($command in @(Get-Command $name -All -CommandType Application -ErrorAction SilentlyContinue)) {
      if (Test-TeamPythonCandidate -Path $command.Source -Prefix $prefix) {
        $script:TeamPythonResolved = [pscustomobject]@{ Path = $command.Source; Prefix = $prefix }
        return $script:TeamPythonResolved
      }
    }
  }
  return $null
}

# The folder of a one-file shim named python3 (a Bash script that runs the
# resolved interpreter), or $null when it is not needed. Bash is asked first:
# if its own python3 works, nothing is written. The caller puts the folder first
# on PATH for one Bash run and removes it afterwards (Remove-TeamPythonShim).
function Get-TeamPythonShim {
  param([string] $BashPath, [string] $ScratchRoot)
  $previous = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    # Single quotes inside: Windows PowerShell 5.1 passes embedded double quotes to a native command
    # unescaped, which splits this argument in Bash and lets cmd.exe read the ">" as a redirect.
    & $BashPath -c "python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'" 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { return $null }
  } catch {
    # This Bash cannot even be started: no shim can help, and the real run that follows
    # reports the problem in its own words.
    return $null
  } finally {
    $ErrorActionPreference = $previous
  }
  $python = Resolve-TeamPython
  if ($null -eq $python) { return $null }
  $directory = Join-Path $ScratchRoot ("team-python-" + [Guid]::NewGuid().ToString("N"))
  [System.IO.Directory]::CreateDirectory($directory) | Out-Null
  $words = @($python.Path.Replace("\", "/")) + @($python.Prefix)
  $quoted = ($words | ForEach-Object { '"' + $_.Replace('"', '\"') + '"' }) -join " "
  $text = "#!/usr/bin/env bash`nexec $quoted `"`$@`"`n"
  [System.IO.File]::WriteAllText((Join-Path $directory "python3"), $text, (New-Object System.Text.UTF8Encoding($false)))
  return $directory
}

function Remove-TeamPythonShim {
  param([string] $Directory)
  if (-not [string]::IsNullOrEmpty($Directory) -and [System.IO.Directory]::Exists($Directory)) {
    try { [System.IO.Directory]::Delete($Directory, $true) } catch { }
  }
}
