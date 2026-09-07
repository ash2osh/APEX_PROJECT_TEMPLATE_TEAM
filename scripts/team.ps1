$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$python = Get-Command python -ErrorAction SilentlyContinue
if ($null -eq $python) { $python = Get-Command python3 -ErrorAction SilentlyContinue }
if ($null -eq $python) { $python = Get-Command py -ErrorAction SilentlyContinue }
if ($null -eq $python) {
    throw 'Python 3.10+ is required'
}
if ($python.Name -eq 'py.exe' -or $python.Name -eq 'py') {
    & $python.Source -3 (Join-Path $repoRoot 'scripts/team.py') @args
} else {
    & $python.Source (Join-Path $repoRoot 'scripts/team.py') @args
}
exit $LASTEXITCODE
