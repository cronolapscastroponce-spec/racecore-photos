@echo off
title Panel Racecore - NO CIERRES ESTA VENTANA
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Primero haz doble clic en 1_instalar.bat
  pause
  exit /b 1
)
echo El panel se abre solo en el navegador: http://localhost:5000
echo Mientras esta ventana siga abierta, el panel funciona.
echo Para pararlo, cierra esta ventana.
echo.
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" app.py --abrir
pause
