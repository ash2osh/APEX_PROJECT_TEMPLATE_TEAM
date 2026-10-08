#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
$operation = if ($args.Count -gt 0) { [string]$args[0] } else { '' }
$appId = if ($args.Count -gt 1) { [string]$args[1] } else { '' }
if ($operation -cnotin @('acquire', 'unlock')) { throw 'application lock error: expected acquire or unlock' }
if ($appId -cnotmatch '^[1-9][0-9]{0,17}$') { throw 'application lock error: expected a positive numeric application id of at most 18 digits' }
$comment = ''
for ($index=2; $index -lt $args.Count; $index++) {
  switch -CaseSensitive ([string]$args[$index]) {
    '--env' {
      if ($index+1 -ge $args.Count) { throw 'application lock error: --env requires dev' }
      $index++
      if ([string]$args[$index] -cne 'dev') { throw 'application lock error: application locks target DEV only' }
    }
    '--comment' {
      if ($operation -cne 'acquire') { throw 'application lock error: --comment is supported only by app-lock' }
      if ($index+1 -ge $args.Count) { throw 'application lock error: --comment requires text' }
      $index++; $comment = [string]$args[$index]
    }
    default { throw "application lock error: unknown option: $($args[$index])" }
  }
}
$originalEnvironment = [Environment]::GetEnvironmentVariables('Process')
try {
  $repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
  $envFile = if ($env:PROJECT_ENV_FILE) { $env:PROJECT_ENV_FILE } else { Join-Path $repoRoot '.env' }
  . (Join-Path $PSScriptRoot 'load_env.ps1') -EnvFile $envFile
  if ([string]::IsNullOrWhiteSpace($env:APEX_WORKSPACE_USERNAME)) { throw 'application lock error: set APEX_WORKSPACE_USERNAME to the existing Builder developer/admin login' }
  & (Join-Path $PSScriptRoot 'check_db_target.ps1') -Operation write -Target apex
  $description = ([string](& (Join-Path $PSScriptRoot 'publish_app.ps1') $appId --env dev --describe)).Split("`t")
  if ($description.Count -ne 6) { throw 'application lock error: could not resolve the application DEV descriptor' }
  . (Join-Path $PSScriptRoot 'resolve_python.ps1')
  $python = Resolve-TeamPython
  if ($null -eq $python) { throw 'Python 3.10 or newer is required for application locks' }
  $recovery = Join-Path $repoRoot ('.sync-state/application-locks/' + $appId + '/run.' + [Guid]::NewGuid().ToString('N'))
  [System.IO.Directory]::CreateDirectory($recovery) | Out-Null
  & $python.Path @($python.Prefix) (Join-Path $PSScriptRoot 'application_lock.py') $operation --app-id $appId `
    --workspace $description[1] --schema $description[2] --connection $description[3] `
    --expected-user $description[4] --classification $description[5] `
    --developer $env:APEX_WORKSPACE_USERNAME --run-dir $recovery --comment $comment
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
  $now = [Environment]::GetEnvironmentVariables('Process')
  foreach ($name in @($now.Keys)) {
    if (-not $originalEnvironment.Contains($name)) { [Environment]::SetEnvironmentVariable($name, $null, 'Process') }
  }
  foreach ($name in @($originalEnvironment.Keys)) {
    [Environment]::SetEnvironmentVariable($name, [string]$originalEnvironment[$name], 'Process')
  }
}
