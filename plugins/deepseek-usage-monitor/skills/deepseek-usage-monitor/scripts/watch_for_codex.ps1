$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$startScript = Join-Path $scriptDir 'start_widget.ps1'
$stopScript = Join-Path $scriptDir 'stop_widget.ps1'
$dataDir = if ($env:DEEPSEEK_USAGE_DATA_DIR) {
    $env:DEEPSEEK_USAGE_DATA_DIR
}
else {
    Join-Path $env:LOCALAPPDATA 'DeepSeekUsageMonitor'
}
$pidPath = Join-Path $dataDir 'watcher.pid'
$stopPath = Join-Path $dataDir 'watcher.stop'
$logPath = Join-Path $dataDir 'watcher.log'
$widgetPidPath = Join-Path $dataDir 'widget.pid'
$intentionalStopPath = Join-Path $dataDir 'intentional_stop.flag'

New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
if (Test-Path -LiteralPath $stopPath) {
    Remove-Item -LiteralPath $stopPath -Force
}

$existingPid = Get-Content -LiteralPath $pidPath -ErrorAction SilentlyContinue
if ($existingPid -and (Get-Process -Id $existingPid -ErrorAction SilentlyContinue)) {
    exit 0
}

Set-Content -LiteralPath $pidPath -Value $PID -Encoding ASCII
$seenRunning = $false

function Write-WatcherLog {
    param([string]$Message)
    $line = '{0:yyyy-MM-dd HH:mm:ss} {1}' -f (Get-Date), $Message
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
}

function Test-WidgetRunning {
    $widgetPid = Get-Content -LiteralPath $widgetPidPath -ErrorAction SilentlyContinue
    return [bool](
        $widgetPid -and
        (Get-Process -Id $widgetPid -ErrorAction SilentlyContinue)
    )
}

function Start-UsageWidget {
    try {
        if (Test-Path -LiteralPath $intentionalStopPath) {
            Remove-Item -LiteralPath $intentionalStopPath -Force
        }
        & powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File $startScript | Out-Null
        return $true
    }
    catch {
        Write-WatcherLog "Could not start usage orb: $($_.Exception.Message)"
        return $false
    }
}

try {
    Write-WatcherLog 'Watcher started; waiting for ChatGPT/Codex.'
    while (-not (Test-Path -LiteralPath $stopPath)) {
        $appRunning = [bool](Get-Process -Name 'ChatGPT' -ErrorAction SilentlyContinue)

        if ($appRunning -and -not $seenRunning) {
            if (Test-Path -LiteralPath $intentionalStopPath) {
                Write-WatcherLog 'ChatGPT/Codex detected; automatic restart suppressed by user close.'
            }
            elseif (Start-UsageWidget) {
                Write-WatcherLog 'ChatGPT/Codex detected; usage orb started.'
            }
            $seenRunning = $true
        }
        elseif ($appRunning -and $seenRunning -and -not (Test-WidgetRunning)) {
            if (
                -not (Test-Path -LiteralPath $intentionalStopPath) -and
                (Start-UsageWidget)
            ) {
                Write-WatcherLog 'Usage orb process was missing; restart completed.'
            }
        }
        elseif (-not $appRunning -and $seenRunning) {
            try {
                & powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File $stopScript | Out-Null
                Write-WatcherLog 'ChatGPT/Codex exited; usage orb stopped.'
            }
            catch {
                Write-WatcherLog "Could not stop usage orb: $($_.Exception.Message)"
            }
            Remove-Item -LiteralPath $intentionalStopPath -Force -ErrorAction SilentlyContinue
            $seenRunning = $false
        }

        Start-Sleep -Seconds 2
    }
}
finally {
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $stopPath -Force -ErrorAction SilentlyContinue
    Write-WatcherLog 'Watcher stopped.'
}
