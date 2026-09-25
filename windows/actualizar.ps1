# Actualiza el panel desde GitHub. NO toca fotos, publicaciones ni ajustes.
# Normalmente se lanza con actualizar.bat, desde la carpeta del panel.
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$base = "https://raw.githubusercontent.com/cronolapscastroponce-spec/racecore-photos/claude/clever-sagan-924ix1"
$archivos = @(
    "app.py", "ia_fondo.py", "requirements.txt", "DESPLIEGUE.md", "fuentes/LEEME.txt",
    "1_instalar.bat", "2_probar.bat", "3_arranque_automatico.bat", "quitar_arranque_automatico.bat", "actualizar.bat",
    "windows/arrancar.bat", "windows/instalar_servicio.ps1", "windows/desinstalar_servicio.ps1", "windows/actualizar.ps1"
)
$carpeta = (Get-Location).Path
if (-not (Test-Path (Join-Path $carpeta "app.py"))) {
    throw "Esto hay que ejecutarlo dentro de la carpeta del panel (donde esta app.py)."
}

Write-Host "Descargando la ultima version..."
$tmp = Join-Path $env:TEMP "racecore-actualizar"
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
$marca = Get-Random
foreach ($a in $archivos) {
    $destino = Join-Path $tmp $a
    New-Item -ItemType Directory -Force -Path (Split-Path $destino) | Out-Null
    Invoke-WebRequest -UseBasicParsing -Uri "$base/$($a)?v=$marca" -OutFile $destino
}
# Solo se copia cuando todo se ha descargado bien
foreach ($a in $archivos) {
    $destino = Join-Path $carpeta $a
    New-Item -ItemType Directory -Force -Path (Split-Path $destino) | Out-Null
    Copy-Item (Join-Path $tmp $a) $destino -Force
}

Write-Host "Actualizando librerias..."
& (Join-Path $carpeta ".venv\Scripts\python.exe") -m pip install -r (Join-Path $carpeta "requirements.txt") --quiet --disable-pip-version-check

Write-Host ""
Write-Host "==========  ACTUALIZADO  =========="
if (Get-ScheduledTask -TaskName "Racecore Redes" -ErrorAction SilentlyContinue) {
    Write-Host "Para que se aplique: doble clic en 3_arranque_automatico.bat"
} else {
    Write-Host "Para que se aplique: cierra la ventana negra del panel y abre otra vez 2_probar.bat"
}
