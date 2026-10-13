param(
    [switch]$Dev,
    [switch]$NoBrowser
)

# ASCII-only script: compatible with Windows PowerShell 5.1 and PowerShell 7.
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root

$Python  = Join-Path $Root '.venv\Scripts\python.exe'
$Config  = Join-Path $Root 'config.toml'
$RunDir  = Join-Path $Root '.run'
$PidFile = Join-Path $RunDir 'app-process.json'
$LogDir  = Join-Path $Root 'logs'
$Url     = 'http://127.0.0.1:8000/'

function Test-AppReady {
    # Unlike Get-NetTCPConnection, this call has a strict 400 ms timeout.
    $Response = $null
    try {
        $Request = [System.Net.HttpWebRequest]::Create($Url + 'api/auto/state')
        $Request.Proxy = $null
        $Request.Timeout = 400
        $Request.ReadWriteTimeout = 400
        $Response = $Request.GetResponse()
        return ([int]$Response.StatusCode -eq 200)
    } catch {
        return $false
    } finally {
        if ($null -ne $Response) { $Response.Close() }
    }
}

function Test-Port8000 {
    $Client = New-Object System.Net.Sockets.TcpClient
    try {
        $Connecting = $Client.ConnectAsync('127.0.0.1', 8000)
        return ($Connecting.Wait(250) -and $Client.Connected)
    } catch {
        return $false
    } finally {
        $Client.Dispose()
    }
}

function Open-Browser {
    if ($NoBrowser) { return }
    try {
        Start-Process -FilePath $Url | Out-Null
        Write-Host '[start] Browser opened.' -ForegroundColor Green
    } catch {
        Write-Warning "Unable to open the browser automatically. Open $Url manually. $($_.Exception.Message)"
    }
}

if (-not (Test-Path -LiteralPath $Python)) {
    Write-Host '[start] Missing .venv\Scripts\python.exe. Run setup.ps1 first.' -ForegroundColor Red
    exit 1
}
if (-not (Test-Path -LiteralPath $Config)) {
    Write-Host '[start] Missing config.toml. Run setup.ps1 first.' -ForegroundColor Red
    exit 1
}

$Txt = Get-Content -LiteralPath $Config -Raw -Encoding UTF8
if ($Txt -match 'YOUR_USER_ID|YOUR_CUSTOMER_ID|YOUR_NAME|YOUR_PHONE') {
    Write-Host '[start] Placeholder user fields remain in config.toml.' -ForegroundColor Red
    exit 1
}

# Idempotent start: opening the existing service must not spawn another copy.
if (Test-AppReady) {
    Write-Host '[start] Server is already running.' -ForegroundColor Yellow
    Open-Browser
    return
}
if (Test-Port8000) {
    Write-Host '[start] Port 8000 is busy, but the expected API does not respond.' -ForegroundColor Red
    Write-Host '[start] Not starting another server; check the existing process first.' -ForegroundColor Red
    exit 1
}

if ($Dev) {
    Write-Host '[start] DEV mode: foreground server. Press Ctrl+C to stop.' -ForegroundColor Cyan
    Open-Browser
    & $Python -u app.py
    exit $LASTEXITCODE
}

# Background mode. Redirect logs so stdout/stderr never keep the terminal busy.
New-Item -ItemType Directory -Force -Path $RunDir, $LogDir | Out-Null
$Stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$OutFile = Join-Path $LogDir "$Stamp.stdout.log"
$ErrFile = Join-Path $LogDir "$Stamp.stderr.log"

# Check an earlier tracked instance instead of overwriting a live PID record.
if (Test-Path -LiteralPath $PidFile) {
    try {
        $Existing = Get-Content -LiteralPath $PidFile -Raw -Encoding UTF8 | ConvertFrom-Json
        $OldPid = [int]$Existing.pid
        $OldProc = Get-CimInstance Win32_Process -Filter "ProcessId=$OldPid" -ErrorAction SilentlyContinue
        if ($OldProc -and
            [string]::Equals([string]$OldProc.ExecutablePath, [string]$Python,
                [System.StringComparison]::OrdinalIgnoreCase) -and
            ([string]$OldProc.CommandLine -match '(?i)\bapp\.py\b')) {
            Write-Host "[start] Tracked Python PID $OldPid is still alive, but the API is unavailable." -ForegroundColor Yellow
            Write-Host '[start] Inspect logs or run .\stop.ps1 before restarting.' -ForegroundColor Yellow
            exit 1
        }
    } catch {
        Write-Warning 'Old PID file could not be parsed; proceeding with a new start.'
    }
    Remove-Item -LiteralPath $PidFile -Force
}

$Proc = Start-Process -FilePath $Python `
    -ArgumentList @('-u', 'app.py') `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $OutFile `
    -RedirectStandardError $ErrFile `
    -PassThru

# Report launch IMMEDIATELY, before checking readiness.
Write-Host "[start] Python launched (PID=$($Proc.Id))." -ForegroundColor Green

$StartedUtc = (Get-Process -Id $Proc.Id).StartTime.ToUniversalTime().ToString('o')
$State = @{
    pid         = $Proc.Id
    python      = $Python
    root        = $Root
    started_utc = $StartedUtc
    stdout      = $OutFile
    stderr      = $ErrFile
}
$Json = $State | ConvertTo-Json
$Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllText($PidFile, $Json, $Utf8NoBom)

# Max ~5 seconds, not 40 expensive Get-NetTCPConnection calls.
$Timer = [System.Diagnostics.Stopwatch]::StartNew()
$Ready = $false
$Exited = $false
while ($Timer.Elapsed.TotalSeconds -lt 5) {
    $Proc.Refresh()
    if ($Proc.HasExited) { $Exited = $true; break }
    if (Test-AppReady) { $Ready = $true; break }
    Start-Sleep -Milliseconds 150
}

if ($Exited) {
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    Write-Host "[start] Python exited early. Check: $ErrFile" -ForegroundColor Red
    if (Test-Path -LiteralPath $ErrFile) {
        Get-Content -LiteralPath $ErrFile -Tail 20 -ErrorAction SilentlyContinue
    }
    exit 1
}

if ($Ready) {
    Write-Host '[start] API is ready.' -ForegroundColor Green
} else {
    Write-Host '[start] API is still starting. It may be ready in a moment.' -ForegroundColor Yellow
}

# Browser opening is NOT contingent on readiness succeeding.
Open-Browser
Write-Host '[start] Returning to PowerShell. The server keeps running in the background.' -ForegroundColor Green
Write-Host '[start] Stop: .\stop.ps1  |  Dev mode: .\start.ps1 -Dev'
Write-Host "[start] Logs: $ErrFile"
