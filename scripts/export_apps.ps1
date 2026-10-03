#Requires -Version 5.1
# Export the configured APEX applications as APEXlang mirrors.
param([string] $AppId)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
. (Join-Path $PSScriptRoot "load_env.ps1") -EnvFile $env:PROJECT_ENV_FILE
. (Join-Path $PSScriptRoot "invoke_sqlcl.ps1")
. (Join-Path $PSScriptRoot "resolve_python.ps1")

function Invoke-PythonScript {
  param([string] $ScriptPath, [string[]] $ScriptArguments)
  $python = Resolve-TeamPython
  if ($null -eq $python) { throw "Python 3.10 or newer is required for APEX export metadata (python3, python or py -3)" }
  & $python.Path @($python.Prefix) $ScriptPath @ScriptArguments
  if ($LASTEXITCODE -ne 0) { throw "Python export helper failed with exit code $LASTEXITCODE" }
}

function Split-ExportList([string] $Value) {
  # A function's output is unrolled, so callers must wrap the call in @( ):
  # that restores an array for zero or one entries. (Do not use `return ,@(...)`;
  # it emits the array as ONE object, and @( ) would then count it as one entry.)
  if ([string]::IsNullOrEmpty($Value)) { return }
  return $Value.Split(',')
}
$apexSchemas = @(Split-ExportList $env:APEX_PARSING_SCHEMA)
$apexConnections = @(Split-ExportList $env:APEX_SQLCL_CONNECTION)
$apexUsers = @(Split-ExportList $env:APEX_EXPECTED_USER)
if ($apexSchemas.Count -eq 0) {
  throw "export error: the APEX profile does not list schema $($env:PROJECT_SCHEMA)"
}

if (-not [string]::IsNullOrWhiteSpace($AppId)) {
  if ($AppId -cnotmatch '^[1-9][0-9]{0,17}$') { throw "export error: expected a positive numeric application id of at most 18 digits" }
  $appIds = @($AppId)
} else {
  $appIds = @($env:APEX_APP_ID.Split(','))
}

$scratchPath = Join-Path $repoRoot "scratch"
# New-Item has no -LiteralPath parameter on either Windows PowerShell 5.1 or
# PowerShell 7. The .NET API is literal and has the same create-if-missing behavior.
[System.IO.Directory]::CreateDirectory($scratchPath) | Out-Null
$stagingPath = Join-Path $scratchPath ("apex-export-" + [Guid]::NewGuid().ToString("N"))
[System.IO.Directory]::CreateDirectory($stagingPath) | Out-Null

