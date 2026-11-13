$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$startScript = Join-Path $scriptDir 'start_widget.ps1'
$watchScript = Join-Path $scriptDir 'watch_for_codex.ps1'
$powershellPath = (Get-Command powershell.exe).Source
$shell = New-Object -ComObject WScript.Shell

function New-MonitorShortcut {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$ScriptPath,
        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    $shortcut = $shell.CreateShortcut($Path)
    $shortcut.TargetPath = $powershellPath
    $shortcut.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$ScriptPath`""
    $shortcut.WorkingDirectory = $scriptDir
    $shortcut.WindowStyle = 7
    $shortcut.Description = $Description
    $shortcut.Save()
}

$desktopDir = [Environment]::GetFolderPath('Desktop')
$startupDir = [Environment]::GetFolderPath('Startup')
$desktopPath = Join-Path $desktopDir 'DeepSeek Usage.lnk'
$startupPath = Join-Path $startupDir 'DeepSeek Usage Monitor.lnk'

New-MonitorShortcut -Path $desktopPath -ScriptPath $startScript -Description 'Open the DeepSeek usage monitor'
New-MonitorShortcut -Path $startupPath -ScriptPath $watchScript -Description 'Start the DeepSeek usage monitor with ChatGPT/Codex'

$oldStartupPath = Join-Path $startupDir 'DeepSeek Usage Monitor.lnk'
$oldChineseDesktop = Join-Path $desktopDir 'DeepSeek 用量.lnk'
$oldChineseStartup = Join-Path $startupDir 'DeepSeek 用量监控自启动.lnk'
foreach ($path in @($oldChineseDesktop, $oldChineseStartup)) {
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Force
    }
}

Write-Output "Desktop shortcut: $desktopPath"
Write-Output "Startup shortcut: $startupPath"
