$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

function Test-VenvReady {
    param([Parameter(Mandatory = $true)][string]$PythonExe)
    if (-not (Test-Path $PythonExe)) {
        return $false
    }
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $PythonExe -c "import ctflow" *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $prev
    }
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    foreach ($candidate in @(
        (Join-Path $HOME '.local\bin'),
        (Join-Path $HOME '.cargo\bin'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links'),
        (Join-Path $HOME 'scoop\shims'),
        (Join-Path $HOME '.cherrystudio\bin')
    )) {
        if ($candidate -and (Test-Path (Join-Path $candidate 'uv.exe'))) {
            $env:PATH = "$candidate;$env:PATH"
            break
        }
    }
}

$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$configFile = Join-Path $PSScriptRoot 'config\config.json'
$mcpFile = Join-Path $PSScriptRoot 'extensions\mcp.json'

if (
    -not (Get-Command uv -ErrorAction SilentlyContinue) -or
    -not (Test-VenvReady -PythonExe $venvPython) -or
    -not (Test-Path $configFile) -or
    -not (Test-Path $mcpFile)
) {
    Write-Host '[环境自检] 检测到运行环境或本地配置尚未就绪，正在进入引导配置流程...' -ForegroundColor Yellow
    & (Join-Path $PSScriptRoot 'build-run.ps1') @args
    exit $LASTEXITCODE
}

& uv run --no-sync python -m ctflow @args
exit $LASTEXITCODE
