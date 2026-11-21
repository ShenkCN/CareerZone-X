# 移除当前用户的登录自启动快捷方式。
$Startup = [Environment]::GetFolderPath("Startup")
$ShortcutPath = Join-Path $Startup "CareerZone X.lnk"
if (Test-Path $ShortcutPath) {
    Remove-Item $ShortcutPath -Force
    Write-Host "已移除：$ShortcutPath" -ForegroundColor Green
} else {
    Write-Host "没有找到自启动快捷方式。" -ForegroundColor Yellow
}