try {
  # Which schema parses each application. With one schema configured that is
  # the schema itself; with several it is read from the live workspace, using
  # the first connection of the profile (the selected schema's, under --schema).
  if ($env:PROJECT_MULTI_SCHEMA -eq "true") {
    & (Join-Path $PSScriptRoot "check_db_target.ps1") -Operation read -Target apex -Schema $apexSchemas[0]
  }
  $appSchemaOf = @{}
  foreach ($appId in $appIds) {
    if ($env:PROJECT_MULTI_SCHEMA -eq "true") {
      try {
        $appSchema = Get-AppParsingSchema -Connection $apexConnections[0] -ExpectedUser $apexUsers[0] `
          -Schema $apexSchemas[0] -AppId $appId `
          -WorkDirectory (Join-Path $stagingPath "lookup/$appId") `
          -ScriptPath (Join-Path $repoRoot "scripts/lookup_app_schema.sql")
      } catch {
        throw "export error: could not determine the parsing schema of application ${appId}: $($_.Exception.Message)"
      }
      if (-not [string]::IsNullOrEmpty($env:PROJECT_SCHEMA) -and $appSchema -cne $env:PROJECT_SCHEMA) {
        throw "export error: application $appId is parsed by $appSchema, not $($env:PROJECT_SCHEMA)"
      }
    } else {
      $appSchema = $apexSchemas[0]
    }
    if ($apexSchemas -cnotcontains $appSchema) {
      throw "export error: application $appId is parsed by $appSchema, which is not listed in APEX_PARSING_SCHEMA ($($apexSchemas -join ' '))"
    }
    $appSchemaOf[[string]$appId] = $appSchema
    & (Join-Path $PSScriptRoot "check_db_target.ps1") -Operation read -Target apex -Schema $appSchema
  }

  # Refuse any dirty destination before opening the first export session. Git
  # warns when the schema parent does not exist on a first export; suppress
  # that diagnostic while preserving the command's failure status.
  foreach ($appId in $appIds) {
    $destination = "apps/$($appSchemaOf[[string]$appId])/$appId"
    # Windows PowerShell 5.1 turns any stderr text from a native command into a
    # terminating error under $ErrorActionPreference = "Stop", even with 2>$null,
    # so the warning would still end the export. Relax the preference for this
    # one call and judge git by its exit status.
    $previousErrorPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
      $dirty = @(git -C $repoRoot status --porcelain --untracked-files=all -- $destination 2>$null)
    } finally {
      $ErrorActionPreference = $previousErrorPreference
    }
    if ($LASTEXITCODE -ne 0) { throw "unable to inspect Git status for mirror: $destination" }
    if (-not [string]::IsNullOrWhiteSpace(($dirty -join "`n"))) {
      throw "refusing to export over dirty mirror: $destination; commit, stash, or remove local changes first"
    }
  }

  foreach ($appId in $appIds) {
    $appSchema = $appSchemaOf[[string]$appId]
    $schemaIndex = [Array]::IndexOf($apexSchemas, $appSchema)
    $appConnection = $apexConnections[$schemaIndex]
    $appUser = $apexUsers[$schemaIndex]
    $stageParent = Join-Path $stagingPath "staged/apps/$appSchema"
    New-Item -ItemType Directory -Force -Path $stageParent | Out-Null
    $runPath = Join-Path $stagingPath "runs/$appId"
    $runStageParent = Join-Path $runPath "apps/$appSchema"
    New-Item -ItemType Directory -Force -Path $runStageParent | Out-Null
    $sqlclOutput = Join-Path $runPath "sqlcl-output.log"

    $sqlclExit = Invoke-Sqlcl -WorkingDirectory $runPath `
      -StdInFile (Join-Path $stagingPath ".sqlcl-stdin") `
      -TranscriptFile $sqlclOutput `
      -Arguments @(
        "-S", "-noupdates", "-name", $appConnection,
        "@$(Join-Path $repoRoot 'scripts/export_apps.sql')",
        $appSchema, $appId, $env:DB_ENVIRONMENT, $appUser
      )
    $transcript = [System.IO.File]::ReadAllText($sqlclOutput)
    if ($sqlclExit -ne 0 -or $transcript -match '(SP2|TNS|ORA|PLS|SQL)-[0-9]{4,5}:|SQLcl Error:') {
      throw "SQLcl export for application $appId failed or reported a client or database error:`n$transcript"
    }
    Write-Output $transcript

    # SQLcl names each export directory after the application alias, which can
    # change independently of the immutable application id used by the mirror.
    $exported = @(Get-ChildItem -LiteralPath $runStageParent -Directory)
    if ($exported.Count -ne 1) {
      throw "expected exactly one exported directory for application $appId, found $($exported.Count)"
    }
    $exportedDir = $exported[0].FullName
    if (-not (Test-Path -LiteralPath (Join-Path $exportedDir "application.apx") -PathType Leaf)) {
      throw "APEX export for application $appId did not create application.apx"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $exportedDir ".apex/apexlang.json") -PathType Leaf)) {
      throw "APEX export for application $appId did not create .apex/apexlang.json"
    }

    $appStage = Join-Path $stageParent $appId
    Move-Item -LiteralPath $exportedDir -Destination $appStage
    & (Join-Path $PSScriptRoot "normalize_apx.ps1") $appStage
    Invoke-PythonScript -ScriptPath (Join-Path $PSScriptRoot "record_export_state.py") `
      -ScriptArguments @(
        $appId,
        (Join-Path $runPath ".apex-export-before.txt"),
        (Join-Path $runPath ".apex-export-after.txt"),
        (Join-Path $appStage "apex-team-export.json")
      )
    Invoke-PythonScript -ScriptPath (Join-Path $PSScriptRoot "preserve_deployments.py") `
      -ScriptArguments @(
        (Join-Path $repoRoot "apps/$appSchema/$appId"),
        $appStage
      )
  }

  # Install every application in one call so a failure on the last does not
  # leave the earlier ones replaced.
  $replaceArgs = @()
  foreach ($appId in $appIds) {
    $appSchema = $appSchemaOf[[string]$appId]
    $replaceArgs += (Join-Path $stagingPath "staged/apps/$appSchema/$appId")
    $replaceArgs += "apps/$appSchema/$appId"
  }
  & (Join-Path $PSScriptRoot "replace_mirror.ps1") @replaceArgs
} finally {
  # On Windows a file can stay locked for a moment after SQLcl ends: retry, then warn
  # rather than turn a finished export into an error.
  for ($cleanupAttempt = 0; $cleanupAttempt -lt 10 -and (Test-Path -LiteralPath $stagingPath); $cleanupAttempt++) {
    if ($cleanupAttempt -gt 0) { Start-Sleep -Milliseconds 200 }
    Remove-Item -LiteralPath $stagingPath -Recurse -Force -ErrorAction SilentlyContinue
  }
  if (Test-Path -LiteralPath $stagingPath) {
    $relativeStaging = $stagingPath.Substring($repoRoot.Length).TrimStart('\', '/')
    Write-Warning "export warning: could not remove the temporary directory $relativeStaging; delete it after closing whatever holds a file in it"
  }
}
