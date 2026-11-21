# 安装当前用户的登录自启动快捷方式（无需管理员权限）。
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Startup = [Environment]::GetFolderPath("Startup")
$ShortcutPath = Join-Path $Startup "CareerZone X.lnk"
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = "powershell.exe"
$Shortcut.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Root\start.ps1`""
$Shortcut.WorkingDirectory = $Root
$Shortcut.IconLocation = "powershell.exe,0"
$Shortcut.Save()
Write-Host "已安装登录自启动：$ShortcutPath" -ForegroundColor Green
