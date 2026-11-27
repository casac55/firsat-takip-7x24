# Bilgisayar açılışında arka planda otomatik çalıştırmak için Windows Zamanlanmış Görev veya Başlangıç kısayolu ekler
$ErrorActionPreference = "Stop"
$ProjectDir = $PSScriptRoot
$PythonExe = Join-Path $ProjectDir ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $PythonExe)) {
    $PythonExe = Join-Path $ProjectDir ".venv\Scripts\python.exe"
}

$TaskName = "SamsungS25UltraFiyatTakip"
try {
    $Action = New-ScheduledTaskAction -Execute $PythonExe -Argument "main.py --daemon" -WorkingDirectory $ProjectDir
    $Trigger = New-ScheduledTaskTrigger -AtLogOn
    $Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 2) -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Force | Out-Null
    Write-Host "Zamanlanmış görev '$TaskName' oluşturuldu! Oturum açıldığında arka planda otomatik başlayacak." -ForegroundColor Green
} catch {
    Write-Host "Zamanlanmış görev yetkisi kısıtlı, Windows Başlangıç klasörüne kısayol ekleniyor..." -ForegroundColor Yellow
    $StartupDir = [Environment]::GetFolderPath("Startup")
    $ShortcutPath = Join-Path $StartupDir "$TaskName.lnk"
    $WshShell = New-Object -ComObject WScript.Shell
    $Shortcut = $WshShell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = $PythonExe
    $Shortcut.Arguments = "main.py --daemon"
    $Shortcut.WorkingDirectory = $ProjectDir
    $Shortcut.WindowStyle = 7
    $Shortcut.Save()
    Write-Host "Başlangıç kısayolu oluşturuldu: $ShortcutPath" -ForegroundColor Green
}
