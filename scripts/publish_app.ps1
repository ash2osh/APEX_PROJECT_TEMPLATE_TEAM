#Requires -Version 5.1
param(
  [Parameter(Position = 0)][string] $AppId,
  [Parameter(ValueFromRemainingArguments = $true)][string[]] $RemainingArguments
)

$ErrorActionPreference = "Stop"

function Show-Usage {
  @"
Usage: scripts/publish_app.ps1 <app_id> [--env <dev|staging|prod>] [--force]

Imports one APEXlang application with deployments/<env>.json. Staging and
production imports require interactive confirmation. --force skips only the
Builder drift check; it does not skip target confirmation or identity checks.
"@ | Write-Output
}

if ($AppId -in @("--help", "-h") -or $RemainingArguments -contains "--help" -or
    $RemainingArguments -contains "-h") {
  Show-Usage
  exit 0
}
if ([string]::IsNullOrWhiteSpace($AppId) -or $AppId -cnotmatch '^[1-9][0-9]*$') {
  throw "publish error: expected a positive numeric application id"
}

$appEnvironment = "dev"
$force = $false
$describe = $false
for ($index = 0; $index -lt $RemainingArguments.Count;) {
  switch ($RemainingArguments[$index]) {
    "--env" {
      if ($index + 1 -ge $RemainingArguments.Count) {
        throw "publish error: --env requires dev, staging, or prod"
      }
      $appEnvironment = $RemainingArguments[$index + 1]
      $index += 2
    }
    "--force" {
      $force = $true
      $index += 1
    }
    "--describe" {
      $describe = $true
      $index += 1
    }
    "--help" { Show-Usage; exit 0 }
    "-h" { Show-Usage; exit 0 }
    default { throw "publish error: unknown option: $($RemainingArguments[$index])" }
  }
}
if ($appEnvironment -notin @("dev", "staging", "prod")) {
  throw "publish error: unsupported environment '$appEnvironment'; use dev, staging, or prod"
}

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
. (Join-Path $PSScriptRoot "load_env.ps1") -EnvFile $env:PROJECT_ENV_FILE

$preferredAppDir = Join-Path $repoRoot "apps/$($env:APEX_PARSING_SCHEMA)/$AppId"
if (Test-Path -LiteralPath $preferredAppDir -PathType Container) {
  $appDir = (Resolve-Path -LiteralPath $preferredAppDir).Path
} else {
  $candidates = @()
  $directAppDir = Join-Path $repoRoot "apps/$AppId"
  if (Test-Path -LiteralPath $directAppDir -PathType Container) {
    $candidates += (Resolve-Path -LiteralPath $directAppDir).Path
  }
  foreach ($schemaDir in (Get-ChildItem -LiteralPath (Join-Path $repoRoot "apps") -Directory -ErrorAction SilentlyContinue)) {
    $candidate = Join-Path $schemaDir.FullName $AppId
    if (Test-Path -LiteralPath $candidate -PathType Container) {
      $candidates += (Resolve-Path -LiteralPath $candidate).Path
    }
  }
  $candidates = @($candidates | Select-Object -Unique)
  if ($candidates.Count -eq 0) {
    throw "publish error: no application source directory found for id $AppId under apps/<schema>/$AppId or apps/$AppId"
  }
  if ($candidates.Count -gt 1) {
    throw "publish error: application id $AppId resolves to multiple source directories; keep it unique or configure APEX_PARSING_SCHEMA"
  }
  $appDir = $candidates[0]
}

$deploymentFile = Join-Path $appDir "deployments/$appEnvironment.json"
if (-not (Test-Path -LiteralPath $deploymentFile -PathType Leaf)) {
  throw "publish error: deployment descriptor not found: $deploymentFile"
}
try {
  $deployment = Get-Content -LiteralPath $deploymentFile -Raw | ConvertFrom-Json
} catch {
  throw "publish error: invalid deployment descriptor: $($_.Exception.Message)"
}
if ($deployment.workspace.name -isnot [string] -or [string]::IsNullOrWhiteSpace($deployment.workspace.name)) {
  throw "publish error: deployment workspace.name must be a non-empty string"
}
$deploymentAppId = 0L
if ($deployment.app.id -is [bool] -or
    -not [Int64]::TryParse([string]$deployment.app.id, [ref]$deploymentAppId) -or
    $deploymentAppId -ne [Int64]$AppId) {
  throw "publish error: deployment app.id must be numeric $AppId"
}
$parsingSchema = [string]$deployment.app.databaseSession.parsingSchema
if ($parsingSchema -cnotmatch '^[A-Z][A-Z0-9_$#]{0,127}$') {
  throw "publish error: deployment parsingSchema must be an uppercase Oracle identifier"
}
if (-not (Test-Path -LiteralPath (Join-Path $appDir "application.apx") -PathType Leaf) -and
    -not (Test-Path -LiteralPath (Join-Path $appDir ".apex/apexlang.json") -PathType Leaf) -and
    $null -eq (Get-ChildItem -LiteralPath $appDir -Filter *.apx -File -Recurse | Select-Object -First 1)) {
  throw "publish error: no APEXlang source found in $appDir"
}

