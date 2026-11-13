$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$monitorScript = Join-Path $scriptDir 'monitor.py'
$dataDir = if ($env:DEEPSEEK_USAGE_DATA_DIR) {
    $env:DEEPSEEK_USAGE_DATA_DIR
}
else {
    Join-Path $env:LOCALAPPDATA 'DeepSeekUsageMonitor'
}
$stopFlag = Join-Path $dataDir 'stop.flag'
$intentionalStopFlag = Join-Path $dataDir 'intentional_stop.flag'

if (-not (Test-Path -LiteralPath $monitorScript)) {
    throw "Monitor script not found: $monitorScript"
}

New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
if (Test-Path -LiteralPath $stopFlag) {
    Remove-Item -LiteralPath $stopFlag -Force
}
if (Test-Path -LiteralPath $intentionalStopFlag) {
    Remove-Item -LiteralPath $intentionalStopFlag -Force
}

$pidFile = Join-Path $dataDir 'widget.pid'
$existingPid = Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue
if ($existingPid -and (Get-Process -Id $existingPid -ErrorAction SilentlyContinue)) {
    Write-Output "DeepSeek usage widget is already running (PID $existingPid)."
    exit 0
}

$python = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not $python) {
    $python = Get-Command python -ErrorAction SilentlyContinue
}
if (-not $python) {
    throw 'Python 3 is required but was not found on PATH.'
}

$pythonPath = $python.Source
$pythonwPath = Join-Path (Split-Path -Parent $pythonPath) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonwPath)) {
    $pythonwPath = $pythonPath
}

$process = Start-Process `
    -FilePath $pythonwPath `
    -ArgumentList @("`"$monitorScript`"") `
    -WorkingDirectory $scriptDir `
    -WindowStyle Hidden `
    -PassThru

Write-Output "Started DeepSeek usage widget (PID $($process.Id))."
