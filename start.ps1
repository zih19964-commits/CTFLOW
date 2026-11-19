$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
& uv run python -m ctflow @args
exit $LASTEXITCODE
