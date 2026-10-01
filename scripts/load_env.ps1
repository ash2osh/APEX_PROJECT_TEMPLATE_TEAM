#Requires -Version 5.1
# Dot-source this file to load a strict KEY=VALUE .env file without executing it.
#
# Dot-sourcing runs in the CALLER's scope, so every variable defined here is
# visible to the caller afterwards. Internals are prefixed with `projectEnv`
# and removed at the end to keep that surface small. The one unavoidable
# exception is the `$EnvFile` parameter itself: a caller that also uses a
# variable named `$EnvFile` (PowerShell names are case-insensitive, so
# `$envFile` too) will have it overwritten. Do not use that name in a script
# that dot-sources this one.
param([string]$EnvFile = $env:PROJECT_ENV_FILE)

$ErrorActionPreference = "Stop"
try {
Remove-Item -Path Env:PROD_SQLCL_CONNECTION, Env:PROD_EXPECTED_USER,
  Env:PROD_SCHEMA, Env:STAGING_SQLCL_CONNECTION, Env:STAGING_EXPECTED_USER,
  Env:STAGING_SCHEMA,
  Env:INSTALL_UC_APX, Env:UC_APX_SKILLS_AGENT -ErrorAction SilentlyContinue
$projectEnvRepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
if ([string]::IsNullOrWhiteSpace($EnvFile)) { $EnvFile = Join-Path $projectEnvRepoRoot ".env" }
# Mirror load_env.sh: a relative PROJECT_ENV_FILE resolves against the
# repository root when it is not found relative to the caller's location.
if (-not [System.IO.Path]::IsPathRooted($EnvFile) -and
    -not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
  $projectEnvRootRelative = Join-Path $projectEnvRepoRoot $EnvFile
  if (Test-Path -LiteralPath $projectEnvRootRelative -PathType Leaf) {
    $EnvFile = $projectEnvRootRelative
  }
}
if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
  throw "project environment error: configuration file not found: $EnvFile (copy .env.example to .env)"
}

$projectEnvSeen = @{}
$projectEnvAllowed = @(
  "PROJECT_NAME", "DEVELOPER_NAME", "DB_ENVIRONMENT", "APEX_APP_ID",
  "TABLES_SCHEMA", "TABLES_PREFIXES", "TABLES_SQLCL_CONNECTION", "TABLES_EXPECTED_USER",
  "CODE_SCHEMA", "CODE_PREFIXES", "CODE_SQLCL_CONNECTION", "CODE_EXPECTED_USER",
  "APEX_PARSING_SCHEMA", "APEX_SQLCL_CONNECTION", "APEX_EXPECTED_USER",
  "INSTALL_UC_APX", "UC_APX_SKILLS_AGENT",
  "PROD_SQLCL_CONNECTION", "PROD_EXPECTED_USER", "PROD_SCHEMA",
  "STAGING_SQLCL_CONNECTION", "STAGING_EXPECTED_USER", "STAGING_SCHEMA"
)
foreach ($projectEnvLine in [System.IO.File]::ReadAllLines($EnvFile)) {
  $projectEnvLine = $projectEnvLine.TrimEnd("`r")
  if ([string]::IsNullOrWhiteSpace($projectEnvLine) -or $projectEnvLine.StartsWith("#")) { continue }
  if ($projectEnvLine -cnotmatch '^([A-Z][A-Z0-9_]*)=(.*)$') {
    throw "project environment error: invalid line in ${EnvFile}: $projectEnvLine"
  }
  $projectEnvKey = $Matches[1]
  $projectEnvValue = $Matches[2]
  if ($projectEnvKey -notin $projectEnvAllowed) { throw "project environment error: unsupported setting in ${EnvFile}: $projectEnvKey" }
  if ($projectEnvSeen.ContainsKey($projectEnvKey)) { throw "project environment error: duplicate setting in ${EnvFile}: $projectEnvKey" }
  # The length guard matters: a one-character value of '"' satisfies both
  # StartsWith and EndsWith, and Substring(1, -1) throws.
  $projectEnvQuoted = $false
  if ($projectEnvValue.Length -ge 2 -and
      (($projectEnvValue.StartsWith('"') -and $projectEnvValue.EndsWith('"')) -or
       ($projectEnvValue.StartsWith("'") -and $projectEnvValue.EndsWith("'")))) {
    $projectEnvValue = $projectEnvValue.Substring(1, $projectEnvValue.Length - 2)
    $projectEnvQuoted = $true
  } elseif ($projectEnvValue -eq '"' -or $projectEnvValue -eq "'") {
    throw "project environment error: $projectEnvKey has an unterminated quoted value"
  }
  if (-not $projectEnvQuoted -and $projectEnvValue -match '\s#') {
    throw "project environment error: $projectEnvKey has an inline comment; .env values are parsed literally, so put the comment on its own line, or quote the value to keep a literal '#'"
  }
  Set-Item -LiteralPath "Env:$projectEnvKey" -Value $projectEnvValue
  $projectEnvSeen[$projectEnvKey] = $true
}

