#Requires -Version 5.1
param(
  [Parameter(Position = 0)][string] $Command,
  [Parameter(ValueFromRemainingArguments = $true)][string[]] $Arguments
)

$ErrorActionPreference = "Stop"

function Show-Usage {
  @"
Usage: scripts/team.ps1 <command> [arguments]

Commands:
  doctor                                      Validate .env and the DEV SQLcl identity
  export <app_id>                             Export one numeric APEX app from DEV
  publish <app_id> [--env dev] [--force]      Drift-check and import to DEV
  check-conflicts                             Check migrations across developers
  migrate <migration.sql> [...]               Check conflicts, then apply migration(s)
  backup-db                                   Refresh the table and code mirrors
  deploy <app_id> --env <staging|prod> [--manual]
                                              Confirm a promotion or print a DBA runbook
"@ | Write-Output
}

function Invoke-TeamBash {
  param([string] $ScriptName, [string[]] $ScriptArguments)
  $bash = Get-Command bash -ErrorAction SilentlyContinue
  if ($null -eq $bash) { throw "Bash is required for '$ScriptName'; install Git for Windows or run scripts/team.sh" }
  & $bash.Source (Join-Path $PSScriptRoot $ScriptName) @ScriptArguments
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

if ([string]::IsNullOrWhiteSpace($Command) -or $Command -in @("--help", "-h")) {
  Show-Usage
  exit 0
}

switch ($Command) {
  "doctor" {
    if ($Arguments.Count -ne 0) { throw "doctor does not accept arguments" }
    . (Join-Path $PSScriptRoot "load_env.ps1") -EnvFile $env:PROJECT_ENV_FILE
    & (Join-Path $PSScriptRoot "check_db_target.ps1") -Operation read -Target apex
    . (Join-Path $PSScriptRoot "invoke_sqlcl.ps1")
    $stdinFile = [System.IO.Path]::GetTempFileName()
    try {
      $sqlclExit = Invoke-Sqlcl -WorkingDirectory $PSScriptRoot -StdInFile $stdinFile -Arguments @(
        "-S", "-noupdates", "-name", $env:APEX_SQLCL_CONNECTION,
        "@$(Join-Path $PSScriptRoot 'doctor.sql')",
        $env:APEX_PARSING_SCHEMA, $env:DB_ENVIRONMENT, $env:APEX_EXPECTED_USER
      )
      if ($sqlclExit -ne 0) { throw "SQLcl connection check failed with exit code $sqlclExit" }
      Write-Output "Doctor checks passed for the configured DEV connection."
    } finally {
      Remove-Item -LiteralPath $stdinFile -Force -ErrorAction SilentlyContinue
    }
  }
  "export" {
    if ($Arguments.Count -ne 1 -or $Arguments[0] -cnotmatch '^[1-9][0-9]*$') {
      throw "usage: scripts/team.ps1 export <numeric_app_id>"
    }
    & (Join-Path $PSScriptRoot "export_apps.ps1") -AppId $Arguments[0]
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  }
  "publish" {
    if ($Arguments.Count -lt 1) { throw "usage: scripts/team.ps1 publish <numeric_app_id> [--env dev] [--force]" }
    for ($index = 0; $index -lt $Arguments.Count; $index++) {
      if ($Arguments[$index] -eq "--env") {
        if ($index + 1 -ge $Arguments.Count -or $Arguments[$index + 1] -cne "dev") {
          throw "publish targets DEV only; use deploy for staging or production"
        }
      }
    }
    & (Join-Path $PSScriptRoot "publish_app.ps1") @Arguments
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  }
  "check-conflicts" {
    if ($Arguments.Count -ne 0) { throw "check-conflicts does not accept arguments" }
    $python = Get-Command python3 -ErrorAction SilentlyContinue
    if ($null -eq $python) { $python = Get-Command python -ErrorAction SilentlyContinue }
    if ($null -eq $python) { throw "Python 3 is required to check migration conflicts" }
    & $python.Source (Join-Path $PSScriptRoot "check_conflicts.py")
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  }
  "migrate" {
    if ($Arguments.Count -lt 1) { throw "usage: scripts/team.ps1 migrate <migrations/<developer>/<file>.sql> [...]" }
    Invoke-TeamBash -ScriptName "migrate.sh" -ScriptArguments $Arguments
  }
  "backup-db" {
    if ($Arguments.Count -ne 0) { throw "backup-db does not accept arguments" }
    & (Join-Path $PSScriptRoot "backup_db.ps1")
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  }
  "deploy" {
    if ($Arguments.Count -lt 1) { throw "usage: scripts/team.ps1 deploy <numeric_app_id> --env <staging|prod> [--manual]" }
    Invoke-TeamBash -ScriptName "deploy.sh" -ScriptArguments $Arguments
  }
  default { throw "unknown command '$Command'; use --help to list commands" }
}
