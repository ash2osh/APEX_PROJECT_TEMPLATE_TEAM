$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$python = Get-Command python -ErrorAction SilentlyContinue
if ($null -eq $python) {
    throw 'Python 3.10+ is required'
}
& $python.Source (Join-Path $repoRoot 'scripts/team.py') @args
exit $LASTEXITCODE