switch ($appEnvironment) {
  "dev" {
    $sqlclConnection = $env:APEX_SQLCL_CONNECTION
    $expectedUser = $env:APEX_EXPECTED_USER
    $targetLabel = "DEV"
    $targetEnvironment = $env:DB_ENVIRONMENT
  }
  "staging" {
    $sqlclConnection = $env:STAGING_SQLCL_CONNECTION
    $expectedUser = $env:STAGING_EXPECTED_USER
    $targetLabel = "STAGING"
    $targetEnvironment = "staging"
  }
  "prod" {
    $sqlclConnection = $env:PROD_SQLCL_CONNECTION
    $expectedUser = $env:PROD_EXPECTED_USER
    $targetLabel = "PROD"
    $targetEnvironment = "production"
  }
}

if ($describe) {
  # Internal read-only interface for deployment summaries and DBA runbooks.
  Write-Output (@($appDir, $deployment.workspace.name, $parsingSchema,
    $sqlclConnection, $expectedUser, $targetEnvironment) -join "`t")
  exit 0
}

if ($appEnvironment -ne "dev" -and
    ([string]::IsNullOrWhiteSpace($sqlclConnection) -or [string]::IsNullOrWhiteSpace($expectedUser))) {
  if ($appEnvironment -eq "staging") {
    throw "publish error: set STAGING_SQLCL_CONNECTION and STAGING_EXPECTED_USER in .env to publish to staging"
  }
  throw "publish error: set PROD_SQLCL_CONNECTION and PROD_EXPECTED_USER in .env to publish to production"
}

if ($appEnvironment -ne "dev") {
  $answer = Read-Host "Deploying to $targetLabel. Proceed? [y/N]"
  if ($answer -notmatch '^(?i:y|yes)$') { throw "Publish cancelled." }
}

if ($appEnvironment -eq "dev" -and -not $force) {
  $driftGuard = Join-Path $repoRoot "scripts/check_builder_drift.py"
  if (-not (Test-Path -LiteralPath $driftGuard -PathType Leaf)) {
    throw "publish error: Builder drift guard is missing; refusing import"
  }
  $python = Get-Command python3 -ErrorAction SilentlyContinue
  if ($null -eq $python) { $python = Get-Command python -ErrorAction SilentlyContinue }
  if ($null -eq $python) { $python = Get-Command py -ErrorAction SilentlyContinue }
  if ($null -eq $python) { throw "Python 3 is required to check Builder drift" }
  if ($python.Name -in @("py.exe", "py")) {
    & $python.Source -3 $driftGuard $AppId $sqlclConnection $appDir --expected-user $expectedUser
  } else {
    & $python.Source $driftGuard $AppId $sqlclConnection $appDir --expected-user $expectedUser
  }
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

if ($appEnvironment -eq "dev") {
  & (Join-Path $PSScriptRoot "check_db_target.ps1") -Operation write -Target apex
}

. (Join-Path $PSScriptRoot "invoke_sqlcl.ps1")
$stdinFile = [System.IO.Path]::GetTempFileName()
$transcriptFile = [System.IO.Path]::GetTempFileName()
try {
  $relativeDeployment = "deployments/$appEnvironment.json"
  $sqlclExit = Invoke-Sqlcl -WorkingDirectory $appDir -StdInFile $stdinFile -Arguments @(
    "-S", "-noupdates", "-name", $sqlclConnection,
    "@$(Join-Path $PSScriptRoot 'publish_app.sql')",
    $parsingSchema, $targetEnvironment, $expectedUser, $relativeDeployment, $AppId
  ) -TranscriptFile $transcriptFile
  $sqlclOutput = [System.IO.File]::ReadAllText($transcriptFile)
  if (-not [string]::IsNullOrEmpty($sqlclOutput)) { Write-Output $sqlclOutput }
  if ($sqlclExit -ne 0) { throw "SQLcl application import failed with exit code $sqlclExit" }
  if ([regex]::IsMatch($sqlclOutput, '(?i)\b(?:SP2|TNS|ORA|PLS|SQL)-[0-9]{4,5}:')) {
    throw "SQLcl reported a client or database error during the application import"
  }
  if (-not [regex]::IsMatch($sqlclOutput, "(?m)^\s*APEX_IMPORT_VERIFIED:$AppId\s*$")) {
    throw "SQLcl did not verify the imported application; the import result is unknown"
  }
  Write-Output "Published APEX App $AppId to $targetLabel ($($deployment.workspace.name) / $parsingSchema)."
} finally {
  Remove-Item -LiteralPath $stdinFile, $transcriptFile -Force -ErrorAction SilentlyContinue
}
