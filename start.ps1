$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
& "$PSScriptRoot\.venv\Scripts\python.exe" -m ctflow @args
exit $LASTEXITCODE
