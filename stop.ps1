$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PidFile = Join-Path $Root '.run\app-process.json'

if (-not (Test-Path -LiteralPath $PidFile)) {
    Write-Host '未找到后台运行记录。若使用 .\start.ps1 -Dev 或 python app.py 启动，请在原终端按 Ctrl+C。' -ForegroundColor Yellow
    exit 0
}

try {
    $State = Get-Content -LiteralPath $PidFile -Raw -Encoding UTF8 | ConvertFrom-Json
    $ServerPid = [int]$State.pid
} catch {
    Write-Host "进程记录损坏：$PidFile，请手动检查，不会盲目结束进程。" -ForegroundColor Red
    exit 1
}

$Proc = Get-CimInstance Win32_Process -Filter "ProcessId=$ServerPid" -ErrorAction SilentlyContinue
if (-not $Proc) {
    Remove-Item -LiteralPath $PidFile -Force
    Write-Host '后台服务已不在运行，已清除失效 PID 记录。' -ForegroundColor Yellow
    exit 0
}

# 三重验证：PID、Python 路径及创建时间，防止 PID 被系统复用后误杀其他进程。
$MatchExe = [string]::Equals(
    [string]$Proc.ExecutablePath,
    [string]$State.python,
    [System.StringComparison]::OrdinalIgnoreCase
)
$MatchCmd = ([string]$Proc.CommandLine -match '(?i)\bapp\.py\b')
$MatchStart = $false
try {
    $ActualUtc = (Get-Process -Id $ServerPid).StartTime.ToUniversalTime()
    $RecordedUtc = [datetime]::Parse([string]$State.started_utc).ToUniversalTime()
    $MatchStart = [Math]::Abs(($ActualUtc - $RecordedUtc).TotalSeconds) -lt 2
} catch {}

if (-not ($MatchExe -and $MatchCmd -and $MatchStart)) {
    Write-Host "拒绝结束 PID=$ServerPid：它与本项目记录不一致，避免误杀。" -ForegroundColor Red
    Write-Host '可先使用 Get-CimInstance Win32_Process 检查实际进程。'
    exit 1
}

Write-Host "正在停止后台服务 PID=$ServerPid（包括它创建的 mitmdump 子进程）..." -ForegroundColor Cyan
# 强制结束进程树，确保后台 mitmdump 不残留。
# 开发阶段可用；注意这不是优雅退出，正在执行的预约请求会中断。
& taskkill.exe /PID $ServerPid /T /F | Out-Host
if ($LASTEXITCODE -ne 0 -and (Get-Process -Id $ServerPid -ErrorAction SilentlyContinue)) {
    Write-Host '结束进程失败，请检查权限或任务管理器。' -ForegroundColor Red
    exit 1
}

for ($i = 0; $i -lt 20; $i++) {
    if (-not (Get-Process -Id $ServerPid -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Milliseconds 100
}
if (Get-Process -Id $ServerPid -ErrorAction SilentlyContinue) {
    Write-Host '进程仍在运行，请检查 Windows 权限。' -ForegroundColor Red
    exit 1
}

Remove-Item -LiteralPath $PidFile -Force
Write-Host '后台服务已停止。' -ForegroundColor Green
