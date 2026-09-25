# Para el panel y quita la tarea programada. Se lanza con quitar_arranque_automatico.bat
param([switch]$Silencioso)
$nombre = "Racecore Redes"

if (Get-ScheduledTask -TaskName $nombre -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $nombre -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $nombre -Confirm:$false
}

# Parar tambien el bucle de arrancar.bat y el python que cuelga de el
Get-CimInstance Win32_Process |
    Where-Object { ($_.Name -eq "cmd.exe" -and $_.CommandLine -like "*arrancar.bat*") -or
                   ($_.Name -eq "python.exe" -and $_.CommandLine -like "*app.py*") } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

if (-not $Silencioso) { Write-Host "OK: panel parado y tarea '$nombre' eliminada." }
