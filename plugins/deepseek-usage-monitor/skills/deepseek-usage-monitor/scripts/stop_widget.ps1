$ErrorActionPreference = 'Stop'

$dataDir = if ($env:DEEPSEEK_USAGE_DATA_DIR) {
    $env:DEEPSEEK_USAGE_DATA_DIR
}
else {
    Join-Path $env:LOCALAPPDATA 'DeepSeekUsageMonitor'
}
$stopFlag = Join-Path $dataDir 'stop.flag'
$intentionalStopFlag = Join-Path $dataDir 'intentional_stop.flag'

New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
Set-Content -LiteralPath $intentionalStopFlag -Value 'stop' -Encoding ASCII
Set-Content -LiteralPath $stopFlag -Value 'stop' -Encoding ASCII

$deadline = (Get-Date).AddSeconds(8)
$pidFile = Join-Path $dataDir 'widget.pid'
while ((Get-Date) -lt $deadline) {
    if (-not (Test-Path -LiteralPath $pidFile)) {
        Write-Output 'DeepSeek usage widget stopped.'
        exit 0
    }
    Start-Sleep -Milliseconds 250
}

$processId = Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue
if ($processId -and (Get-Process -Id $processId -ErrorAction SilentlyContinue)) {
    throw "The widget did not stop within 8 seconds (PID $processId). Check $dataDir\widget.log."
}

Write-Output 'DeepSeek usage widget stopped.'
