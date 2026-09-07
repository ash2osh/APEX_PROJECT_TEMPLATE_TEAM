$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
& (Join-Path $RepoRoot "scripts\team.ps1") build-release @args
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
