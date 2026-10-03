$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root


# ============================================================
# 输出工具
# ============================================================

function Info($s) {
    Write-Host "[INFO] $s" -ForegroundColor Cyan
}

function Ok($s) {
    Write-Host "[ OK ] $s" -ForegroundColor Green
}

function Warn($s) {
    Write-Host "[WARN] $s" -ForegroundColor Yellow
}

function Fail($s) {
    Write-Host "[FAIL] $s" -ForegroundColor Red
    exit 1
}


# ============================================================
# 查找 uv
# ============================================================

function Find-Uv {
    # 1. PATH
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }

    # 2. 本项目安装器使用的位置
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "uv\uv.exe"),
        (Join-Path $env:USERPROFILE ".local\bin\uv.exe")
    )

    foreach ($p in $candidates) {
        if (Test-Path $p) {
            return $p
        }
    }

    return $null
}


# ============================================================
# 强制释放 8080
# ============================================================

function Free-Port8080 {
    $listeners = Get-NetTCPConnection `
        -LocalPort 8080 `
        -State Listen `
        -ErrorAction SilentlyContinue

    if (-not $listeners) {
        return
    }

    $pids = @(
        $listeners |
        Select-Object -ExpandProperty OwningProcess -Unique |
        Where-Object {
            $_ -and $_ -ne $PID
        }
    )

    foreach ($pidToKill in $pids) {

        $proc = Get-Process `
            -Id $pidToKill `
            -ErrorAction SilentlyContinue

        if ($proc) {
            Warn "8080 被占用: PID=$pidToKill ($($proc.ProcessName))，正在结束"
        }
        else {
            Warn "8080 被占用: PID=$pidToKill，正在结束"
        }

        try {
            taskkill /F /PID $pidToKill | Out-Null
        }
        catch {
            Warn "结束 PID=$pidToKill 失败"
        }
    }

    $deadline = (Get-Date).AddSeconds(5)

    while ((Get-Date) -lt $deadline) {

        $still = Get-NetTCPConnection `
            -LocalPort 8080 `
            -State Listen `
            -ErrorAction SilentlyContinue

        if (-not $still) {
            Ok "8080 已释放"
            return
        }

        Start-Sleep -Milliseconds 200
    }

    Fail "8080 无法释放"
}


# ============================================================
# 确保当前 mitmproxy CA 被 Windows 正确信任
# ============================================================

function Ensure-MitmCertificate {

    param (
        [string]$CaCert
    )

    if (-not (Test-Path $CaCert)) {
        Fail "找不到 mitmproxy CA: $CaCert"
    }

    Info "检查 mitmproxy CA 指纹..."

    try {
        $currentThumb = (Get-PfxCertificate $CaCert).Thumbprint.ToUpper()
    }
    catch {
        Fail "无法读取当前 mitmproxy CA"
    }

    Info "当前 CA Thumbprint: $currentThumb"

    $installed = Get-ChildItem Cert:\LocalMachine\Root |
        Where-Object {
            $_.Thumbprint.ToUpper() -eq $currentThumb
        }

    if ($installed) {
        Ok "Windows 已信任当前 mitmproxy CA"
        return
    }

    Info "当前 mitmproxy CA 尚未被 Windows 信任"

    $escapedCaCert = $CaCert.Replace("'", "''")

    $script = @"
`$ErrorActionPreference = 'Stop'

Get-ChildItem Cert:\LocalMachine\Root |
Where-Object { `$_.Subject -like '*mitmproxy*' } |
Remove-Item -Force

certutil -addstore Root '$escapedCaCert'

