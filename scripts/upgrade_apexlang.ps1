#Requires -Version 5.1
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
$ErrorActionPreference = 'Stop'
try {
  if ($Arguments.Count -lt 1 -or $Arguments[0] -cnotmatch '^[1-9][0-9]{0,17}$') { throw 'expected a positive numeric application id' }
  $appId = $Arguments[0]
  $mode = ''
  for ($index = 1; $index -lt $Arguments.Count; $index++) {
    switch -CaseSensitive ($Arguments[$index]) {
      '--env' {
        if ($index + 1 -ge $Arguments.Count -or $Arguments[$index + 1] -cne 'dev') { throw 'source conversion targets DEV only' }
        $index++
      }
      '--mode' {
        if ($index + 1 -ge $Arguments.Count -or $mode) { throw '--mode requires builder or files once' }
        $mode = $Arguments[++$index]
      }
      default { throw "unknown option: $($Arguments[$index])" }
    }
  }
  if ($mode -cnotin @('builder', 'files')) { throw '--mode must be builder or files' }
  $repoRoot = Split-Path -Parent $PSScriptRoot
  $environmentFile = if ($env:PROJECT_ENV_FILE) { $env:PROJECT_ENV_FILE } else { Join-Path $repoRoot '.env' }
  . (Join-Path $PSScriptRoot 'load_env.ps1') -EnvFile $environmentFile
  . (Join-Path $PSScriptRoot 'invoke_sqlcl.ps1')
  . (Join-Path $PSScriptRoot 'resolve_python.ps1')
  $description = & (Join-Path $PSScriptRoot 'publish_app.ps1') $appId --env dev --describe
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  $target = @(([string]$description).Split("`t"))
  if ($target.Count -ne 6) { throw 'could not resolve the selected DEV descriptor' }
  $python = Resolve-TeamPython
  if ($null -eq $python) { throw 'Python 3.10 or newer is required' }
  & $python.Path @($python.Prefix) (Join-Path $PSScriptRoot 'upgrade_apexlang.py') $appId --mode $mode `
    --source-dir $target[0] --repo-root $repoRoot --workspace $target[1] --schema $target[2] `
    --connection $target[3] --expected-user $target[4] --classification $target[5] `
    "--developer=$($env:APEX_WORKSPACE_USERNAME)"
  exit $LASTEXITCODE
} catch {
  [Console]::Error.WriteLine("source upgrade error: $($_.Exception.Message)")
  exit 2
}
