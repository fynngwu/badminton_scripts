$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$StateDir = Join-Path $Root ".setup-state"
$ClashStateDir = Join-Path $StateDir "clash"

# ============================================================
# 输出工具
# ============================================================
function Info($s) { Write-Host "[INFO] $s" -ForegroundColor Cyan }
function Ok($s)   { Write-Host "[ OK ] $s" -ForegroundColor Green }
function Warn($s) { Write-Host "[WARN] $s" -ForegroundColor Yellow }
function Fail($s) { Write-Host "[FAIL] $s" -ForegroundColor Red; exit 1 }

function Ensure-Dir([string]$Path) {
    if (-not (Test-Path $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}

# ============================================================
# 整个 setup 一开始就提权，避免 CA 阶段出现“半安装”状态
# ============================================================
function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Administrator)) {
    Write-Host "[INFO] setup 需要管理员权限（仅用于安装 Windows Root CA 等系统配置）" -ForegroundColor Cyan
    $self = $MyInvocation.MyCommand.Path
    try {
        Start-Process powershell.exe -Verb RunAs -ArgumentList (
            "-NoProfile -ExecutionPolicy Bypass -File `"$self`""
        ) | Out-Null
    }
    catch {
        Fail "管理员权限请求被取消，未执行安装"
    }
    exit
}

Ensure-Dir $StateDir

# ============================================================
# 查找 uv
# ============================================================
function Find-Uv {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "uv\uv.exe"),
        (Join-Path $env:USERPROFILE ".local\bin\uv.exe")
    )

    foreach ($p in $candidates) {
        if (Test-Path $p) { return $p }
    }
    return $null
}

# ============================================================
# 强制释放 8080
# ============================================================
function Free-Port8080 {
    $listeners = Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue
    if (-not $listeners) { return }

    $pids = @(
        $listeners |
        Select-Object -ExpandProperty OwningProcess -Unique |
        Where-Object { $_ -and $_ -ne $PID }
    )

    foreach ($pidToKill in $pids) {
        $proc = Get-Process -Id $pidToKill -ErrorAction SilentlyContinue
        if ($proc) {
            Warn "8080 被占用: PID=$pidToKill ($($proc.ProcessName))，正在结束"
        } else {
            Warn "8080 被占用: PID=$pidToKill，正在结束"
        }
        try { taskkill /F /PID $pidToKill | Out-Null }
        catch { Warn "结束 PID=$pidToKill 失败" }
    }

    $deadline = (Get-Date).AddSeconds(5)
    while ((Get-Date) -lt $deadline) {
        $still = Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue
        if (-not $still) { Ok "8080 已释放"; return }
        Start-Sleep -Milliseconds 200
    }
    Fail "8080 无法释放"
}

# ============================================================
# mitmproxy CA：只确保“当前生成的 CA”已加入 LocalMachine\Root
# 不删除其它旧 CA，卸载脚本负责按用户选择清理。
# ============================================================
function Ensure-MitmCertificate {
    param([string]$CaCert)

    if (-not (Test-Path $CaCert)) { Fail "找不到 mitmproxy CA: $CaCert" }

    try { $currentThumb = (Get-PfxCertificate $CaCert).Thumbprint.ToUpper() }
    catch { Fail "无法读取当前 mitmproxy CA" }

    Info "当前 CA Thumbprint: $currentThumb"

    $installed = Get-ChildItem Cert:\LocalMachine\Root | Where-Object {
        $_.Thumbprint.ToUpper() -eq $currentThumb
    }

    if (-not $installed) {
        Info "安装当前 mitmproxy CA 到 Windows LocalMachine Root..."
        certutil -addstore Root $CaCert | Out-Null
        if ($LASTEXITCODE -ne 0) { Fail "certutil 安装 CA 失败" }
        Set-Content -Path (Join-Path $StateDir "ca_installed_by_setup.flag") -Value "1" -Encoding ASCII
    } else {
        Ok "Windows 已信任当前 mitmproxy CA"
    }

    Set-Content -Path (Join-Path $StateDir "ca_thumbprint.txt") -Value $currentThumb -Encoding ASCII

    $verify = Get-ChildItem Cert:\LocalMachine\Root | Where-Object {
        $_.Thumbprint.ToUpper() -eq $currentThumb
    }
    if (-not $verify) { Fail "CA 安装后指纹验证失败" }
    Ok "mitmproxy CA 信任正常"
}

# ============================================================
# Clash Verge Rev 全局扩展脚本
# 默认 Windows 路径：
# %APPDATA%\io.github.clash-verge-rev.clash-verge-rev\profiles\Script.js
# 可通过环境变量 CLASH_VERGE_CONFIG_DIR 覆盖配置根目录。
# ============================================================
function Find-ClashProfilesDir {
    $candidates = @()

    if ($env:CLASH_VERGE_CONFIG_DIR) {
        $candidates += (Join-Path $env:CLASH_VERGE_CONFIG_DIR "profiles")
    }

    $candidates += (Join-Path $env:APPDATA "io.github.clash-verge-rev.clash-verge-rev\profiles")
    $candidates += (Join-Path $env:LOCALAPPDATA "io.github.clash-verge-rev.clash-verge-rev\profiles")

    foreach ($p in $candidates) {
        if (Test-Path (Split-Path -Parent $p)) { return $p }
    }

    # Clash 尚未启动过时，先使用官方默认 Roaming 路径。
    return (Join-Path $env:APPDATA "io.github.clash-verge-rev.clash-verge-rev\profiles")
}

function Configure-ClashVerge {
    $profilesDir = Find-ClashProfilesDir
    Ensure-Dir $profilesDir
    Ensure-Dir $ClashStateDir

    $scriptPath = Join-Path $profilesDir "Script.js"
    $backupPath = Join-Path $ClashStateDir "Script.js.original"
    $absentFlag = Join-Path $ClashStateDir "Script.js.original_absent.flag"
    $pathFile   = Join-Path $ClashStateDir "script_path.txt"

    # 第一次由本 setup 接管时才做原始备份；重复运行绝不覆盖原始备份。
    if (-not (Test-Path $backupPath) -and -not (Test-Path $absentFlag)) {
        if (Test-Path $scriptPath) {
            Copy-Item $scriptPath $backupPath -Force
            Ok "已备份 Clash 原始全局脚本"
        } else {
            Set-Content -Path $absentFlag -Value "1" -Encoding ASCII
            Ok "Clash 原本没有 Script.js，已记录原始状态"
        }
    }

    Set-Content -Path $pathFile -Value $scriptPath -Encoding UTF8

    $begin = "// BADMINTON_SETUP_BEGIN"
    $end   = "// BADMINTON_SETUP_END"

    $block = @'
  // BADMINTON_SETUP_BEGIN
  // 由 badminton_scripts/setup.ps1 注入；uninstall.ps1 可恢复安装前 Script.js。
  const __badmintonProxyName = "MITM-Reservation";
  const __badmintonRule = "DOMAIN,reservation.sustech.edu.cn,MITM-Reservation";

  config.proxies = Array.isArray(config.proxies) ? config.proxies : [];
  config.proxies = config.proxies.filter(
    p => !(p && p.name === __badmintonProxyName)
  );
  config.proxies.push({
    name: __badmintonProxyName,
    type: "http",
    server: "127.0.0.1",
    port: 8080,
  });

  config.rules = Array.isArray(config.rules) ? config.rules : [];
  config.rules = config.rules.filter(r => r !== __badmintonRule);
  config.rules.unshift(__badmintonRule);
  // BADMINTON_SETUP_END
'@

    $text = ""
    if (Test-Path $scriptPath) {
        $text = Get-Content $scriptPath -Raw -Encoding UTF8
    }

    if ($text -match [regex]::Escape($begin)) {
        Ok "Clash 全局扩展脚本已包含预约规则，跳过重复注入"
    }
    elseif ([string]::IsNullOrWhiteSpace($text)) {
        $newText = @"
function main(config, profileName) {
$block
  return config;
}
"@
        Set-Content -Path $scriptPath -Value $newText -Encoding UTF8
        Ok "已创建 Clash 全局扩展脚本"
    }
    else {
        # 官方/常见 Script.js 使用 function main(...)。
        # 为避免原脚本在后续逻辑中再次覆盖 rules/proxies，这里不是“插到开头”，
        # 而是把原 main 重命名，再用新的 main 包一层：先跑原脚本，再追加预约规则。
        $mainRx = [regex]'function\s+main\s*\('

        if ($mainRx.IsMatch($text)) {
            $renamed = $mainRx.Replace($text, 'function __badminton_original_main(', 1)
            $wrapper = @"

function main(config, profileName) {
  config = __badminton_original_main(config, profileName) || config;
$block
  return config;
}
"@
            $newText = $renamed + $wrapper
            Set-Content -Path $scriptPath -Value $newText -Encoding UTF8
            Ok "已包装原有 Clash main(config)：原逻辑先执行，预约规则最后追加"
        }
        else {
            Warn "现有 Script.js 不是常见 function main(...) 写法"
            Warn "为保证一键部署，将临时使用预约脚本；原文件已完整备份，可一键恢复"
            $newText = @"
function main(config, profileName) {
$block
  return config;
}
"@
            Set-Content -Path $scriptPath -Value $newText -Encoding UTF8
            Ok "已写入 Clash 预约全局脚本"
        }
    }

    # 基础文本校验，避免生成空脚本。
    $verifyText = Get-Content $scriptPath -Raw -Encoding UTF8
    if ($verifyText -notmatch 'function\s+main\s*\(' -or
        $verifyText -notmatch 'MITM-Reservation' -or
        $verifyText -notmatch 'reservation\.sustech\.edu\.cn') {
        Fail "Clash Script.js 写入后校验失败，原始备份仍保存在 $ClashStateDir"
    }

    Ok "Clash Script.js 已配置: $scriptPath"
    return $scriptPath
}

function Restart-ClashVerge {
    $procs = @(Get-Process -Name "clash-verge" -ErrorAction SilentlyContinue)
    if (-not $procs -or $procs.Count -eq 0) {
        Warn "Clash Verge 当前未运行；脚本已写好，下次启动 Clash 时会生效"
        return
    }

    $exe = $null
    foreach ($p in $procs) {
        try {
            if ($p.Path -and (Test-Path $p.Path)) { $exe = $p.Path; break }
        } catch {}
    }

    Info "重启 Clash Verge，使全局扩展脚本立即生效..."
    foreach ($p in $procs) {
        try { Stop-Process -Id $p.Id -Force -ErrorAction Stop } catch {}
    }
    Start-Sleep -Seconds 1

    if ($exe) {
        try {
            Start-Process -FilePath $exe | Out-Null
            Start-Sleep -Seconds 2
            Ok "Clash Verge 已重启"
        }
        catch {
            Warn "自动重启 Clash Verge 失败，请手动启动一次 Clash Verge"
        }
    } else {
        Warn "无法取得 clash-verge.exe 路径，请手动重启 Clash Verge"
    }
}

# ============================================================
# START
# ============================================================
Write-Host ""
Write-Host "========================================" -ForegroundColor DarkCyan
Write-Host "      Badminton Setup v2 (reversible)" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor DarkCyan
Write-Host ""

# 1. 检查仓库文件
$requiredFiles = @(
    "app.py", "engine.py", "utils.py", "mitm_addon.py", "index.html",
    "fast_db.npz", "requirements.txt", "config.example.toml"
)
foreach ($f in $requiredFiles) {
    if (-not (Test-Path (Join-Path $Root $f))) { Fail "缺少文件: $f" }
}
Ok "仓库文件完整"

# 2. uv
$Uv = Find-Uv
if ($Uv) {
    Ok "检测到已有 uv: $Uv"
}
else {
    Info "未检测到 uv，使用 Astral 官方安装器"
    $UvDir = Join-Path $env:LOCALAPPDATA "uv"
    $env:UV_INSTALL_DIR = $UvDir
    $env:UV_NO_MODIFY_PATH = "1"
    try { irm https://astral.sh/uv/install.ps1 | iex }
    catch { Fail "uv 官方安装器执行失败" }

    $Uv = Find-Uv
    if (-not $Uv) { Fail "uv 安装完成，但找不到 uv.exe" }
    Set-Content -Path (Join-Path $StateDir "uv_installed_by_setup.flag") -Value "1" -Encoding ASCII
}
Set-Content -Path (Join-Path $StateDir "uv_path.txt") -Value $Uv -Encoding UTF8
Ok (& $Uv --version)

# 3. Python 3.11 + .venv
# 先对 uv-managed Python 目录做快照，只有 setup 真正新增了 3.11 时才记录为“可卸载”。
$UvPythonDir = (& $Uv python dir | Out-String).Trim()
$beforeManaged311 = @()
if ($UvPythonDir -and (Test-Path $UvPythonDir)) {
    $beforeManaged311 = @(
        Get-ChildItem $UvPythonDir -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '3\.11' } |
        Select-Object -ExpandProperty FullName
    )
}

Info "确保 uv 可用 Python 3.11..."
& $Uv python install 3.11
if ($LASTEXITCODE -ne 0) { Fail "安装/检测 Python 3.11 失败" }

$afterManaged311 = @()
if ($UvPythonDir -and (Test-Path $UvPythonDir)) {
    $afterManaged311 = @(
        Get-ChildItem $UvPythonDir -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '3\.11' } |
        Select-Object -ExpandProperty FullName
    )
}
if ($afterManaged311.Count -gt $beforeManaged311.Count) {
    Set-Content -Path (Join-Path $StateDir "python311_installed_by_setup.flag") -Value "1" -Encoding ASCII
}

$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Info "创建 Python 3.11 .venv..."
    & $Uv venv --python 3.11 .venv
    if ($LASTEXITCODE -ne 0) { Fail "创建 .venv 失败" }
    Set-Content -Path (Join-Path $StateDir "venv_created_by_setup.flag") -Value "1" -Encoding ASCII
} else {
    Ok "检测到已有 .venv"
}

# 4. 安装依赖
Info "安装/同步依赖..."
& $Uv pip install --python $Python -r requirements.txt
if ($LASTEXITCODE -ne 0) { Fail "依赖安装失败" }

& $Python -c @"
import fastapi
import cv2
import httpx
import mitmproxy
import win32gui
import numpy
import PIL
"@
if ($LASTEXITCODE -ne 0) { Fail "Python 依赖检查失败" }
Ok "Python 环境完成"

# 5. config.toml
$Config = Join-Path $Root "config.toml"
if (-not (Test-Path $Config)) {
    Copy-Item "config.example.toml" $Config
    Set-Content -Path (Join-Path $StateDir "config_created_by_setup.flag") -Value "1" -Encoding ASCII
    Ok "已创建 config.toml"
} else {
    Ok "保留已有 config.toml"
}

# 6. 生成 mitmproxy CA（用 18080，不占正式 8080）
$Mitmdump = Join-Path $Root ".venv\Scripts\mitmdump.exe"
if (-not (Test-Path $Mitmdump)) { Fail "找不到 mitmdump.exe" }

$CaCert = Join-Path $env:USERPROFILE ".mitmproxy\mitmproxy-ca-cert.cer"
if (-not (Test-Path $CaCert)) {
    Info "首次启动 mitmproxy，生成 CA..."
    $CaProcess = Start-Process $Mitmdump -ArgumentList @(
        "--listen-host", "127.0.0.1", "--listen-port", "18080"
    ) -PassThru

    $deadline = (Get-Date).AddSeconds(12)
    while ((Get-Date) -lt $deadline -and -not (Test-Path $CaCert)) {
        if ($CaProcess.HasExited) { Fail "mitmdump 在生成 CA 时提前退出" }
        Start-Sleep -Milliseconds 250
    }
    if (-not $CaProcess.HasExited) {
        Stop-Process -Id $CaProcess.Id -Force -ErrorAction SilentlyContinue
    }
    if (-not (Test-Path $CaCert)) { Fail "mitmproxy CA 生成失败" }
    Set-Content -Path (Join-Path $StateDir "mitm_home_created_by_setup.flag") -Value "1" -Encoding ASCII
}
Ok "mitmproxy CA 已生成"

# 7. 信任当前 CA
Ensure-MitmCertificate -CaCert $CaCert

# 8. 一键配置 Clash Verge 全局扩展脚本并重启 GUI
$ClashScript = Configure-ClashVerge
Restart-ClashVerge

# 9. 正式启动临时 bootstrap mitmproxy:8080
Free-Port8080
Info "启动 bootstrap mitmproxy: 127.0.0.1:8080"
$Mitm = Start-Process $Mitmdump -ArgumentList @(
    "-q", "--listen-host", "127.0.0.1", "--listen-port", "8080",
    "-s", (Join-Path $Root "mitm_addon.py")
) -PassThru

$listenDeadline = (Get-Date).AddSeconds(8)
$ready = $false
while ((Get-Date) -lt $listenDeadline) {
    $listener = Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue
    if ($listener) { $ready = $true; break }
    if ($Mitm.HasExited) { break }
    Start-Sleep -Milliseconds 200
}
if (-not $ready) { Fail "mitmproxy 未能监听 8080" }
Ok "8080 正在监听；Clash 已自动指向 MITM-Reservation"

# 10. 自动获取用户资料
Info "开始自动抓取用户资料"
Write-Host ""
Write-Host "请确保：" -ForegroundColor Cyan
Write-Host "  1. Clash Verge 已打开（setup 已自动写入全局脚本）"
Write-Host "  2. 企业微信已在 CA 安装后完全重启"
Write-Host "  3. 已进入预约页面"
Write-Host ""
Write-Host "无需手动刷新，脚本会自动 Ctrl+R。"
Write-Host ""

$deadline = (Get-Date).AddSeconds(90)
$nextRefresh = Get-Date
$done = $false

while ((Get-Date) -lt $deadline) {
    $txt = Get-Content $Config -Raw -Encoding UTF8
    if ($txt -notmatch 'YOUR_USER_ID|YOUR_CUSTOMER_ID|YOUR_NAME|YOUR_PHONE') {
        $done = $true
        break
    }

    if ((Get-Date) -ge $nextRefresh) {
        Info "尝试自动刷新预约窗口..."
        & $Python -c @"
from utils import refresh_window
refresh_window()
"@
        $nextRefresh = (Get-Date).AddSeconds(5)
    }

    if ($Mitm.HasExited) { Warn "mitmdump 已退出"; break }
    Start-Sleep -Milliseconds 300
}

# 11. 结束临时 bootstrap mitm；app.py 启动后 engine 自行管理 mitmdump
if (-not $Mitm.HasExited) {
    Stop-Process -Id $Mitm.Id -Force -ErrorAction SilentlyContinue
}

Write-Host ""
if ($done) {
    Ok "用户资料和 Token 已自动写入 config.toml"
    Write-Host ""
    Write-Host "部署完成。" -ForegroundColor Green
    Write-Host "Clash 原始全局脚本备份：$ClashStateDir" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "以后运行：" -ForegroundColor Cyan
    Write-Host "    .\start.ps1" -ForegroundColor White
    Write-Host ""
    Write-Host "需要卸载/回滚时：" -ForegroundColor Cyan
    Write-Host "    先编辑 .\uninstall.config.ps1 的 0/1 开关" -ForegroundColor White
    Write-Host "    再运行 .\uninstall.ps1" -ForegroundColor White
} else {
    Warn "90 秒内没有抓到用户资料"
    Write-Host ""
    Write-Host "Clash 脚本已经自动配置；请重点检查：" -ForegroundColor Yellow
    Write-Host "  企业微信是否在 CA 安装后彻底重启"
    Write-Host "  预约页面是否已经打开"
    Write-Host "  Clash Verge 是否正在运行并已加载最新全局脚本"
    Write-Host ""
}
