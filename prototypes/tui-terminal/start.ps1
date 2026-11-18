param(
    [ValidateSet('harbor', 'ribbon', 'flux', 'folio', 'ledger')]
    [string]$Design = 'flux'
)

$projectPython = Join-Path $PSScriptRoot '..\..\.venv\Scripts\python.exe'
& $projectPython (Join-Path $PSScriptRoot 'app.py') --design $Design
exit $LASTEXITCODE
