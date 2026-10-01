#Requires -Version 5.1
# No param block: a [Parameter()] attribute makes this an advanced script, and an
# advanced script exits 0 after Ctrl-C whatever its finally block says. $args also
# keeps option-like tokens (--schema=X, -x) that the parameter binder would take
# for parameters, so the first token is always the command, as in team.sh.
$Command = if ($args.Count -gt 0) { [string] $args[0] } else { "" }
$Arguments = @($args | Select-Object -Skip 1 | ForEach-Object { [string] $_ })

$ErrorActionPreference = "Stop"

# Set once the command has ended by its own means: it returned, failed, or chose
# an exit status. The finally block at the bottom treats any other end as Ctrl-C.
$finished = $false

function Fail([string] $Message) {
  [Console]::Error.WriteLine("team error: $Message")
  $script:finished = $true
  exit 2
}

function Exit-Team([int] $Code) {
  $script:finished = $true
  exit $Code
}

function Show-Usage {
  @"
Usage: scripts/team.ps1 <command> [arguments]

Commands:
  doctor                                      Validate .env and the DEV SQLcl identity
  export <app_id>                             Export one numeric APEX app from DEV
  publish <app_id> [--env dev] [--force]      Drift-check and import to DEV
  check-conflicts <folder> [...] (--env <env>|--local)
                                              Preflight selected migrations against local/live scope
  migrate <folder> [...] --env dev|staging|prod
                                              Preflight, then apply selected migration folders
  compare-schema [--from <env>] (--to <env>|--env <env>)
                (--object <name>|--pattern <glob>) [...] [--format text|json]
                                              Compare selected live schema objects read-only
  backup-db                                   Refresh the table and code mirrors
  deploy <app_id> --env <staging|prod> [--manual]
                                              Confirm a promotion or print a DBA runbook
  upgrade-template [--source <url|path>] [--ref <ref>] [--dry-run]
                                              Update template-owned files from the template
Options:
  --schema <NAME>                             Run one configured schema (any command except upgrade-template)
  --help                                      Show this help
"@ | Write-Output
}

# The Bash that runs the migration, comparison and deployment helpers. TEAM_BASH
# names one explicitly. On Windows, bash.exe in System32 or WindowsApps is the
# WSL launcher, which cannot run a Windows script path, and Git for Windows
# keeps only its cmd directory on PATH by default, so Git Bash is looked up
# next to git.exe and in the usual install directories.
function Resolve-TeamBash {
  if (-not [string]::IsNullOrWhiteSpace($env:TEAM_BASH)) {
    if (-not (Test-Path -LiteralPath $env:TEAM_BASH -PathType Leaf)) {
      Fail "TEAM_BASH is set but is not a file: $($env:TEAM_BASH)"
    }
    return $env:TEAM_BASH
  }
  $onWindows = ($PSVersionTable.PSEdition -eq "Desktop") -or ($null -ne $IsWindows -and $IsWindows)
  $found = @(Get-Command bash -All -ErrorAction SilentlyContinue)
  if (-not $onWindows) {
    if ($found.Count -gt 0) { return $found[0].Source }
    return $null
  }
  foreach ($command in $found) {
    if ($command.Source -notmatch '(?i)[\\/](System32|SysWOW64|Sysnative|WindowsApps)[\\/]') { return $command.Source }
  }
  $git = Get-Command git -ErrorAction SilentlyContinue
  if ($null -ne $git) {
    $gitDirectory = Split-Path -Parent $git.Source
    foreach ($relative in @("..\bin\bash.exe", "..\..\bin\bash.exe", "..\usr\bin\bash.exe", "..\..\usr\bin\bash.exe")) {
      $candidate = [System.IO.Path]::GetFullPath((Join-Path $gitDirectory $relative))
      if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
  }
  foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $(if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA "Programs" }))) {
    if ([string]::IsNullOrWhiteSpace($root)) { continue }
    $candidate = Join-Path $root "Git\bin\bash.exe"
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
  }
  return $null
}

