#Requires -Version 5.1
param(
  [Parameter(Position = 0)][string] $AppId,
  [Parameter(ValueFromRemainingArguments = $true)][string[]] $RemainingArguments
)

$ErrorActionPreference = "Stop"

function Show-Usage {
  @"
Usage: scripts/publish_app.ps1 <app_id> [--env <dev|staging|prod>] [--force]
       scripts/publish_app.ps1 <app_id> --env dev --file <pages/file.apx> [--file ...] [--no-team-notice]

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
if ([string]::IsNullOrWhiteSpace($AppId) -or $AppId -cnotmatch '^[1-9][0-9]{0,17}$') {
  throw "publish error: expected a positive numeric application id of at most 18 digits"
}

$appEnvironment = "dev"
$force = $false
$describe = $false
$selectedFiles = @()
$noTeamNotice = $false
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
    '--file' {
      if ($index + 1 -ge $RemainingArguments.Count -or [string]::IsNullOrEmpty($RemainingArguments[$index + 1])) { throw '--file requires an app-relative page path' }
      $selectedFiles += $RemainingArguments[$index + 1]
      $index += 2
    }
    '--no-team-notice' { $noTeamNotice = $true; $index += 1 }
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
if ($selectedFiles.Count -gt 0) {
  if ($appEnvironment -cne 'dev') { throw 'partial publishing targets DEV only' }
  if ($force) { throw 'partial publishing does not accept --force' }
  if ($describe) { throw '--describe cannot be combined with --file' }
} elseif ($noTeamNotice) { throw '--no-team-notice requires --file' }

# An & script call has its own variables but shares the caller's process
# environment. Restore the loader's narrowed values on every exit path.
$publishEnvSnapshot = [Environment]::GetEnvironmentVariables("Process")
try {
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
. (Join-Path $PSScriptRoot "load_env.ps1") -EnvFile $env:PROJECT_ENV_FILE
. (Join-Path $PSScriptRoot "resolve_python.ps1")

$selectedSchema = if (-not [string]::IsNullOrEmpty($env:PROJECT_SCHEMA)) { $env:PROJECT_SCHEMA } else { $env:APEX_PARSING_SCHEMA }
$preferredAppDir = Join-Path $repoRoot "apps/$selectedSchema/$AppId"
if (Test-Path -LiteralPath $preferredAppDir -PathType Container) {
  $appDir = $preferredAppDir
} else {
  $candidates = @()
  $directAppDir = Join-Path $repoRoot "apps/$AppId"
  if (Test-Path -LiteralPath $directAppDir -PathType Container) {
    $candidates += $directAppDir
  }
  foreach ($schemaDir in (Get-ChildItem -LiteralPath (Join-Path $repoRoot "apps") -Directory -ErrorAction SilentlyContinue)) {
    $candidate = Join-Path $schemaDir.FullName $AppId
    if (Test-Path -LiteralPath $candidate -PathType Container) {
      $candidates += $candidate
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

$python = Resolve-TeamPython
if ($null -eq $python) { throw "Python 3.10 or newer is required to validate the application source tree (python3, python or py -3)" }
$sourceValidator = Join-Path $PSScriptRoot "validate_app_source.py"
if (-not (Test-Path -LiteralPath $sourceValidator -PathType Leaf)) {
  throw "publish error: application source validator is missing; refusing import"
}
& $python.Path @($python.Prefix) $sourceValidator $repoRoot $appDir
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$appDir = [System.IO.Path]::GetFullPath($appDir)

$deploymentFile = Join-Path $appDir "deployments/$appEnvironment.json"
if (-not (Test-Path -LiteralPath $deploymentFile -PathType Leaf)) {
  throw "publish error: deployment descriptor not found: $deploymentFile"
}
$descriptorScript = Join-Path $PSScriptRoot 'deployment_descriptor.py'
$descriptorJson = & $python.Path @($python.Prefix) $descriptorScript $deploymentFile $AppId --json
if ($LASTEXITCODE -ne 0) { throw 'publish error: invalid deployment descriptor; see the property error above' }
$deployment = $descriptorJson | ConvertFrom-Json
$parsingSchema = [string]$deployment.app.databaseSession.parsingSchema
# With several schemas the descriptor's parsing schema selects the connection.
# Folder, descriptor, --schema and (below) the live app must all agree.
if ($env:PROJECT_MULTI_SCHEMA -eq "true") {
  $appFolderSchema = Split-Path -Leaf (Split-Path -Parent $appDir)
  if ($appFolderSchema -cne $parsingSchema) {
    throw "publish error: application $AppId is stored under apps/$appFolderSchema but its descriptor parses as $parsingSchema; move the folder or fix the descriptor"
  }
  if (-not [string]::IsNullOrEmpty($env:PROJECT_SCHEMA) -and $env:PROJECT_SCHEMA -cne $parsingSchema) {
    throw "publish error: --schema $($env:PROJECT_SCHEMA) does not match the application's parsing schema $parsingSchema"
  }
  $env:PROJECT_SCHEMA = $parsingSchema
  . (Join-Path $PSScriptRoot "load_env.ps1") -EnvFile $env:PROJECT_ENV_FILE
} elseif ($appEnvironment -eq "dev") {
  # One schema: the DEV descriptor must name it, and the folder is named after it.
  # Staging and production descriptors may name other schemas (apps/templates).
  $appFolderSchema = Split-Path -Leaf (Split-Path -Parent $appDir)
  if ($appFolderSchema -cne $parsingSchema) {
    throw "publish error: application $AppId is stored under apps/$appFolderSchema but its descriptor parses as $parsingSchema; move the folder or fix the descriptor"
  }
  if ($env:APEX_PARSING_SCHEMA -cne $parsingSchema) {
    throw "publish error: schema $parsingSchema is not listed in APEX_PARSING_SCHEMA; add its connection and expected user to .env"
  }
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

if ($env:PROJECT_MULTI_SCHEMA -eq "true") {
  switch ($appEnvironment) {
    "staging" {
      if ($env:STAGING_SCHEMA -cne $parsingSchema) {
        throw "publish error: schema $parsingSchema is not listed in STAGING_SCHEMA, so it cannot be published to staging"
      }
    }
    "prod" {
      if ($env:PROD_SCHEMA -cne $parsingSchema) {
        throw "publish error: schema $parsingSchema is not listed in PROD_SCHEMA, so it cannot be published to production"
      }
    }
  }
}

if ($describe) {
  # Internal read-only interface for deployment summaries and DBA runbooks.
  Write-Output (@($appDir, $deployment.workspace.name, $parsingSchema,
    $sqlclConnection, $expectedUser, $targetEnvironment) -join "`t")
  exit 0
}

if ($appEnvironment -eq 'dev' -and [string]::IsNullOrWhiteSpace($env:APEX_WORKSPACE_USERNAME)) {
  throw 'publish error: set APEX_WORKSPACE_USERNAME to the existing Builder developer/admin login'
}
& $python.Path @($python.Prefix) $sourceValidator $repoRoot $appDir --for-import
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if ([string]::IsNullOrWhiteSpace($sqlclConnection) -or [string]::IsNullOrWhiteSpace($expectedUser)) {
  switch ($appEnvironment) {
    "dev" {
      throw "publish error: schema $parsingSchema is not listed in APEX_PARSING_SCHEMA; add its connection and expected user to .env"
    }
    "staging" {
      if ($env:PROJECT_MULTI_SCHEMA -eq "true") {
        throw "publish error: schema $parsingSchema is not listed in STAGING_SCHEMA, so it cannot be published to staging"
      }
      throw "publish error: set STAGING_SQLCL_CONNECTION and STAGING_EXPECTED_USER in .env to publish to staging"
    }
    "prod" {
      if ($env:PROJECT_MULTI_SCHEMA -eq "true") {
        throw "publish error: schema $parsingSchema is not listed in PROD_SCHEMA, so it cannot be published to production"
      }
      throw "publish error: set PROD_SQLCL_CONNECTION and PROD_EXPECTED_USER in .env to publish to production"
    }
  }
}

if ($appEnvironment -ne "dev") {
  $answer = Read-Host "Deploying to $targetLabel. Proceed? [y/N]"
  if ($answer -notmatch '^(?i:y|yes)$') { throw "Publish cancelled." }
}

# Classify the target before the live lookup opens its read-only session.
if ($appEnvironment -eq "dev") {
  & (Join-Path $PSScriptRoot "check_db_target.ps1") -Operation write -Target apex
}
if ($selectedFiles.Count -gt 0) {
  $partialArguments = @()
  foreach ($selectedFile in $selectedFiles) { $partialArguments += @('--file', $selectedFile) }
  if ($noTeamNotice) { $partialArguments += '--no-team-notice' }
  & $python.Path @($python.Prefix) (Join-Path $PSScriptRoot 'partial_publish.py') $AppId @partialArguments `
    --source-dir $appDir --repo-root $repoRoot --workspace $deployment.workspace.name `
    --schema $parsingSchema --connection $sqlclConnection --expected-user $expectedUser `
    --classification $targetEnvironment --developer $env:APEX_WORKSPACE_USERNAME --developer-name $env:DEVELOPER_NAME
  exit $LASTEXITCODE
}

# The live application must be parsed by the schema the descriptor names. An
# application that is not there yet (first import) is allowed.
if ($env:PROJECT_MULTI_SCHEMA -eq "true") {
  . (Join-Path $PSScriptRoot "invoke_sqlcl.ps1")
  $lookupDir = Join-Path $repoRoot ("scratch/apex-lookup-" + [Guid]::NewGuid().ToString("N"))
  try {
    try {
      $liveSchema = Get-AppParsingSchema -Connection $sqlclConnection -ExpectedUser $expectedUser `
        -Schema $parsingSchema -AppId $AppId -WorkDirectory $lookupDir `
        -ScriptPath (Join-Path $repoRoot "scripts/lookup_app_schema.sql") -Environment $targetEnvironment
      if ($liveSchema -cne $parsingSchema) {
        throw "publish error: application $AppId is parsed by $liveSchema, not the descriptor's $parsingSchema; refusing to import"
      }
    } catch {
      if ($_.Exception.Message -like "*was not found in the workspace*") {
        # First import: nothing to compare.
      } elseif ($_.Exception.Message -like "publish error:*") {
        throw
      } else {
        throw "publish error: could not verify the live parsing schema of application ${AppId}: $($_.Exception.Message)"
      }
    }
  } finally {
    if (Test-Path -LiteralPath $lookupDir) { Remove-Item -LiteralPath $lookupDir -Recurse -Force -ErrorAction SilentlyContinue }
  }
}

# The import session re-checks the live state the drift guard approved; '-'
# skips that re-check for -Force and for staging or production.
$expectedLiveState = "-"

. (Join-Path $PSScriptRoot "invoke_sqlcl.ps1")
$stdinFile = [System.IO.Path]::GetTempFileName()
$transcriptFile = [System.IO.Path]::GetTempFileName()
$verifyTranscriptFile = [System.IO.Path]::GetTempFileName()
$publishWorkDir = Join-Path $repoRoot ("scratch/apex-publish-" + [Guid]::NewGuid().ToString("N"))
[System.IO.Directory]::CreateDirectory($publishWorkDir) | Out-Null
$applicationSource = Join-Path $appDir "application.apx"
$unstampedSource = Join-Path $publishWorkDir "application.apx.unstamped"
$stampedSource = Join-Path $publishWorkDir "application.apx.stamped"
$stampingSource = Join-Path $publishWorkDir "application.apx.stamping"
$restoreSource = Join-Path $publishWorkDir "application.apx.restore"
$restoreUnstamped = $false
# Set once the import changed the target and cleared after its verification,
# so a failure in between can say what to do next.
$importUnverified = $false
# Set while the import session runs and cleared once its output has been read: an
# interrupt in between leaves the import's result unknown (SQLcl may have
# finished it already).
$importRunning = $false
$lockRecovery = $null
$lockHeld = $false
$importAttempted = $false
$lockArguments = @()

# .NET rather than Get-FileHash: a Windows PowerShell that inherited PowerShell 7's
# PSModulePath (anything started under pwsh passes it on) loses that cmdlet, and
# the swap below has already moved the working file aside when it needs the hash.
function Get-FileSha256([string]$Path) {
  $sha = [System.Security.Cryptography.SHA256]::Create()
  try {
    $stream = [System.IO.File]::OpenRead($Path)
    try { return [System.BitConverter]::ToString($sha.ComputeHash($stream)) } finally { $stream.Dispose() }
  } finally { $sha.Dispose() }
}

# Replace Target with Replacement only while Target still holds the bytes of
# Expected. Target is renamed aside before the comparison and the replacement
# is installed with File.Move, which never overwrites, so an editor save at any
# moment is kept rather than replaced. On Windows the file's own ACL is kept.
# Returns "swapped", "changed" (Target differs from Expected or reappeared),
# "locked" (Target could not be moved aside, for example an editor holds it
# open), or "not-installed" (the replacement could not be moved into place).
function Invoke-SwapIfUnchanged([string]$Target, [string]$Expected, [string]$Replacement, [string]$Aside) {
  $acl = $null
  if ($PSVersionTable.PSEdition -eq "Desktop" -or $IsWindows) {
    try { $acl = Get-Acl -LiteralPath $Target } catch { $acl = $null }
  }
  try { [System.IO.File]::Move($Target, $Aside) } catch { return "locked" }
  if ((Get-FileSha256 $Aside) -ne (Get-FileSha256 $Expected)) {
    if (-not (Test-Path -LiteralPath $Target)) {
      try { [System.IO.File]::Move($Aside, $Target) } catch { }
    }
    return "changed"
  }
  if (Test-Path -LiteralPath $Target) { return "changed" }
  try { [System.IO.File]::Move($Replacement, $Target) } catch {
    if (Test-Path -LiteralPath $Target) { return "changed" }
    return "not-installed"
  }
  if ($null -ne $acl) {
    try { Set-Acl -LiteralPath $Target -AclObject $acl } catch {
      Write-Warning "publish warning: could not restore the Windows permissions of ${Target}: $($_.Exception.Message)"
    }
  }
  return "swapped"
}
$publishedVersion = ""
try {
  Assert-SqlclApexVersion -WorkDirectory (Join-Path $publishWorkDir "version")
  $lockAssertScript = Join-Path $PSScriptRoot 'no_application_lock.sql'
  if ($appEnvironment -eq 'dev') {
    $lockPython = Resolve-TeamPython
    if ($null -eq $lockPython) { throw 'Python 3.10 or newer is required for application locks' }
    $lockRecovery = Join-Path $repoRoot ('.sync-state/application-locks/' + $AppId + '/publish.' + [Guid]::NewGuid().ToString('N'))
    [System.IO.Directory]::CreateDirectory($lockRecovery) | Out-Null
    $lockArguments = @('--connection', $sqlclConnection, '--expected-user', $expectedUser, '--schema', $parsingSchema,
      '--workspace', [string]$deployment.workspace.name, '--developer', $env:APEX_WORKSPACE_USERNAME, '--app-id', $AppId,
      '--classification', $targetEnvironment, '--run-dir', $lockRecovery)
    & $lockPython.Path @($lockPython.Prefix) (Join-Path $PSScriptRoot 'application_lock.py') acquire @lockArguments
    if ($LASTEXITCODE -ne 0) { throw 'publish error: native application lock acquisition failed; preserve its recovery evidence' }
    $lockHeld = $true
    $lockAssertScript = Join-Path $lockRecovery 'assert-lock.sql'
  }
if ($appEnvironment -eq "dev" -and -not $force) {
  $driftGuard = Join-Path $repoRoot "scripts/check_builder_drift.py"
  if (-not (Test-Path -LiteralPath $driftGuard -PathType Leaf)) {
    throw "publish error: Builder drift guard is missing; refusing import"
  }
  $python = Resolve-TeamPython
  if ($null -eq $python) { throw "Python 3.10 or newer is required to check Builder drift (python3, python or py -3)" }
  $approvedStateFile = [System.IO.Path]::GetTempFileName()
  try {
    & $python.Path @($python.Prefix) $driftGuard $AppId $sqlclConnection $appDir --expected-user $expectedUser --state-out $approvedStateFile --wrapper team.ps1
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $expectedLiveState = ([System.IO.File]::ReadAllText($approvedStateFile)).Trim()
  } finally {
    Remove-Item -LiteralPath $approvedStateFile -Force -ErrorAction SilentlyContinue
  }
  if ($expectedLiveState -cnotmatch '^(ABSENT|P\.([0-9T:-]+|NONE)\.[0-9A-F]*)$') {
    throw "publish error: Builder drift guard did not record the approved live state; refusing import"
  }
}

  # An import leaves no Builder timestamp, so a DEV publish stamps its own tag
  # into the application version before import. The drift guard compares that
  # version to spot a teammate's import. Staging and production import the
  # committed tag unchanged.
  if ($appEnvironment -eq "dev") {
    if (-not (Test-Path -LiteralPath $applicationSource -PathType Leaf)) {
      throw "publish error: DEV publish needs application.apx to stamp the publish tag"
    }
    # Stamp a private copy, then swap it in only if nobody saved the file
    # meanwhile; a failed stamp leaves the working file untouched.
    Copy-Item -LiteralPath $applicationSource -Destination $unstampedSource
    Copy-Item -LiteralPath $applicationSource -Destination $stampingSource
    $stampPython = Resolve-TeamPython
    if ($null -eq $stampPython) { throw "Python 3.10 or newer is required to stamp the publish tag (python3, python or py -3)" }
    $stampArgs = @((Join-Path $PSScriptRoot "stamp_publish_version.py"), $stampingSource, $env:DEVELOPER_NAME)
    $publishedVersion = & $stampPython.Path @($stampPython.Prefix) @stampArgs
    if ($LASTEXITCODE -ne 0) { throw "publish error: could not stamp the application version" }
    Copy-Item -LiteralPath $stampingSource -Destination $stampedSource
    Copy-Item -LiteralPath $unstampedSource -Destination $restoreSource
    $stampResult = Invoke-SwapIfUnchanged -Target $applicationSource -Expected $unstampedSource `
      -Replacement $stampingSource -Aside (Join-Path $publishWorkDir "application.apx.before-stamp")
    if ($stampResult -eq "locked") {
      throw "publish error: could not move application.apx to stamp the publish tag; close any program holding it open (or check the folder's permissions) and publish again"
    }
    if ($stampResult -eq "not-installed") {
      throw "publish error: could not install the stamped application.apx; nothing was imported, publish again"
    }
    if ($stampResult -ne "swapped") {
      throw "publish error: application.apx changed while the publish tag was stamped; publish again"
    }
    $restoreUnstamped = $true
    Write-Output "Stamped application version: $publishedVersion"
  }

  $applicationInput = $appDir
  $deploymentFile = Join-Path $appDir "deployments/$appEnvironment.json"
  $importRunning = $true
  $importAttempted = $true
  $sqlclExit = Invoke-Sqlcl -WorkingDirectory $publishWorkDir -StdInFile $stdinFile -Arguments @(
    "-S", "-noupdates", "-name", $sqlclConnection,
    "@$(Join-Path $PSScriptRoot 'publish_app.sql')",
    $parsingSchema, $targetEnvironment, $expectedUser,
    $applicationInput, $deploymentFile, $AppId, $expectedLiveState, $lockAssertScript
  ) -TranscriptFile $transcriptFile
  $importRunning = $false
  $sqlclOutput = [System.IO.File]::ReadAllText($transcriptFile)
  if (-not [string]::IsNullOrEmpty($sqlclOutput)) { Write-Output $sqlclOutput }
  if ($sqlclExit -ne 0) { throw "SQLcl application import failed with exit code $sqlclExit" }
  if ([regex]::IsMatch($sqlclOutput, '(?i)\b(?:SP2|TNS|ORA|PLS|SQL)-[0-9]{4,5}:')) {
    throw "SQLcl reported a client or database error during the application import"
  }
  if (-not [regex]::IsMatch($sqlclOutput, "(?m)^\s*APEX_IMPORT_VERIFIED:$AppId\s*$")) {
    throw "SQLcl did not verify the imported application; the import result is unknown"
  }
  # SQLcl exits 0 without importing when, for example, the descriptor names an
  # unknown workspace ("... is invalid"). Only its success line proves an import.
  if (-not [regex]::IsMatch($sqlclOutput, '(?m)^\s*Import successful\.\s*$')) {
    throw "SQLcl did not report a successful APEX import; see the client output above"
  }
  # The stamped source is live now; keep it for the developer to commit.
  $restoreUnstamped = $false
  $importUnverified = $true

  # Re-export the selected target and compare exact APEXlang bytes before
  # reporting success. Only a DEV publish updates the local DEV drift marker.
  $verifyRunDir = Join-Path $publishWorkDir "post-import/runs/$AppId"
  $verifyParent = Join-Path $verifyRunDir "apps/$parsingSchema"
  [System.IO.Directory]::CreateDirectory($verifyParent) | Out-Null
  $verifyExit = Invoke-Sqlcl -WorkingDirectory $verifyRunDir -StdInFile $stdinFile `
    -Arguments @(
      "-S", "-noupdates", "-name", $sqlclConnection,
      "@$(Join-Path $PSScriptRoot 'export_apps.sql')",
      $parsingSchema, $AppId, $targetEnvironment, $expectedUser
    ) -TranscriptFile $verifyTranscriptFile
  $verifyOutput = [System.IO.File]::ReadAllText($verifyTranscriptFile)
  if (-not [string]::IsNullOrEmpty($verifyOutput)) { Write-Output $verifyOutput }
  if ($verifyExit -ne 0) {
    throw "post-import APEX export failed with exit code $verifyExit; the imported source was not verified"
  }
  if ([regex]::IsMatch($verifyOutput, '(?i)\b(?:SP2|TNS|ORA|PLS|SQL)-[0-9]{4,5}:')) {
    throw "SQLcl reported an error while verifying the post-import APEX source"
  }

  $exportedDirs = @(Get-ChildItem -LiteralPath $verifyParent -Directory)
  if ($exportedDirs.Count -ne 1) {
    throw "expected exactly one post-import export for application $AppId, found $($exportedDirs.Count)"
  }
  $exportedDir = $exportedDirs[0].FullName
  if (-not (Test-Path -LiteralPath (Join-Path $exportedDir "application.apx") -PathType Leaf) -or
      -not (Test-Path -LiteralPath (Join-Path $exportedDir ".apex/apexlang.json") -PathType Leaf)) {
    throw "post-import export for application $AppId is missing required APEXlang source files"
  }
  & (Join-Path $PSScriptRoot "normalize_apx.ps1") $exportedDir

  $python = Resolve-TeamPython
  if ($null -eq $python) { throw "Python 3.10 or newer is required to verify the post-import APEX source (python3, python or py -3)" }
  $verifyScript = Join-Path $PSScriptRoot "verify_publish_state.py"
  $verifyArgs = @(
    $verifyScript, $AppId, $appDir, $exportedDir,
    (Join-Path $verifyRunDir ".apex-export-before.txt"),
    (Join-Path $verifyRunDir ".apex-export-after.txt"),
    "--repo-root", $repoRoot,
    "--deployment-file", $deploymentFile, "--deployment-state", (Join-Path $verifyRunDir ".apex-deployment-state.json")
  )
  & $python.Path @($python.Prefix) @verifyArgs
  if ($LASTEXITCODE -ne 0) {
    throw "post-import APEX source verification failed with exit code $LASTEXITCODE"
  }
  if ($appEnvironment -eq 'dev') {
    & $lockPython.Path @($lockPython.Prefix) (Join-Path $PSScriptRoot 'application_lock.py') check @lockArguments
    if ($LASTEXITCODE -ne 0) { throw 'publish error: post-import application lock verification failed; baseline retained' }
    & $lockPython.Path @($lockPython.Prefix) (Join-Path $PSScriptRoot 'application_lock.py') release @lockArguments
    if ($LASTEXITCODE -ne 0) { throw 'publish error: application unlock result unknown; baseline retained' }
    $lockHeld = $false
    $importAttempted = $false
    & $python.Path @($python.Prefix) @verifyArgs --record-baseline
    if ($LASTEXITCODE -ne 0) { throw 'publish error: could not record the verified DEV baseline' }
  }
  $importUnverified = $false
  Write-Output "Published APEX App $AppId to $targetLabel ($($deployment.workspace.name) / $parsingSchema)."
  if ($publishedVersion) {
    $relativeSource = $applicationSource.Substring($repoRoot.Length).TrimStart('\', '/')
    Write-Output "Commit the stamped version in ${relativeSource}: $publishedVersion"
  }
} finally {
  if ($lockRecovery) {
    if ($lockHeld -and -not $importAttempted) {
      & $lockPython.Path @($lockPython.Prefix) (Join-Path $PSScriptRoot 'application_lock.py') release @lockArguments
      if ($LASTEXITCODE -eq 0) { $lockHeld = $false }
    }
    if ($lockHeld -or $importAttempted) {
      Write-Warning "publish: preserve lock recovery evidence at $lockRecovery/recovery.json; inspect the live application and coordinate recovery before app-unlock."
    }
  }
  if ($importRunning) {
    if ($appEnvironment -eq "dev") {
      $stampedText = if ($publishedVersion) { $publishedVersion } else { "the version you published" }
      Write-Warning "publish: interrupted while the import was running, so its result is unknown: DEV may or may not run your source. Commit your changes, run scripts/team.ps1 export $AppId, and read the live version; $stampedText means the import completed."
    } else {
      Write-Warning "publish: interrupted while the import into $targetLabel was running, so its result is unknown; inspect $targetLabel before importing again."
    }
  }
  if ($importUnverified) {
    if ($appEnvironment -eq "dev") {
      $nextStep = "Run scripts/team.ps1 export $AppId to see what is live, reconcile, and commit."
      if ($publishedVersion) {
        $relativeStamped = $applicationSource.Substring($repoRoot.Length).TrimStart('\', '/')
        $nextStep = "Commit the stamped $relativeStamped first (it is what was imported), then run scripts/team.ps1 export $AppId to see what is live, reconcile, and commit."
      }
      Write-Warning "publish: DEV now runs the imported source, but it was not verified. $nextStep"
    } else {
      Write-Warning "publish: $targetLabel now runs the imported source, but it was not verified; compare it with the committed source before importing again."
    }
  }
  if ($restoreUnstamped -and (Test-Path -LiteralPath $applicationSource -PathType Leaf)) {
    # Undo only our own stamp. An edit saved while the publish ran is kept.
    $restoreResult = Invoke-SwapIfUnchanged -Target $applicationSource -Expected $stampedSource `
      -Replacement $restoreSource -Aside (Join-Path $publishWorkDir "application.apx.displaced")
    if ($restoreResult -eq "changed") {
      Write-Warning "publish warning: $applicationSource changed while publishing; left as is (check its version line)"
    } elseif ($restoreResult -ne "swapped") {
      Write-Warning "publish warning: could not remove the publish tag from $applicationSource ($restoreResult); restore its version line by hand"
    }
  }
  # Interrupted or failed between moving application.apx aside and installing
  # its replacement: put the moved-aside file back before scratch is deleted.
  $keepPublishWorkDir = $importAttempted
  if ($keepPublishWorkDir) { Write-Warning "publish: unverified import diagnostics retained at $publishWorkDir" }
  foreach ($aside in @((Join-Path $publishWorkDir "application.apx.before-stamp"), (Join-Path $publishWorkDir "application.apx.displaced"))) {
    if (-not (Test-Path -LiteralPath $applicationSource) -and (Test-Path -LiteralPath $aside -PathType Leaf)) {
      try { [System.IO.File]::Move($aside, $applicationSource) } catch {
        Write-Warning "publish error: could not put application.apx back; recover it from $aside"
        $keepPublishWorkDir = $true
      }
    }
  }
  Remove-Item -LiteralPath $stdinFile, $transcriptFile, $verifyTranscriptFile -Force -ErrorAction SilentlyContinue
  if (-not $keepPublishWorkDir -and (Test-Path -LiteralPath $publishWorkDir)) {
    Remove-Item -LiteralPath $publishWorkDir -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $publishWorkDir) {
      $relativeWorkDir = $publishWorkDir.Substring($repoRoot.Length).TrimStart('\', '/')
      Write-Warning "publish warning: could not remove the temporary directory $relativeWorkDir; delete it after closing whatever holds a file in it"
    }
  }
}
} finally {
  $publishEnvNow = [Environment]::GetEnvironmentVariables("Process")
  foreach ($publishEnvName in @($publishEnvNow.Keys)) {
    if (-not $publishEnvSnapshot.Contains($publishEnvName)) {
      [Environment]::SetEnvironmentVariable($publishEnvName, $null, "Process")
    }
  }
  foreach ($publishEnvName in @($publishEnvSnapshot.Keys)) {
    if ($publishEnvNow[$publishEnvName] -cne $publishEnvSnapshot[$publishEnvName]) {
      [Environment]::SetEnvironmentVariable($publishEnvName, [string]$publishEnvSnapshot[$publishEnvName], "Process")
    }
  }
}
