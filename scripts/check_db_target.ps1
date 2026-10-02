#Requires -Version 5.1
# Pre-connect environment classification. Database identity is verified in SQL.
#
# Production safety in this template is an instruction to the client, not a
# privilege audit: read targets are allowed, write operation classes are
# refused, and the operator is told to run SELECT statements only.
param(
  [Parameter(Mandatory = $true)][ValidateSet("read", "write")][string]$Operation,
  [Parameter(Mandatory = $true)][ValidateSet("tables", "code", "apex")][string]$Target,
  [string]$Schema
)

$ErrorActionPreference = "Stop"
# `&` isolates variables, not the process environment. This script selects a
# schema and dot-sources the loader, both of which rewrite $env:, so restore the
# caller's environment exactly on the way out (also when it throws).
$checkTargetEnvSnapshot = [Environment]::GetEnvironmentVariables("Process")
try {
  # A schema argument is the same selection as --schema; the loader narrows on it.
  if (-not [string]::IsNullOrEmpty($Schema)) { $env:PROJECT_SCHEMA = $Schema }
  . (Join-Path $PSScriptRoot "load_env.ps1") -EnvFile $env:PROJECT_ENV_FILE

  switch ($Target) {
    "tables" { $targetConnection = $env:TABLES_SQLCL_CONNECTION }
    "code"   { $targetConnection = $env:CODE_SQLCL_CONNECTION }
    "apex"   { $targetConnection = $env:APEX_SQLCL_CONNECTION }
  }

  Assert-ProjectEnvSingleSchema -Label "check_db_target ($Target)"
  if ([string]::IsNullOrEmpty($targetConnection)) {
    throw "the $Target profile does not list schema $($env:PROJECT_SCHEMA)"
  }

  # Same production marker as scripts/db_targets.py.
  $productionPattern = '(?i)(^|[^A-Za-z0-9])(production|live)[0-9]*([^A-Za-z0-9]|$)|(prod|prd)[0-9]*([^A-Za-z0-9]|$)|(^|[^A-Za-z0-9])(prod|prd)(db|[0-9])'
  $nonProductionPattern = '(?i)(pre|non)[-_.]?(prod|prd)'
  # Remove pre-production words (PREPROD, non-prod) before looking for a marker.
  $connectionWords = $targetConnection -replace $nonProductionPattern, ' '
  if ($connectionWords -match $productionPattern -and $env:DB_ENVIRONMENT -ne "production") {
    throw "$Target connection '$targetConnection' resembles production but DB_ENVIRONMENT=$($env:DB_ENVIRONMENT); ask the user whether this is production before continuing"
  }
  if ($env:DB_ENVIRONMENT -eq "production") {
    if ($Operation -ne "read") { throw "production database operations are always read-only; '$Operation' is blocked" }
    Write-Warning @"
PRODUCTION SESSION - READ ONLY
  Run SELECT statements only.
  Do NOT run INSERT, UPDATE, DELETE, MERGE, or any other DML.
  Do NOT run CREATE, ALTER, DROP, TRUNCATE, or any other DDL.
  Do NOT COMMIT. Prepare changes for an approved deployment instead.
  This is not enforced by the database. It is your contract.
"@
  }
} finally {
  $checkTargetEnvNow = [Environment]::GetEnvironmentVariables("Process")
  foreach ($checkTargetName in @($checkTargetEnvNow.Keys)) {
    if (-not $checkTargetEnvSnapshot.Contains($checkTargetName)) {
      [Environment]::SetEnvironmentVariable($checkTargetName, $null, "Process")
    }
  }
  foreach ($checkTargetName in @($checkTargetEnvSnapshot.Keys)) {
    if ($checkTargetEnvNow[$checkTargetName] -cne $checkTargetEnvSnapshot[$checkTargetName]) {
      [Environment]::SetEnvironmentVariable($checkTargetName, [string]$checkTargetEnvSnapshot[$checkTargetName], "Process")
    }
  }
}