# Preserve compatibility with existing .env files while exposing stable
# defaults to project skills.
if (-not $projectEnvSeen.ContainsKey("INSTALL_UC_APX")) { $env:INSTALL_UC_APX = "false" }
if (-not $projectEnvSeen.ContainsKey("UC_APX_SKILLS_AGENT")) { $env:UC_APX_SKILLS_AGENT = "universal" }

$projectEnvRequired = @(
  "PROJECT_NAME", "DEVELOPER_NAME", "DB_ENVIRONMENT", "APEX_APP_ID",
  "TABLES_SCHEMA", "TABLES_PREFIXES", "TABLES_SQLCL_CONNECTION", "TABLES_EXPECTED_USER",
  "CODE_SCHEMA", "CODE_PREFIXES", "CODE_SQLCL_CONNECTION", "CODE_EXPECTED_USER",
  "APEX_PARSING_SCHEMA", "APEX_SQLCL_CONNECTION", "APEX_EXPECTED_USER"
)
foreach ($projectEnvKey in $projectEnvRequired) {
  if (-not $projectEnvSeen.ContainsKey($projectEnvKey) -or
      [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($projectEnvKey, "Process"))) {
    throw "project environment error: $projectEnvKey is required in $EnvFile"
  }
}
function Assert-ProjectEnvUniqueCsv([string]$Name, [string]$Value) {
  $projectEnvCsvSeen = @{}
  foreach ($projectEnvCsvItem in $Value.Split(',')) {
    if ($projectEnvCsvSeen.ContainsKey($projectEnvCsvItem)) {
      throw "$Name must not contain duplicate values: $projectEnvCsvItem"
    }
    $projectEnvCsvSeen[$projectEnvCsvItem] = $true
  }
}

