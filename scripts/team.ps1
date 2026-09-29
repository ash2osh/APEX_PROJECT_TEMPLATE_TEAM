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

# --schema NAME is the one selection channel: strip it and set PROJECT_SCHEMA so
# every child script's loader narrows to that schema.
if ($Command -ne "upgrade-template") {
  $schemaFiltered = @()
  for ($index = 0; $index -lt $Arguments.Count; $index++) {
    if ($Arguments[$index] -eq "--schema") {
      if ($index + 1 -ge $Arguments.Count) { throw "--schema requires a schema name" }
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
    throw "--schema must be an uppercase Oracle identifier"
  }
}

switch ($Command) {
  "doctor" {
    if ($Arguments.Count -ne 0) { throw "doctor does not accept arguments" }
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
    if ($doctorTotal -eq 0) { throw "no configured profile lists schema $($env:PROJECT_SCHEMA)" }
    if ($doctorFailed -gt 0) { throw "$doctorFailed of $doctorTotal doctor check(s) failed" }
    if ($doctorTotal -eq 1) {
      Write-Output "Doctor checks passed for the configured DEV connection."
    } else {
      Write-Output "Doctor checks passed for all $doctorTotal configured DEV schema connections."
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
    if ($Arguments.Count -lt 1) { throw "usage: scripts/team.ps1 check-conflicts <migration-folder> [...] (--env dev|staging|prod | --local)" }
    Invoke-TeamBash -ScriptName "check_conflicts.sh" -ScriptArguments $Arguments
  }
  "migrate" {
    if ($Arguments.Count -lt 1) { throw "usage: scripts/team.ps1 migrate <migration-folder> [...] --env dev|staging|prod" }
    Invoke-TeamBash -ScriptName "migrate.sh" -ScriptArguments $Arguments
  }
  "compare-schema" {
    if ($Arguments.Count -lt 1) { throw "usage: scripts/team.ps1 compare-schema [--from <env>] (--to <env>|--env <env>) (--object <name>|--pattern <glob>) [...]" }
    Invoke-TeamBash -ScriptName "compare_schema.sh" -ScriptArguments $Arguments
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
  "upgrade-template" {
    $python = Get-Command python3 -ErrorAction SilentlyContinue
    if ($null -eq $python) { $python = Get-Command python -ErrorAction SilentlyContinue }
    if ($null -eq $python) { throw "Python 3 is required to upgrade the template" }
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
    exit $upgradeStatus
  }
  default { throw "unknown command '$Command'; use --help to list commands" }
}
