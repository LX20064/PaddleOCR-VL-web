# One-click Windows icon cache fix (taskbar/explorer icon shows blank, block or stale icon)
$ErrorActionPreference = 'SilentlyContinue'

Write-Host 'Stopping explorer.exe ...'
Stop-Process -Name explorer -Force
Start-Sleep -Seconds 2

$targets = @(
  "$env:LOCALAPPDATA\IconCache.db",
  "$env:LOCALAPPDATA\Microsoft\Windows\Explorer\iconcache_*.db",
  "$env:LOCALAPPDATA\Microsoft\Windows\Explorer\thumbcache_*.db"
)
foreach ($t in $targets) {
  Get-ChildItem -Path $t | Remove-Item -Force
}

Write-Host 'Icon cache cleared. Restarting explorer.exe ...'
Start-Process explorer.exe
Start-Sleep -Seconds 1

Write-Host 'DONE. If taskbar icon still stale, reboot the PC (most thorough).'