function Invoke-TeamBash {
  param([string] $ScriptName, [string[]] $ScriptArguments)
  $bashPath = Resolve-TeamBash
  if ($null -eq $bashPath) {
    Fail "Bash is required for '$ScriptName'; install Git for Windows, set TEAM_BASH to its bash.exe, or run scripts/team.sh"
  }
  & $bashPath (Join-Path $PSScriptRoot $ScriptName) @ScriptArguments
  if ($LASTEXITCODE -ne 0) { Exit-Team $LASTEXITCODE }
}

if ([string]::IsNullOrWhiteSpace($Command) -or $Command -in @("--help", "-h")) {
  Show-Usage
  exit 0
}

# --schema NAME is the one selection channel: strip it and set PROJECT_SCHEMA so
# every child script's loader narrows to that schema.
if ($Command -ne "upgrade-template") {
  $schemaFiltered = @()
  for ($index = 0; $index -lt $Arguments.Count; $index++) {
    if ($Arguments[$index] -eq "--schema") {
      if ($index + 1 -ge $Arguments.Count) { Fail "--schema requires a schema name" }
      $env:PROJECT_SCHEMA = $Arguments[$index + 1]
      $index++
    } elseif ($Arguments[$index] -like "--schema=*") {
      $env:PROJECT_SCHEMA = $Arguments[$index].Substring("--schema=".Length)
    } else {
      $schemaFiltered += $Arguments[$index]
    }
  }
  $Arguments = $schemaFiltered
  if (-not [string]::IsNullOrEmpty($env:PROJECT_SCHEMA) -and $env:PROJECT_SCHEMA -cnotmatch '^[A-Z][A-Z0-9_$#]{0,127}$') {
    Fail "--schema must be an uppercase Oracle identifier"
  }
}

# On Unix, SIGTERM (kill, an IDE's stop button) ends pwsh at once and no finally
# block runs, so an interrupted publish would leave its stamped application.apx
# and its scratch directory with no word about either. Turn SIGTERM into the
# Ctrl-C that the blocks below already handle. Best effort: it needs .NET 6, and
# a missing compiler only means SIGTERM keeps its old effect. Only the commands
# that run in this process need it; the others hand over to Bash or Python.
if ($Command -in @("doctor", "export", "publish", "backup-db") -and $PSVersionTable.PSEdition -eq "Core" -and -not $IsWindows) {
  try {
    Add-Type -ErrorAction Stop -WarningAction SilentlyContinue -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class TeamSignals {
  [DllImport("libc", SetLastError = true)] private static extern int kill(int pid, int signal);
  private static PosixSignalRegistration registration;
  public static void TermBecomesInterrupt() {
    int self = Environment.ProcessId;
    registration = PosixSignalRegistration.Create(PosixSignal.SIGTERM, context => { context.Cancel = true; kill(self, 2); });
  }
}
"@
    [TeamSignals]::TermBecomesInterrupt()
  } catch {
    # Not available here; SIGTERM keeps ending the process at once.
  }
}

