$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
& (Join-Path $repoRoot 'scripts/team.ps1') deploy-app @args
exit $LASTEXITCODE
