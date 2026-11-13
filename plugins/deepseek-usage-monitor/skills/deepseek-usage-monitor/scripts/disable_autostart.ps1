$ErrorActionPreference = 'Stop'

$startupDir = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startupDir 'DeepSeek Usage Monitor.lnk'

if (Test-Path -LiteralPath $shortcutPath) {
    Remove-Item -LiteralPath $shortcutPath -Force
    Write-Output 'Disabled DeepSeek usage monitor autostart.'
}
else {
    Write-Output 'DeepSeek usage monitor autostart is not enabled.'
}