if (`$LASTEXITCODE -ne 0) {
    throw 'certutil 安装 CA 失败'
}
"@

    $tempScript = Join-Path $env:TEMP "install_mitm_ca.ps1"

    Set-Content `
        -Path $tempScript `
        -Value $script `
        -Encoding UTF8

    try {
        $p = Start-Process powershell `
            -Verb RunAs `
            -ArgumentList @(
                "-NoProfile",
                "-ExecutionPolicy", "Bypass",
                "-File", "`"$tempScript`""
            ) `
            -Wait `
            -PassThru
    }
    catch {
        Remove-Item $tempScript -Force -ErrorAction SilentlyContinue
        Fail "无法以管理员权限安装 mitmproxy CA"
    }

    Remove-Item $tempScript -Force -ErrorAction SilentlyContinue

    if ($p.ExitCode -ne 0) {
        Fail "mitmproxy CA 安装失败"
    }

    Start-Sleep -Milliseconds 500

    $verify = Get-ChildItem Cert:\LocalMachine\Root |
        Where-Object {
            $_.Thumbprint.ToUpper() -eq $currentThumb
        }

    if (-not $verify) {
        Fail "CA 安装完成后指纹验证仍失败"
    }

    Ok "Windows 已安装并信任当前 mitmproxy CA"
}


# ============================================================
# START
# ============================================================

Write-Host ""
Write-Host "========================================" -ForegroundColor DarkCyan
Write-Host "           Badminton Setup" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor DarkCyan
Write-Host ""


# ============================================================
# 1. 检查仓库文件
# ============================================================

$requiredFiles = @(
    "app.py",
    "engine.py",
    "utils.py",
    "mitm_addon.py",
    "index.html",
    "fast_db.npz",
    "requirements.txt",
    "config.example.toml"
)

foreach ($f in $requiredFiles) {

    if (-not (Test-Path (Join-Path $Root $f))) {
        Fail "缺少文件: $f"
    }
}

Ok "仓库文件完整"


# ============================================================
# 2. uv
# ============================================================

$Uv = Find-Uv

if ($Uv) {

    Ok "检测到已有 uv:"
    Write-Host "    $Uv"
}
else {

    Info "未检测到 uv"
    Info "使用 Astral 官方安装器安装"

    $UvDir = Join-Path $env:LOCALAPPDATA "uv"

    $env:UV_INSTALL_DIR = $UvDir
    $env:UV_NO_MODIFY_PATH = "1"

    try {

        irm https://astral.sh/uv/install.ps1 | iex
    }
    catch {

        Fail "uv 官方安装器执行失败"
    }

    $Uv = Find-Uv

    if (-not $Uv) {
        Fail "uv 安装完成，但找不到 uv.exe"
    }
}

Ok (& $Uv --version)


# ============================================================
# 3. Python 3.11 + .venv
# ============================================================

$Python = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {

    Info "创建 Python 3.11 环境..."

    & $Uv venv `
        --python 3.11 `
        .venv

    if ($LASTEXITCODE -ne 0) {
        Fail "创建 .venv 失败"
    }
}
else {

    Ok "检测到已有 .venv"
}


# ============================================================
# 4. 安装依赖
# ============================================================

Info "安装/同步依赖..."

& $Uv pip install `
    --python $Python `
    -r requirements.txt

if ($LASTEXITCODE -ne 0) {
    Fail "依赖安装失败"
}


# 导入检查

& $Python -c @"
import fastapi
import cv2
import httpx
import mitmproxy
import win32gui
import numpy
import PIL
"@

if ($LASTEXITCODE -ne 0) {
    Fail "Python 依赖检查失败"
}

Ok "Python 环境完成"


# ============================================================
# 5. config.toml
# ============================================================

$Config = Join-Path $Root "config.toml"

if (-not (Test-Path $Config)) {

    Copy-Item `
        "config.example.toml" `
        $Config

    Ok "已创建 config.toml"
}
else {

    Ok "保留已有 config.toml"
}


# ============================================================
# 6. 生成 mitmproxy CA
#
# 注意：
# 这里使用 18080
# 不占 Clash 正式使用的 8080
# ============================================================

$Mitmdump = Join-Path `
    $Root `
    ".venv\Scripts\mitmdump.exe"

if (-not (Test-Path $Mitmdump)) {
    Fail "找不到 mitmdump.exe"
}

$CaCert = Join-Path `
    $env:USERPROFILE `
    ".mitmproxy\mitmproxy-ca-cert.cer"


if (-not (Test-Path $CaCert)) {

    Info "首次启动 mitmproxy，生成 CA..."

    $CaProcess = Start-Process `
        $Mitmdump `
        -ArgumentList @(
            "--listen-host",
            "127.0.0.1",
            "--listen-port",
            "18080"
        ) `
        -PassThru


    $deadline = (Get-Date).AddSeconds(12)

    while (
        (Get-Date) -lt $deadline -and
        -not (Test-Path $CaCert)
    ) {

        if ($CaProcess.HasExited) {
            Fail "mitmdump 在生成 CA 时提前退出"
        }

        Start-Sleep -Milliseconds 250
    }


    if (-not $CaProcess.HasExited) {

        Stop-Process `
            -Id $CaProcess.Id `
            -Force `
            -ErrorAction SilentlyContinue
    }


    if (-not (Test-Path $CaCert)) {
        Fail "mitmproxy CA 生成失败"
    }
}

Ok "mitmproxy CA 已生成"


# ============================================================
# 7. 校验证书指纹并自动修复
# ============================================================

Ensure-MitmCertificate `
    -CaCert $CaCert


# ============================================================
# 8. 正式启动 8080 mitmproxy
# ============================================================

Free-Port8080

Info "启动 mitmproxy: 127.0.0.1:8080"


$Mitm = Start-Process `
    $Mitmdump `
    -ArgumentList @(
        "-q",
        "--listen-host",
        "127.0.0.1",
        "--listen-port",
        "8080",
        "-s",
        (Join-Path $Root "mitm_addon.py")
    ) `
    -PassThru


# ============================================================
# 9. 确认 8080 已监听
# ============================================================

$listenDeadline = (Get-Date).AddSeconds(8)

$ready = $false


while ((Get-Date) -lt $listenDeadline) {

    $listener = Get-NetTCPConnection `
        -LocalPort 8080 `
        -State Listen `
        -ErrorAction SilentlyContinue

    if ($listener) {

        $ready = $true
        break
    }


    if ($Mitm.HasExited) {

        break
    }


    Start-Sleep -Milliseconds 200
}


if (-not $ready) {

    Fail "mitmproxy 未能监听 8080"
}


Ok "8080 正在监听"

Write-Host ""
Write-Host "Clash → 127.0.0.1:8080 已有 mitmproxy 接收" `
    -ForegroundColor Green


# ============================================================
# 10. 自动获取用户资料
# ============================================================

Info "开始自动抓取用户资料"

Write-Host ""
Write-Host "请确保：" -ForegroundColor Cyan
Write-Host "  1. Clash 已开启"
Write-Host "  2. 企业微信已完全重启"
Write-Host "  3. 已进入预约页面"
Write-Host ""
Write-Host "无需手动刷新，脚本会自动 Ctrl+R。"
Write-Host ""


$deadline = (Get-Date).AddSeconds(90)

$nextRefresh = Get-Date

$done = $false


while ((Get-Date) -lt $deadline) {


    # --------------------------
    # 判断 config 是否已填好
    # --------------------------

    $txt = Get-Content `
        $Config `
        -Raw `
        -Encoding UTF8


    if (
        $txt -notmatch `
        'YOUR_USER_ID|YOUR_CUSTOMER_ID|YOUR_NAME|YOUR_PHONE'
    ) {

        $done = $true
        break
    }


    # --------------------------
    # 每 5 秒自动刷新企业微信
    # --------------------------

    if ((Get-Date) -ge $nextRefresh) {

        Info "尝试自动刷新预约窗口..."

        & $Python -c @"
from utils import refresh_window
refresh_window()
"@

        $nextRefresh = (Get-Date).AddSeconds(5)
    }


    if ($Mitm.HasExited) {

        Warn "mitmdump 已退出"
        break
    }


    Start-Sleep -Milliseconds 300
}


# ============================================================
# 11. 结束临时 bootstrap mitm
#
# app.py 正式启动后 engine 会自行管理 mitmdump
# ============================================================

if (-not $Mitm.HasExited) {

    Stop-Process `
        -Id $Mitm.Id `
        -Force `
        -ErrorAction SilentlyContinue
}


# ============================================================
# DONE
# ============================================================

Write-Host ""

if ($done) {

    Ok "用户资料和 Token 已自动写入 config.toml"

    Write-Host ""
    Write-Host "部署完成。" -ForegroundColor Green
    Write-Host ""
    Write-Host "以后运行：" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "    .\start.ps1" -ForegroundColor White
    Write-Host ""
}
else {

    Warn "90 秒内没有抓到用户资料"

    Write-Host ""
    Write-Host "请检查：" -ForegroundColor Yellow
    Write-Host "  Clash 路由"
    Write-Host "  mitmproxy CA"
    Write-Host "  企业微信是否彻底重启"
    Write-Host "  预约页面是否已经打开"
    Write-Host ""
}