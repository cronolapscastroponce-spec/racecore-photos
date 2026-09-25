# Registra el panel como tarea programada: arranca con Windows (sin iniciar
# sesion) y se relanza sola si se cae. Ejecutar en PowerShell como administrador:
#   powershell -ExecutionPolicy Bypass -File windows\instalar_servicio.ps1
$ErrorActionPreference = "Stop"
$nombre   = "Racecore Redes"
$proyecto = Split-Path -Parent $PSScriptRoot
$bat      = Join-Path $PSScriptRoot "arrancar.bat"

if (-not (Test-Path (Join-Path $proyecto ".venv\Scripts\python.exe"))) {
    throw "No existe .venv en $proyecto. Crea el entorno primero (ver DESPLIEGUE.md, paso 1)."
}
if (-not (Test-Path (Join-Path $proyecto ".env"))) {
    Write-Warning "No hay .env: el panel arrancara sin claves de OpenAI/Telegram."
}

# Parar lo que haya en marcha (tarea anterior o un python app.py lanzado a mano)
& (Join-Path $PSScriptRoot "desinstalar_servicio.ps1") -Silencioso

$accion     = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$bat`"" -WorkingDirectory $proyecto
$disparador = New-ScheduledTaskTrigger -AtStartup
$usuario    = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$ajustes    = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) `
                -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $nombre -Action $accion -Trigger $disparador `
    -Principal $usuario -Settings $ajustes -Force | Out-Null
Start-ScheduledTask -TaskName $nombre

Write-Host "OK: tarea '$nombre' instalada y en marcha."
Write-Host "Panel: http://localhost:5000   Log: $proyecto\logs\panel.log"
