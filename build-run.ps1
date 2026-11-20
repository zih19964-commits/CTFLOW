$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

function Invoke-QuietCheck {
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [string[]]$Arguments = @()
    )
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @Arguments *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $prev
    }
}

function Confirm-Download {
    param([Parameter(Mandatory = $true)][string]$Prompt)
    if ([Console]::IsInputRedirected) {
        Write-Host "$Prompt [Y/n]: "
        $answer = [Console]::In.ReadLine()
    } else {
        try {
            $answer = Read-Host "$Prompt [Y/n]"
        } catch {
            return $false
        }
    }
    if ($null -eq $answer) {
        return $false
    }
    $trimmed = $answer.Trim().ToLowerInvariant()
    return ($trimmed -eq '' -or $trimmed -eq 'y' -or $trimmed -eq 'yes' -or $trimmed -eq '是')
}

function Update-SessionPath {
    # 仅刷新当前进程 PATH，探测常见本地安装目录，不修改系统全局环境变量
    $merged = @()
    foreach ($scope in @('Process', 'User', 'Machine')) {
        $raw = [Environment]::GetEnvironmentVariable('PATH', $scope)
        if ($raw) {
            $merged += ($raw -split ';')
        }
    }
    $candidates = @(
        (Join-Path $HOME '.local\bin'),
        (Join-Path $HOME '.cargo\bin'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links'),
        (Join-Path $HOME 'scoop\shims'),
        (Join-Path $HOME '.cherrystudio\bin')
    )
    foreach ($dir in ($candidates + $merged)) {
        if ($dir -and (Test-Path $dir) -and ($merged -notcontains $dir)) {
            $merged = @($dir) + $merged
        }
    }
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    $clean = foreach ($item in $merged) {
        $trimmed = $item.Trim()
        if ($trimmed -and $seen.Add($trimmed)) {
            $trimmed
        }
    }
    $env:PATH = ($clean -join ';')
}

function Ensure-Uv {
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        return $true
    }
    Update-SessionPath
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        return $true
    }

    Write-Host ''
    Write-Host '============================================================' -ForegroundColor Yellow
    Write-Host '[环境缺失] 未检测到 uv（Python 虚拟环境与依赖管理工具）' -ForegroundColor Yellow
    Write-Host '  官方安装指南：https://docs.astral.sh/uv/getting-started/installation/' -ForegroundColor Cyan
    Write-Host '  安装包下载页：https://github.com/astral-sh/uv/releases' -ForegroundColor Cyan
    Write-Host '============================================================' -ForegroundColor Yellow

    if (-not (Confirm-Download '是否现在自动下载并安装 uv？')) {
        Write-Host '已取消自动安装。请按上方地址安装 uv 后重新运行本脚本。' -ForegroundColor Red
        return $false
    }

    Write-Host '正在通过官方安装器下载并安装 uv...' -ForegroundColor Cyan
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        Invoke-RestMethod 'https://astral.sh/uv/install.ps1' | Invoke-Expression
    } catch {
        Write-Host "官方脚本下载受阻：$($_.Exception.Message)" -ForegroundColor Yellow
    }

    Update-SessionPath
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        Write-Host 'uv 安装成功！' -ForegroundColor Green
        return $true
    }

    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host '正在尝试通过系统 winget 安装 uv...' -ForegroundColor Cyan
        & winget install --id=AstralSoftware.uv -e --accept-source-agreements --accept-package-agreements
        Update-SessionPath
        if (Get-Command uv -ErrorAction SilentlyContinue) {
            Write-Host 'uv 安装成功！' -ForegroundColor Green
            return $true
        }
    }

    Write-Host '自动安装 uv 未成功，请检查网络连接或前往以下地址手动下载安装：' -ForegroundColor Red
    Write-Host '  https://docs.astral.sh/uv/getting-started/installation/' -ForegroundColor Cyan
    return $false
}

