# Actualiza el panel desde GitHub. NO toca fotos, publicaciones ni ajustes.
# Normalmente se lanza con actualizar.bat, desde la carpeta del panel.
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$base = "https://raw.githubusercontent.com/cronolapscastroponce-spec/racecore-photos/claude/clever-sagan-924ix1"
$archivos = @(
    "app.py", "ia_fondo.py", "requirements.txt", "DESPLIEGUE.md", "fuentes/LEEME.txt",
    "fuentes/Montserrat-BlackItalic.ttf", "fuentes/Montserrat-ExtraBoldItalic.ttf", "fuentes/OFL-Montserrat.txt",
    "1_instalar.bat", "2_probar.bat", "3_arranque_automatico.bat", "quitar_arranque_automatico.bat", "actualizar.bat",
    "windows/arrancar.bat", "windows/instalar_servicio.ps1", "windows/desinstalar_servicio.ps1", "windows/actualizar.ps1"
)
$carpeta = (Get-Location).Path
if (-not (Test-Path (Join-Path $carpeta "app.py"))) {
    throw "Esto hay que ejecutarlo dentro de la carpeta del panel (donde esta app.py)."
}

# Si hay una version nueva de este mismo actualizador, se usa esa (puede traer archivos nuevos)
if ($PSCommandPath) {
    $nuevo = Join-Path $env:TEMP "racecore-actualizar.ps1"
    Invoke-WebRequest -UseBasicParsing -Uri "$base/windows/actualizar.ps1?v=$(Get-Random)" -OutFile $nuevo
    if ((Get-FileHash $nuevo).Hash -ne (Get-FileHash $PSCommandPath).Hash) {
        Copy-Item $nuevo $PSCommandPath -Force
        & $PSCommandPath
        return
    }
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
# Solo se copia cuando todo se ha descargado bien, y solo lo que ha cambiado: si el panel
# esta en marcha tiene abiertas las fuentes y Windows no deja sobrescribirlas.
$bloqueados = @()
foreach ($a in $archivos) {
    $origen = Join-Path $tmp $a
    $destino = Join-Path $carpeta $a
    $igual = $false
    try { $igual = (Test-Path $destino) -and ((Get-FileHash $origen).Hash -eq (Get-FileHash $destino).Hash) } catch {}
    if ($igual) { continue }
    New-Item -ItemType Directory -Force -Path (Split-Path $destino) | Out-Null
    try {
        Copy-Item $origen $destino -Force
        Write-Host "  actualizado: $a"
    } catch {
        $bloqueados += $a
    }
}

Write-Host "Actualizando librerias..."
& (Join-Path $carpeta ".venv\Scripts\python.exe") -m pip install -r (Join-Path $carpeta "requirements.txt") --quiet --disable-pip-version-check

Write-Host ""
if ($bloqueados) {
    Write-Host "No se han podido actualizar porque el panel los esta usando:" -ForegroundColor Yellow
    $bloqueados | ForEach-Object { Write-Host "  $_" -ForegroundColor Yellow }
    Write-Host "Cierra el panel (la ventana negra, o quitar_arranque_automatico.bat) y vuelve a abrir actualizar.bat." -ForegroundColor Yellow
    Write-Host ""
}
Write-Host "==========  ACTUALIZADO  =========="
if (Get-ScheduledTask -TaskName "Racecore Redes" -ErrorAction SilentlyContinue) {
    Write-Host "Para que se aplique: doble clic en 3_arranque_automatico.bat"
} else {
    Write-Host "Para que se aplique: cierra la ventana negra del panel y abre otra vez 2_probar.bat"
}