function Split-ProjectEnvList([string]$Value) {
  # A function's output is unrolled, so callers must wrap the call in @( ):
  # that restores an array for zero or one entries. (Do not use `return ,@(...)`;
  # it emits the array as ONE object, and @( ) would then count it as one entry.)
  if ([string]::IsNullOrEmpty($Value)) { return }
  return $Value.Split(',')
}
function Assert-ProjectEnvList([string]$Name, [string]$Kind) {
  $projectEnvListValue = [Environment]::GetEnvironmentVariable($Name, "Process")
  if ([string]::IsNullOrEmpty($projectEnvListValue)) { return }
  if ($projectEnvListValue -cmatch '(^,|,$|,,)') { throw "$Name must not contain empty entries" }
  foreach ($projectEnvListItem in @(Split-ProjectEnvList $projectEnvListValue)) {
    if ($Kind -eq "identifier" -and $projectEnvListItem -cnotmatch '^[A-Z][A-Z0-9_$#]{0,127}$') {
      throw "$Name must be an uppercase Oracle identifier"
    }
    if ($Kind -eq "alias" -and $projectEnvListItem -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$') {
      throw "$Name contains unsupported characters"
    }
  }
}
function Assert-ProjectEnvTriple([string]$SchemaKey, [string]$ConnectionKey, [string]$UserKey) {
  $projectEnvSchemas = @(Split-ProjectEnvList ([Environment]::GetEnvironmentVariable($SchemaKey, "Process")))
  $projectEnvConnections = @(Split-ProjectEnvList ([Environment]::GetEnvironmentVariable($ConnectionKey, "Process")))
  $projectEnvUsers = @(Split-ProjectEnvList ([Environment]::GetEnvironmentVariable($UserKey, "Process")))
  if ($projectEnvSchemas.Count -ne $projectEnvConnections.Count -or $projectEnvSchemas.Count -ne $projectEnvUsers.Count) {
    throw "$ConnectionKey, $UserKey and $SchemaKey must list the same number of entries"
  }
  Assert-ProjectEnvUniqueCsv -Name $SchemaKey -Value ([Environment]::GetEnvironmentVariable($SchemaKey, "Process"))
}
foreach ($projectEnvPrefix in @("PROD", "STAGING")) {
  $projectEnvConnectionKey = "${projectEnvPrefix}_SQLCL_CONNECTION"
  $projectEnvUserKey = "${projectEnvPrefix}_EXPECTED_USER"
  $projectEnvSchemaKey = "${projectEnvPrefix}_SCHEMA"
  $projectEnvConnectionSeen = $projectEnvSeen.ContainsKey($projectEnvConnectionKey)
  $projectEnvUserSeen = $projectEnvSeen.ContainsKey($projectEnvUserKey)
  $projectEnvSchemaSeen = $projectEnvSeen.ContainsKey($projectEnvSchemaKey)
  if ($projectEnvConnectionSeen -ne $projectEnvUserSeen) {
    throw "$projectEnvConnectionKey and $projectEnvUserKey must be configured together"
  }
  if ($projectEnvSchemaSeen -and -not $projectEnvConnectionSeen) {
    throw "$projectEnvSchemaKey requires $projectEnvConnectionKey and $projectEnvUserKey"
  }
  if ($projectEnvConnectionSeen) {
    $projectEnvConnectionValue = [Environment]::GetEnvironmentVariable($projectEnvConnectionKey, "Process")
    $projectEnvUserValue = [Environment]::GetEnvironmentVariable($projectEnvUserKey, "Process")
    if ([string]::IsNullOrWhiteSpace($projectEnvConnectionValue) -or
        [string]::IsNullOrWhiteSpace($projectEnvUserValue)) {
      throw "$projectEnvConnectionKey and $projectEnvUserKey must not be empty"
    }
    Assert-ProjectEnvList -Name $projectEnvConnectionKey -Kind alias
    Assert-ProjectEnvList -Name $projectEnvUserKey -Kind identifier
    if ($projectEnvSchemaSeen) {
      Assert-ProjectEnvTriple -SchemaKey $projectEnvSchemaKey -ConnectionKey $projectEnvConnectionKey -UserKey $projectEnvUserKey
    } else {
      $projectEnvConnectionCount = @(Split-ProjectEnvList $projectEnvConnectionValue).Count
      if ($projectEnvConnectionCount -ne @(Split-ProjectEnvList $projectEnvUserValue).Count) {
        throw "$projectEnvConnectionKey and $projectEnvUserKey must list the same number of entries"
      }
      if ($projectEnvConnectionCount -gt 1) {
        throw "$projectEnvSchemaKey is required when $projectEnvConnectionKey lists several connections"
      }
    }
  }
}
if ($env:APEX_APP_ID -notmatch '^[1-9][0-9]*(,[1-9][0-9]*)*$') {
  throw "APEX_APP_ID must be a comma-separated list of positive integers without spaces"
}
Assert-ProjectEnvUniqueCsv -Name "APEX_APP_ID" -Value $env:APEX_APP_ID
foreach ($projectEnvKey in @("TABLES_PREFIXES", "CODE_PREFIXES")) {
  $projectEnvPrefixValue = [Environment]::GetEnvironmentVariable($projectEnvKey, "Process")
  if ($projectEnvPrefixValue -eq "*") { continue }
  if ($projectEnvPrefixValue -cnotmatch '^[A-Z][A-Z0-9_$#]*(,[A-Z][A-Z0-9_$#]*)*$') {
    throw "$projectEnvKey must be * or a comma-separated list of uppercase Oracle identifier prefixes without spaces"
  }
  Assert-ProjectEnvUniqueCsv -Name $projectEnvKey -Value $projectEnvPrefixValue
  foreach ($projectEnvPrefixItem in $projectEnvPrefixValue.Split(',')) {
    if ($projectEnvPrefixItem.Length -gt 128) {
      throw "$projectEnvKey prefixes must be at most 128 characters"
    }
  }
}
# DEV publish stamps this name into the app version tag, where '-' separates it
# from the date.
if ($env:DEVELOPER_NAME -cnotmatch '^[A-Z][A-Z0-9_]{0,29}$') {
  throw "DEVELOPER_NAME must be uppercase letters, digits, or underscores (at most 30), such as ASHARIF"
}
# -cnotin: -notin ignores case, but load_env.sh and the Bash helpers do not.
if ($env:DB_ENVIRONMENT -cnotin @("development", "test", "staging", "production")) {
  throw "DB_ENVIRONMENT must be development, test, staging, or production"
}
if ($env:INSTALL_UC_APX -cnotin @("true", "false")) { throw "INSTALL_UC_APX must be true or false" }
if ($env:UC_APX_SKILLS_AGENT -cnotin @("universal", "claude-code")) {
  throw "UC_APX_SKILLS_AGENT must be universal or claude-code"
}
foreach ($projectEnvKey in @("TABLES_SCHEMA", "TABLES_EXPECTED_USER", "CODE_SCHEMA", "CODE_EXPECTED_USER",
    "APEX_PARSING_SCHEMA", "APEX_EXPECTED_USER", "STAGING_SCHEMA", "PROD_SCHEMA")) {
  Assert-ProjectEnvList -Name $projectEnvKey -Kind identifier
}
foreach ($projectEnvKey in @("TABLES_SQLCL_CONNECTION", "CODE_SQLCL_CONNECTION", "APEX_SQLCL_CONNECTION")) {
  Assert-ProjectEnvList -Name $projectEnvKey -Kind alias
}
Assert-ProjectEnvTriple -SchemaKey TABLES_SCHEMA -ConnectionKey TABLES_SQLCL_CONNECTION -UserKey TABLES_EXPECTED_USER
Assert-ProjectEnvTriple -SchemaKey CODE_SCHEMA -ConnectionKey CODE_SQLCL_CONNECTION -UserKey CODE_EXPECTED_USER
Assert-ProjectEnvTriple -SchemaKey APEX_PARSING_SCHEMA -ConnectionKey APEX_SQLCL_CONNECTION -UserKey APEX_EXPECTED_USER