function Ensure-Python {
    $preferred = '3.13'
    $versionFile = Join-Path $PSScriptRoot '.python-version'
    if (Test-Path $versionFile) {
        $parsed = (Get-Content $versionFile -TotalCount 1).Trim()
        if ($parsed) {
            $preferred = $parsed
        }
    }

    $venvDir = Join-Path $PSScriptRoot '.venv'
    $venvPython = Join-Path $venvDir 'Scripts\python.exe'
    if (Test-Path $venvPython) {
        if (Invoke-QuietCheck $venvPython @('-c', 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)')) {
            return 'venv'
        }
        Write-Host '[环境检测] 检测到当前 .venv 虚拟环境不可用（可能由换机拷贝导致路径失效）。' -ForegroundColor Yellow
        if (-not (Confirm-Download '是否清理失效的 .venv 并重新构建虚拟环境？')) {
            Write-Host '已取消重建虚拟环境，无法继续启动。' -ForegroundColor Red
            return $null
        }
        Remove-Item -Recurse -Force $venvDir
    }

    if (Invoke-QuietCheck 'uv' @('python', 'find', '--no-project', '--no-python-downloads', $preferred)) {
        return $preferred
    }
    if (Invoke-QuietCheck 'uv' @('python', 'find', '--no-project', '--no-python-downloads', '>=3.11')) {
        return '>=3.11'
    }

    Write-Host ''
    Write-Host '============================================================' -ForegroundColor Yellow
    Write-Host "[环境缺失] 未检测到满足要求的 Python 环境（需 Python >= 3.11，推荐 $preferred）" -ForegroundColor Yellow
    Write-Host '  Python 官方下载页：https://www.python.org/downloads/' -ForegroundColor Cyan
    Write-Host '============================================================' -ForegroundColor Yellow

    if (-not (Confirm-Download "是否现在自动下载并安装 Python $preferred？")) {
        Write-Host "已取消自动安装。可前往 https://www.python.org/downloads/ 下载安装，或执行：uv python install $preferred" -ForegroundColor Red
        return $null
    }

    Write-Host "正在通过 uv 下载并安装 Python $preferred..." -ForegroundColor Cyan
    & uv python install $preferred
    if ($LASTEXITCODE -eq 0 -and (Invoke-QuietCheck 'uv' @('python', 'find', '--no-project', '--no-python-downloads', '>=3.11'))) {
        Write-Host "Python $preferred 安装成功！" -ForegroundColor Green
        return $preferred
    }

    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "正在尝试通过系统 winget 安装 Python $preferred..." -ForegroundColor Cyan
        & winget install -e --id "Python.Python.$preferred" --scope user --accept-source-agreements --accept-package-agreements
        Update-SessionPath
        if (Invoke-QuietCheck 'uv' @('python', 'find', '--no-project', '--no-python-downloads', '>=3.11')) {
            Write-Host "Python $preferred 安装成功！" -ForegroundColor Green
            return '>=3.11'
        }
    }

    Write-Host '自动安装 Python 未成功，请前往官方地址下载安装包（安装时勾选 Add Python to PATH）：' -ForegroundColor Red
    Write-Host '  https://www.python.org/downloads/' -ForegroundColor Cyan
    return $null
}

function Sync-Dependencies {
    param([Parameter(Mandatory = $true)][string]$PythonSpec)
    $syncArgs = @('sync', '--locked', '--no-python-downloads')
    if ($PythonSpec -ne 'venv') {
        $syncArgs += @('--python', $PythonSpec)
    }

    & uv @syncArgs
    if ($LASTEXITCODE -eq 0) {
        return $true
    }

    Write-Host ''
    Write-Host '[依赖同步] 默认源下载项目依赖失败，可能受网络连接限制。' -ForegroundColor Yellow
    if (-not (Confirm-Download '是否临时切换清华大学 PyPI 镜像源重试安装？')) {
        return $false
    }

    $mirrorArgs = $syncArgs + @('--default-index', 'https://pypi.tuna.tsinghua.edu.cn/simple')
    & uv @mirrorArgs
    return ($LASTEXITCODE -eq 0)
}

$setupOnly = $false
$forwardArgs = @()
foreach ($arg in $args) {
    if ($arg -eq '--setup-only') {
        $setupOnly = $true
    } else {
        $forwardArgs += $arg
    }
}

if (-not (Ensure-Uv)) {
    exit 1
}

$pythonSpec = Ensure-Python
if (-not $pythonSpec) {
    exit 1
}

if (-not (Sync-Dependencies -PythonSpec $pythonSpec)) {
    Write-Host '项目虚拟环境与依赖同步失败，请检查网络后重试。' -ForegroundColor Red
    exit 1
}

if (-not (Test-Path 'config/config.json') -or -not (Test-Path 'extensions/mcp.json')) {
    & uv run --no-sync ctflow init
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

if ($setupOnly) {
    Write-Host '[完成] CTflow 运行环境与本地配置已全部就绪！' -ForegroundColor Green
    exit 0
}

& uv run --no-sync python -m ctflow @forwardArgs
exit $LASTEXITCODE