try {
  switch ($Command) {
    "doctor" {
      if ($Arguments.Count -ne 0) { Fail "doctor does not accept arguments" }
      . (Join-Path $PSScriptRoot "load_env.ps1") -EnvFile $env:PROJECT_ENV_FILE
      . (Join-Path $PSScriptRoot "invoke_sqlcl.ps1")
      $scratchPath = Join-Path ((Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path) "scratch"
      [System.IO.Directory]::CreateDirectory($scratchPath) | Out-Null
      function Invoke-DoctorOne([string]$Connection, [string]$ExpectedUser, [string]$SchemaName) {
        $sqlclWorkDir = Join-Path $scratchPath ("sqlcl-doctor-" + [Guid]::NewGuid().ToString("N"))
        [System.IO.Directory]::CreateDirectory($sqlclWorkDir) | Out-Null
        $stdinFile = Join-Path $sqlclWorkDir ".sqlcl-stdin"
        $transcriptFile = Join-Path $sqlclWorkDir "sqlcl-output.log"
        New-Item -ItemType File -Path $stdinFile | Out-Null
        try {
          $sqlclExit = Invoke-Sqlcl -WorkingDirectory $sqlclWorkDir -StdInFile $stdinFile `
            -TranscriptFile $transcriptFile -Arguments @(
            "-S", "-noupdates", "-name", $Connection,
            "@$(Join-Path $PSScriptRoot 'doctor.sql')",
            $SchemaName, $env:DB_ENVIRONMENT, $ExpectedUser
          )
          $output = [System.IO.File]::ReadAllText($transcriptFile)
          # Write-Host, not Write-Output: this function returns a status, and anything
          # written to the output stream would become part of that return value.
          Write-Host $output
          if ($sqlclExit -ne 0) {
            [Console]::Error.WriteLine("team error: SQLcl doctor check failed for schema $SchemaName (connection $Connection)")
            return $false
          }
          if ($output -notmatch "(?m)^\s*APEX_DOCTOR_VERIFIED:$([regex]::Escape($ExpectedUser))\s*$") {
            [Console]::Error.WriteLine("team error: SQLcl did not verify the doctor script for schema $SchemaName; the result is unknown")
            return $false
          }
          return $true
        } catch {
          # Invoke-Sqlcl uses Start-Process, which throws when SQLcl cannot start.
          # That is a failed check, not a reason to skip the remaining schemas.
          [Console]::Error.WriteLine("team error: SQLcl doctor check failed for schema $SchemaName (connection $Connection): $($_.Exception.Message)")
          return $false
        } finally {
          if (Test-Path -LiteralPath $sqlclWorkDir) {
            Remove-Item -LiteralPath $sqlclWorkDir -Recurse -Force -ErrorAction SilentlyContinue
          }
        }
      }

      $doctorSeen = @{}
      $doctorTotal = 0
      $doctorFailed = 0
      foreach ($doctorProfile in @("apex", "tables", "code")) {
        switch ($doctorProfile) {
          "apex"   { $doctorSchemas = $env:APEX_PARSING_SCHEMA; $doctorConnections = $env:APEX_SQLCL_CONNECTION; $doctorUsers = $env:APEX_EXPECTED_USER }
          "tables" { $doctorSchemas = $env:TABLES_SCHEMA; $doctorConnections = $env:TABLES_SQLCL_CONNECTION; $doctorUsers = $env:TABLES_EXPECTED_USER }
          "code"   { $doctorSchemas = $env:CODE_SCHEMA; $doctorConnections = $env:CODE_SQLCL_CONNECTION; $doctorUsers = $env:CODE_EXPECTED_USER }
        }
        if ([string]::IsNullOrEmpty($doctorSchemas)) { continue }
        $schemaList = @($doctorSchemas.Split(","))
        $connectionList = @($doctorConnections.Split(","))
        $userList = @($doctorUsers.Split(","))
        for ($doctorIndex = 0; $doctorIndex -lt $schemaList.Count; $doctorIndex++) {
          $doctorKey = "$($connectionList[$doctorIndex])|$($userList[$doctorIndex])|$($schemaList[$doctorIndex])"
          if ($doctorSeen.ContainsKey($doctorKey)) { continue }
          $doctorSeen[$doctorKey] = $true
          $doctorTotal++
          & (Join-Path $PSScriptRoot "check_db_target.ps1") -Operation read -Target $doctorProfile -Schema $schemaList[$doctorIndex]
          if ($env:PROJECT_MULTI_SCHEMA -eq "true") {
            Write-Output "Doctor: schema $($schemaList[$doctorIndex]) via connection $($connectionList[$doctorIndex]) as $($userList[$doctorIndex])"
          }
          if (-not (Invoke-DoctorOne $connectionList[$doctorIndex] $userList[$doctorIndex] $schemaList[$doctorIndex])) { $doctorFailed++ }
        }
      }
      if ($doctorTotal -eq 0) { Fail "no configured profile lists schema $($env:PROJECT_SCHEMA)" }
      if ($doctorFailed -gt 0) { Fail "$doctorFailed of $doctorTotal doctor check(s) failed" }
      if ($doctorTotal -eq 1) {
        Write-Output "Doctor checks passed for the configured DEV connection."
      } else {
        Write-Output "Doctor checks passed for all $doctorTotal configured DEV schema connections."
      }
    }
    "export" {
      if ($Arguments.Count -ne 1) { Fail "usage: scripts/team.ps1 export <numeric_app_id>" }
      if ($Arguments[0] -cnotmatch '^[1-9][0-9]{0,17}$') { Fail "expected a positive numeric application id of at most 18 digits" }
      & (Join-Path $PSScriptRoot "export_apps.ps1") -AppId $Arguments[0]
      if ($LASTEXITCODE -ne 0) { Exit-Team $LASTEXITCODE }
    }
    "publish" {
      if ($Arguments.Count -lt 1) { Fail "usage: scripts/team.ps1 publish <numeric_app_id> [--env dev] [--force]" }
      for ($index = 0; $index -lt $Arguments.Count; $index++) {
        if ($Arguments[$index] -eq "--env") {
          if ($index + 1 -ge $Arguments.Count) { Fail "--env requires dev; use deploy for staging or production" }
          if ($Arguments[$index + 1] -cne "dev") { Fail "publish targets DEV only; use deploy for staging or production" }
        }
      }
      & (Join-Path $PSScriptRoot "publish_app.ps1") @Arguments
      if ($LASTEXITCODE -ne 0) { Exit-Team $LASTEXITCODE }
    }
    "check-conflicts" {
      if ($Arguments.Count -lt 1) { Fail "usage: scripts/team.ps1 check-conflicts <migration-folder> [...] (--env dev|staging|prod | --local)" }
      Invoke-TeamBash -ScriptName "check_conflicts.sh" -ScriptArguments $Arguments
    }
    "migrate" {
      if ($Arguments.Count -lt 1) { Fail "usage: scripts/team.ps1 migrate <migration-folder> [...] --env dev|staging|prod" }
      Invoke-TeamBash -ScriptName "migrate.sh" -ScriptArguments $Arguments
    }
    "compare-schema" {
      if ($Arguments.Count -lt 1) { Fail "usage: scripts/team.ps1 compare-schema [--from <env>] (--to <env>|--env <env>) (--object <name>|--pattern <glob>) [...]" }
      Invoke-TeamBash -ScriptName "compare_schema.sh" -ScriptArguments $Arguments
    }
    "backup-db" {
      if ($Arguments.Count -ne 0) { Fail "backup-db does not accept arguments" }
      & (Join-Path $PSScriptRoot "backup_db.ps1")
      if ($LASTEXITCODE -ne 0) { Exit-Team $LASTEXITCODE }
    }
    "deploy" {
      if ($Arguments.Count -lt 1) { Fail "usage: scripts/team.ps1 deploy <numeric_app_id> --env <staging|prod> [--manual]" }
      Invoke-TeamBash -ScriptName "deploy.sh" -ScriptArguments $Arguments
    }
    "upgrade-template" {
      $python = Get-Command python3 -ErrorAction SilentlyContinue
      if ($null -eq $python) { $python = Get-Command python -ErrorAction SilentlyContinue }
      if ($null -eq $python) { Fail "Python 3 is required to upgrade the template" }
      $repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
      & $python.Source (Join-Path $PSScriptRoot "upgrade_template.py") --project-root $repoRoot @Arguments
      $upgradeStatus = $LASTEXITCODE
      $envFile = Join-Path $repoRoot ".env"
      if ($upgradeStatus -ne 2 -and $Arguments -notcontains "--dry-run" -and (Test-Path -LiteralPath $envFile)) {
        try {
          . (Join-Path $PSScriptRoot "load_env.ps1") -EnvFile $envFile
        } catch {
          Write-Warning ".env needs attention after the upgrade: $($_.Exception.Message)"
        }
      }
      Exit-Team $upgradeStatus
    }
    default { Fail "unknown command '$Command'; use --help to list commands" }
  }
  $finished = $true
} catch {
  $finished = $true
  throw
} finally {
  # Ctrl-C stops this script as well as the helper it was running, so nothing
  # after the helper runs and pwsh would exit 0, which a caller reads as success.
  # It does not run the catch block, so a script that ended without finishing was
  # interrupted: report 130, as Bash does. (Only a script without [Parameter()]
  # attributes keeps an exit status chosen in finally; see the top of the file.)
  if (-not $finished) { exit 130 }
}
