$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$env:PYTHONPATH = (Join-Path $repoRoot "scripts") + $(if ($env:PYTHONPATH) { ";$env:PYTHONPATH" } else { "" })
& python -m teamlib.release_adapter @args
exit $LASTEXITCODE