# The configured schemas, and whether any list names more than one.
$projectEnvUnion = @()
$projectEnvMulti = $false
foreach ($projectEnvKey in @("TABLES_SCHEMA", "CODE_SCHEMA", "APEX_PARSING_SCHEMA")) {
  $projectEnvItems = @(Split-ProjectEnvList ([Environment]::GetEnvironmentVariable($projectEnvKey, "Process")))
  if ($projectEnvItems.Count -gt 1) { $projectEnvMulti = $true }
  foreach ($projectEnvItem in $projectEnvItems) {
    if ($projectEnvUnion -cnotcontains $projectEnvItem) { $projectEnvUnion += $projectEnvItem }
  }
}
foreach ($projectEnvKey in @("STAGING_SCHEMA", "PROD_SCHEMA", "STAGING_SQLCL_CONNECTION", "PROD_SQLCL_CONNECTION")) {
  if (@(Split-ProjectEnvList ([Environment]::GetEnvironmentVariable($projectEnvKey, "Process"))).Count -gt 1) {
    $projectEnvMulti = $true
  }
}
# The project's one DEV schema (CODE_SCHEMA with exactly one entry), captured
# before narrowing rewrites it. Only that schema may map to a differently named
# staging or production schema; the Python resolver applies the same rule.
$projectEnvDevSchema = ""
if (@(Split-ProjectEnvList $env:CODE_SCHEMA).Count -eq 1) { $projectEnvDevSchema = $env:CODE_SCHEMA }
$env:PROJECT_SCHEMAS = ($projectEnvUnion -join ",")
$env:PROJECT_MULTI_SCHEMA = if ($projectEnvMulti) { "true" } else { "false" }
$env:PROJECT_CODE_SCHEMAS = $env:CODE_SCHEMA

