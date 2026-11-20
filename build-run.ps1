$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host '未找到 uv，请先安装：https://docs.astral.sh/uv/getting-started/installation/' -ForegroundColor Red
    exit 1
}

uv sync --locked
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

if (-not (Test-Path 'config/config.json')) {
    uv run ctflow init
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

uv run python -m ctflow @args
exit $LASTEXITCODE
