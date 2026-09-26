#Requires -Version 5.1
# Export the configured APEX application as an APEXlang mirror.
param([string] $AppId)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
. (Join-Path $PSScriptRoot "load_env.ps1") -EnvFile $env:PROJECT_ENV_FILE
. (Join-Path $PSScriptRoot "invoke_sqlcl.ps1")
& (Join-Path $PSScriptRoot "check_db_target.ps1") -Operation read -Target apex

function Invoke-PythonScript {
  param([string] $ScriptPath, [string[]] $ScriptArguments)
  $python = Get-Command python3 -ErrorAction SilentlyContinue
  if ($null -eq $python) { $python = Get-Command python -ErrorAction SilentlyContinue }
  if ($null -eq $python) { $python = Get-Command py -ErrorAction SilentlyContinue }
  if ($null -eq $python) { throw "Python 3 is required for APEX export metadata" }
  if ($python.Name -in @("py.exe", "py")) {
    & $python.Source -3 $ScriptPath @ScriptArguments
  } else {
    & $python.Source $ScriptPath @ScriptArguments
  }
  if ($LASTEXITCODE -ne 0) { throw "Python export helper failed with exit code $LASTEXITCODE" }
}

if (-not [string]::IsNullOrWhiteSpace($AppId)) {
  if ($AppId -cnotmatch '^[1-9][0-9]*$') { throw "export error: expected a positive numeric application id" }
  $appIds = @($AppId)
} else {
  $appIds = @($env:APEX_APP_ID.Split(','))
}

# Refuse any dirty destination before making the first database connection.
foreach ($appId in $appIds) {
  $destination = "apps/$($env:APEX_PARSING_SCHEMA)/$appId"
  $dirty = @(git -C $repoRoot status --porcelain --untracked-files=all -- $destination)
  if ($LASTEXITCODE -ne 0) { throw "unable to inspect Git status for mirror: $destination" }
  if (-not [string]::IsNullOrWhiteSpace(($dirty -join "`n"))) {
    throw "refusing to export over dirty mirror: $destination; commit, stash, or remove local changes first"
  }
}

$scratchPath = Join-Path $repoRoot "scratch"
# New-Item has no -LiteralPath parameter on either Windows PowerShell 5.1 or
# PowerShell 7. The .NET API is literal and has the same create-if-missing behavior.
[System.IO.Directory]::CreateDirectory($scratchPath) | Out-Null
$stagingPath = Join-Path $scratchPath ("apex-export-" + [Guid]::NewGuid().ToString("N"))
$stageParent = Join-Path $stagingPath "staged/apps/$($env:APEX_PARSING_SCHEMA)"
New-Item -ItemType Directory -Force -Path $stageParent | Out-Null

try {
  foreach ($appId in $appIds) {
    $runPath = Join-Path $stagingPath "runs/$appId"
    $runStageParent = Join-Path $runPath "apps/$($env:APEX_PARSING_SCHEMA)"
    New-Item -ItemType Directory -Force -Path $runStageParent | Out-Null

    $sqlclExit = Invoke-Sqlcl -WorkingDirectory $runPath `
      -StdInFile (Join-Path $stagingPath ".sqlcl-stdin") `
      -Arguments @(
        "-S", "-noupdates", "-name", $env:APEX_SQLCL_CONNECTION,
        "@$(Join-Path $repoRoot 'scripts/export_apps.sql')",
        $env:APEX_PARSING_SCHEMA, $appId, $env:DB_ENVIRONMENT,
        $env:APEX_EXPECTED_USER
      )
    if ($sqlclExit -ne 0) {
      throw "SQLcl export for application $appId failed with exit code $sqlclExit"
    }

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
        (Join-Path $repoRoot "apps/$($env:APEX_PARSING_SCHEMA)/$appId"),
        $appStage
      )
  }

  # Install every application in one call so a failure on the last does not
  # leave the earlier ones replaced.
  $replaceArgs = @()
  foreach ($appId in $appIds) {
    $replaceArgs += (Join-Path $stageParent $appId)
    $replaceArgs += "apps/$($env:APEX_PARSING_SCHEMA)/$appId"
  }
  & (Join-Path $PSScriptRoot "replace_mirror.ps1") @replaceArgs
} finally {
  if (Test-Path -LiteralPath $stagingPath) {
    Remove-Item -LiteralPath $stagingPath -Recurse -Force -ErrorAction Stop
  }
}
