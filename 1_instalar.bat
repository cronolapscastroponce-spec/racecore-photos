@echo off
title Instalar panel de redes Racecore
cd /d "%~dp0"
echo ============================================
echo   Instalando el panel de redes de Racecore
echo ============================================
echo.

call :buscar_python
if not defined PYEXE (
  echo Instalando Python, tarda unos minutos...
  winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
  call :buscar_python
)
if not defined PYEXE (
  echo.
  echo No se ha podido instalar Python automaticamente.
  goto error
)
echo Python: %PYEXE%

if not exist ".venv\Scripts\python.exe" (
  echo Preparando el entorno...
  "%PYEXE%" -m venv .venv || goto error
)
echo Instalando librerias, tarda un poco...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet --disable-pip-version-check
".venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet --disable-pip-version-check || goto error

echo.
echo ============================================
echo   LISTO. Ahora haz doble clic en 2_probar.bat
echo ============================================
pause
exit /b 0

:buscar_python
set "PYEXE="
for %%P in ("%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "%ProgramFiles%\Python312\python.exe") do (
  if not defined PYEXE if exist "%%~P" set "PYEXE=%%~P"
)
exit /b 0

:error
echo.
echo *** Algo ha fallado. Haz una captura de esta ventana y mandasela a Claude. ***
pause
exit /b 1
