$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$env:PYTHONPATH = (Join-Path $repoRoot "scripts") + $(if ($env:PYTHONPATH) { ";$env:PYTHONPATH" } else { "" })
$python = Get-Command python -ErrorAction SilentlyContinue
if ($null -eq $python) { $python = Get-Command python3 -ErrorAction SilentlyContinue }
if ($null -eq $python) { $python = Get-Command py -ErrorAction SilentlyContinue }
if ($null -eq $python) { throw 'Python 3.10+ is required' }
if ($python.Name -eq 'py.exe' -or $python.Name -eq 'py') {
    & $python.Source -3 -m teamlib.release_adapter @args
} else {
    & $python.Source -m teamlib.release_adapter @args
}
exit $LASTEXITCODE
