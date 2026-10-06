$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$StateDir = Join-Path $Root ".setup-state"
$ClashStateDir = Join-Path $StateDir "clash"
$Cfg = Join-Path $Root "uninstall.config.ps1"

function Info($s) { Write-Host "[INFO] $s" -ForegroundColor Cyan }
function Ok($s)   { Write-Host "[ OK ] $s" -ForegroundColor Green }
function Warn($s) { Write-Host "[WARN] $s" -ForegroundColor Yellow }
function Fail($s) { Write-Host "[FAIL] $s" -ForegroundColor Red; exit 1 }

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Path $Cfg)) { Fail "缺少 uninstall.config.ps1" }
. $Cfg

$expected = @(
    "RemoveVenv", "RemoveUv", "RemoveMitmProxyCA",
    "RestoreClashGlobalScript", "RemoveConfigToml"
)
foreach ($name in $expected) {
    $v = Get-Variable -Name $name -ValueOnly -ErrorAction SilentlyContinue
    if ($v -notin @(0, 1)) { Fail "$name 必须是 0 或 1" }
}

# CA 修改需要管理员权限。为减少分支，整个卸载一次性提权。
if (-not (Test-Administrator)) {
    Write-Host "[INFO] uninstall 需要管理员权限（用于 CA 清理等）" -ForegroundColor Cyan
    $self = $MyInvocation.MyCommand.Path
    try {
        Start-Process powershell.exe -Verb RunAs -ArgumentList (
            "-NoProfile -ExecutionPolicy Bypass -File `"$self`""
        ) | Out-Null
    }
    catch {
        Fail "管理员权限请求被取消，未执行卸载"
    }
    exit
}

function Remove-StateFile([string]$name) {
    $p = Join-Path $StateDir $name
    Remove-Item $p -Force -ErrorAction SilentlyContinue
}

function Find-Uv {
    $statePath = Join-Path $StateDir "uv_path.txt"
    if (Test-Path $statePath) {
        $p = (Get-Content $statePath -Raw -Encoding UTF8).Trim()
        if ($p -and (Test-Path $p)) { return $p }
    }

    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    foreach ($p in @(
        (Join-Path $env:LOCALAPPDATA "uv\uv.exe"),
        (Join-Path $env:USERPROFILE ".local\bin\uv.exe")
    )) {
        if (Test-Path $p) { return $p }
    }
    return $null
}

function Stop-ProjectMitm {
    $venvPrefix = (Join-Path $Root ".venv\Scripts").ToLowerInvariant()
    $addonPath = (Join-Path $Root "mitm_addon.py").ToLowerInvariant()

    # 先看 8080 监听者，只结束明确属于本项目的 mitm 进程。
    $listeners = Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue
    foreach ($l in @($listeners)) {
        $pidToCheck = $l.OwningProcess
        if (-not $pidToCheck) { continue }
        try {
            $p = Get-CimInstance Win32_Process -Filter "ProcessId=$pidToCheck"
            $exe = [string]$p.ExecutablePath
            $cmd = [string]$p.CommandLine
            $belongs = ($exe.ToLowerInvariant().StartsWith($venvPrefix)) -or
                       ($cmd.ToLowerInvariant().Contains($addonPath))
            if ($belongs) {
                Info "结束项目 mitm 进程 PID=$pidToCheck"
                Stop-Process -Id $pidToCheck -Force -ErrorAction SilentlyContinue
            }
        } catch {}
    }

    # 再兜底查找命令行中含本项目 mitm_addon.py 的残留进程。
    try {
        $all = Get-CimInstance Win32_Process | Where-Object {
            $_.CommandLine -and $_.CommandLine.ToLowerInvariant().Contains($addonPath)
        }
        foreach ($p in @($all)) {
            if ($p.ProcessId -ne $PID) {
                Info "结束残留 mitm 进程 PID=$($p.ProcessId)"
                Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
            }
        }
    } catch {}

    Start-Sleep -Milliseconds 400
}

function Restart-ClashVerge {
    $procs = @(Get-Process -Name "clash-verge" -ErrorAction SilentlyContinue)
    if (-not $procs -or $procs.Count -eq 0) { return }

    $exe = $null
    foreach ($p in $procs) {
        try {
            if ($p.Path -and (Test-Path $p.Path)) { $exe = $p.Path; break }
        } catch {}
    }

    foreach ($p in $procs) {
        try { Stop-Process -Id $p.Id -Force -ErrorAction Stop } catch {}
    }
    Start-Sleep -Seconds 1

    if ($exe) {
        try { Start-Process -FilePath $exe | Out-Null; Ok "Clash Verge 已重启并重新加载脚本" }
        catch { Warn "Clash 脚本已恢复，但自动重启失败，请手动启动 Clash Verge" }
    } else {
        Warn "Clash 脚本已恢复，请手动重启 Clash Verge"
    }
}

function Restore-ClashScript {
    $backupPath = Join-Path $ClashStateDir "Script.js.original"
    $absentFlag = Join-Path $ClashStateDir "Script.js.original_absent.flag"
    $pathFile   = Join-Path $ClashStateDir "script_path.txt"

    if (Test-Path $pathFile) {
        $scriptPath = (Get-Content $pathFile -Raw -Encoding UTF8).Trim()
    } else {
        $scriptPath = Join-Path $env:APPDATA "io.github.clash-verge-rev.clash-verge-rev\profiles\Script.js"
    }

    $parent = Split-Path -Parent $scriptPath
    if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }

    if (Test-Path $backupPath) {
        Copy-Item $backupPath $scriptPath -Force
        Ok "已恢复安装前的 Clash 全局扩展脚本"
        Remove-Item $ClashStateDir -Recurse -Force -ErrorAction SilentlyContinue
        Restart-ClashVerge
        return
    }

    if (Test-Path $absentFlag) {
        Remove-Item $scriptPath -Force -ErrorAction SilentlyContinue
        Ok "安装前没有 Clash Script.js；已删除 setup 创建的脚本"
        Remove-Item $ClashStateDir -Recurse -Force -ErrorAction SilentlyContinue
        Restart-ClashVerge
        return
    }

    # 没有备份状态时，只做保守 fallback：从现有 main() 中删掉 setup 标记块。
    if (Test-Path $scriptPath) {
        $text = Get-Content $scriptPath -Raw -Encoding UTF8
        $pattern = '(?s)\s*// BADMINTON_SETUP_BEGIN.*?// BADMINTON_SETUP_END\s*'
        if ($text -match '// BADMINTON_SETUP_BEGIN') {
            $clean = [regex]::Replace($text, $pattern, "`r`n", 1)
            Set-Content -Path $scriptPath -Value $clean -Encoding UTF8
            Ok "未找到原始备份，但已移除 BADMINTON_SETUP 标记块"
            Restart-ClashVerge
        } else {
            Warn "没有 Clash 原始备份，也没有发现 BADMINTON_SETUP 标记；不修改 Script.js"
        }
    } else {
        Warn "没有找到 Clash Script.js 或其备份"
    }
}

Write-Host ""
Write-Host "========================================" -ForegroundColor DarkCyan
Write-Host "     Badminton Uninstall / Rollback" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor DarkCyan
Write-Host ""
Write-Host "本次开关：" -ForegroundColor Cyan
Write-Host "  RemoveVenv                 = $RemoveVenv"
Write-Host "  RemoveUv                   = $RemoveUv"
Write-Host "  RemoveMitmProxyCA          = $RemoveMitmProxyCA"
Write-Host "  RestoreClashGlobalScript   = $RestoreClashGlobalScript"
Write-Host "  RemoveConfigToml           = $RemoveConfigToml"
Write-Host ""

Stop-ProjectMitm

# 1. Clash 原始脚本恢复
if ($RestoreClashGlobalScript -eq 1) {
    Info "恢复 Clash Verge 全局扩展脚本..."
    Restore-ClashScript
} else {
    Info "保留 Clash Verge 全局扩展脚本"
}

# 2. .venv
if ($RemoveVenv -eq 1) {
    $venv = Join-Path $Root ".venv"
    if (Test-Path $venv) {
        Info "删除 .venv..."
        Remove-Item $venv -Recurse -Force
        Ok ".venv 已删除"
    } else {
        Ok ".venv 不存在"
    }
    Remove-StateFile "venv_created_by_setup.flag"
} else {
    Info "保留 .venv"
}

# 3. uv + setup 安装的 uv-managed Python 3.11
if ($RemoveUv -eq 1) {
    $Uv = Find-Uv
    $pyFlag = Join-Path $StateDir "python311_installed_by_setup.flag"

    if ($Uv -and (Test-Path $pyFlag)) {
        Info "卸载 setup 额外安装的 uv-managed Python 3.11..."
        & $Uv python uninstall 3.11
        if ($LASTEXITCODE -ne 0) {
            Warn "uv python uninstall 3.11 返回非 0；继续清理其它内容"
        } else {
            Ok "uv-managed Python 3.11 已卸载"
        }
        Remove-Item $pyFlag -Force -ErrorAction SilentlyContinue
    }

    $uvOwned = Join-Path $StateDir "uv_installed_by_setup.flag"
    if (Test-Path $uvOwned) {
        $uvInstallDir = Join-Path $env:LOCALAPPDATA "uv"
        if (Test-Path $uvInstallDir) {
            Info "删除 setup 安装的 uv: $uvInstallDir"
            Remove-Item $uvInstallDir -Recurse -Force -ErrorAction SilentlyContinue
        }

        # 兼容官方安装器的另一常见位置，仅在 setup 记录为自己安装时清理。
        foreach ($p in @(
            (Join-Path $env:USERPROFILE ".local\bin\uv.exe"),
            (Join-Path $env:USERPROFILE ".local\bin\uvx.exe")
        )) {
            Remove-Item $p -Force -ErrorAction SilentlyContinue
        }

        Ok "setup 安装的 uv 已删除"
        Remove-Item $uvOwned -Force -ErrorAction SilentlyContinue
    } else {
        Ok "uv 在 setup 前已存在，按安全策略保留原有 uv"
    }
    Remove-StateFile "uv_path.txt"
} else {
    Info "保留 uv / uv-managed Python"
}

# 4. mitmproxy CA + 本地 CA 材料
if ($RemoveMitmProxyCA -eq 1) {
    Info "删除 Windows Root 中的 mitmproxy CA..."
    $certs = @(Get-ChildItem Cert:\LocalMachine\Root | Where-Object {
        $_.Subject -like '*mitmproxy*'
    })

    foreach ($cert in $certs) {
        try {
            Remove-Item ("Cert:\LocalMachine\Root\" + $cert.Thumbprint) -Force
            Ok "已删除 CA: $($cert.Thumbprint)"
        } catch {
            Warn "删除 CA 失败: $($cert.Thumbprint)"
        }
    }

    $mitmHome = Join-Path $env:USERPROFILE ".mitmproxy"
    if (Test-Path $mitmHome) {
        Remove-Item $mitmHome -Recurse -Force
        Ok "已删除 $mitmHome"
    }

    Remove-StateFile "ca_installed_by_setup.flag"
    Remove-StateFile "ca_thumbprint.txt"
    Remove-StateFile "mitm_home_created_by_setup.flag"
} else {
    Info "保留 mitmproxy CA / .mitmproxy"
}

# 5. config.toml（默认保留）
if ($RemoveConfigToml -eq 1) {
    $config = Join-Path $Root "config.toml"
    if (Test-Path $config) {
        Remove-Item $config -Force
        Ok "config.toml 已删除"
    }
    Remove-StateFile "config_created_by_setup.flag"
} else {
    Info "保留 config.toml"
}

# 清理空的 setup 状态目录；若仍有未卸载项目的状态，则保留它们。
if (Test-Path $StateDir) {
    $remain = @(Get-ChildItem $StateDir -Force -ErrorAction SilentlyContinue)
    if ($remain.Count -eq 0) {
        Remove-Item $StateDir -Force -ErrorAction SilentlyContinue
    }
}

Write-Host ""
Ok "所选项目处理完成"
Write-Host ""
Write-Host "若你把 RestoreClashGlobalScript=1，Clash 已恢复到 setup 运行前的 Script.js。" -ForegroundColor Green
Write-Host "若你把 RemoveMitmProxyCA=1，之后再次使用 mitmproxy 必须重新生成并信任 CA。" -ForegroundColor Yellow
Write-Host ""
