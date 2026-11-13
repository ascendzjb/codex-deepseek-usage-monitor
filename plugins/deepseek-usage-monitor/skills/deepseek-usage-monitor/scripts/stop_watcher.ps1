$ErrorActionPreference = 'Stop'

$dataDir = if ($env:DEEPSEEK_USAGE_DATA_DIR) {
    $env:DEEPSEEK_USAGE_DATA_DIR
}
else {
    Join-Path $env:LOCALAPPDATA 'DeepSeekUsageMonitor'
}
$pidPath = Join-Path $dataDir 'watcher.pid'
$stopPath = Join-Path $dataDir 'watcher.stop'

New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
Set-Content -LiteralPath $stopPath -Value 'stop' -Encoding ASCII

$deadline = (Get-Date).AddSeconds(8)
while ((Get-Date) -lt $deadline) {
    if (-not (Test-Path -LiteralPath $pidPath)) {
        Write-Output 'DeepSeek process watcher stopped.'
        exit 0
    }
    Start-Sleep -Milliseconds 250
}

throw "The watcher did not stop within 8 seconds. Check $dataDir\watcher.log."
