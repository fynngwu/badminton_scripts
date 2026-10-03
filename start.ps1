$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Python = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    Write-Host "未配置环境，请先运行 .\setup.ps1" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path "config.toml")) {
    Write-Host "缺少 config.toml，请先运行 .\setup.ps1" -ForegroundColor Red
    exit 1
}

$txt = Get-Content "config.toml" -Raw -Encoding UTF8
if ($txt -match 'YOUR_USER_ID|YOUR_CUSTOMER_ID|YOUR_NAME|YOUR_PHONE') {
    Write-Host "用户信息尚未自动配置，请重新运行 .\setup.ps1" -ForegroundColor Red
    exit 1
}

# 已经运行则只打开网页
if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) {
    Start-Process "http://127.0.0.1:8000"
    exit 0
}

# 2 秒后打开浏览器
Start-Job {
    Start-Sleep 2
    Start-Process "http://127.0.0.1:8000"
} | Out-Null

& $Python app.py