function Set-ProjectEnvNarrow([string]$SchemaKey, [string]$ConnectionKey, [string]$UserKey, [string]$Mode) {
  $schemas = @(Split-ProjectEnvList ([Environment]::GetEnvironmentVariable($SchemaKey, "Process")))
  $connections = @(Split-ProjectEnvList ([Environment]::GetEnvironmentVariable($ConnectionKey, "Process")))
  $users = @(Split-ProjectEnvList ([Environment]::GetEnvironmentVariable($UserKey, "Process")))
  $index = -1
  for ($i = 0; $i -lt $schemas.Count; $i++) {
    if ($schemas[$i] -ceq $env:PROJECT_SCHEMA) { $index = $i; break }
  }
  if ($index -ge 0) {
    Set-Item -LiteralPath "Env:$SchemaKey" -Value $schemas[$index]
    Set-Item -LiteralPath "Env:$ConnectionKey" -Value $connections[$index]
    Set-Item -LiteralPath "Env:$UserKey" -Value $users[$index]
  } elseif ($Mode -eq "lenient" -and -not $projectEnvMulti -and $schemas.Count -eq 1 -and
      $projectEnvDevSchema -ne "" -and $env:PROJECT_SCHEMA -ceq $projectEnvDevSchema) {
    # A project with one DEV schema may name staging or production differently,
    # for that schema only.
  } else {
    # An empty value would be removed by Set-Item on some hosts, so set it
    # through the .NET API, which keeps a defined-but-empty variable.
    foreach ($name in @($SchemaKey, $ConnectionKey, $UserKey)) {
      [Environment]::SetEnvironmentVariable($name, "", "Process")
    }
  }
}

if (-not [string]::IsNullOrEmpty($env:PROJECT_SCHEMA)) {
  if ($env:PROJECT_SCHEMA -cnotmatch '^[A-Z][A-Z0-9_$#]{0,127}$') {
    throw "PROJECT_SCHEMA must be an uppercase Oracle identifier"
  }
  if (@($projectEnvUnion) -cnotcontains $env:PROJECT_SCHEMA) {
    throw "schema $($env:PROJECT_SCHEMA) is not configured; configured schemas: $($env:PROJECT_SCHEMAS)"
  }
  Set-ProjectEnvNarrow TABLES_SCHEMA TABLES_SQLCL_CONNECTION TABLES_EXPECTED_USER strict
  Set-ProjectEnvNarrow CODE_SCHEMA CODE_SQLCL_CONNECTION CODE_EXPECTED_USER strict
  Set-ProjectEnvNarrow APEX_PARSING_SCHEMA APEX_SQLCL_CONNECTION APEX_EXPECTED_USER strict
  foreach ($projectEnvPrefix in @("STAGING", "PROD")) {
    if (-not [string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable("${projectEnvPrefix}_SCHEMA", "Process"))) {
      Set-ProjectEnvNarrow "${projectEnvPrefix}_SCHEMA" "${projectEnvPrefix}_SQLCL_CONNECTION" "${projectEnvPrefix}_EXPECTED_USER" lenient
    }
  }
}

# Kept after the load so a script can refuse to guess between schemas.
function Assert-ProjectEnvSingleSchema([string]$Label) {
  if ($env:PROJECT_MULTI_SCHEMA -eq "true" -and [string]::IsNullOrEmpty($env:PROJECT_SCHEMA)) {
    throw "$Label needs one schema because several are configured ($($env:PROJECT_SCHEMAS)); pass --schema <NAME>"
  }
}

# Mirror load_env.sh, which unsets its own temporaries after a successful load.
Remove-Variable -Name projectEnvRepoRoot, projectEnvSeen, projectEnvAllowed,
  projectEnvRequired, projectEnvLine, projectEnvKey, projectEnvValue,
  projectEnvPrefixValue, projectEnvPrefixItem, projectEnvQuoted,
  projectEnvPrefix, projectEnvConnectionKey, projectEnvUserKey, projectEnvSchemaKey,
  projectEnvConnectionSeen, projectEnvUserSeen, projectEnvConnectionValue,
  projectEnvUserValue, projectEnvSchemaSeen, projectEnvSchemaValue,
  projectEnvRootRelative, projectEnvUnion, projectEnvMulti, projectEnvDevSchema, projectEnvItems,
  projectEnvItem, projectEnvConnectionCount `
  -ErrorAction SilentlyContinue
Remove-Item -Path Function:Assert-ProjectEnvUniqueCsv, Function:Split-ProjectEnvList,
  Function:Assert-ProjectEnvList, Function:Assert-ProjectEnvTriple,
  Function:Set-ProjectEnvNarrow -ErrorAction SilentlyContinue
}
finally {
  Remove-Variable -Name projectEnvRepoRoot, projectEnvRootRelative -ErrorAction SilentlyContinue
}
