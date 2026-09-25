@echo off
rem Arranca el panel y lo relanza si se cae. Lo usa la tarea programada
rem (windows\instalar_servicio.ps1); tambien vale para lanzarlo a mano.
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
if not exist logs mkdir logs

:bucle
echo [%date% %time%] Arrancando panel >> logs\panel.log
".venv\Scripts\python.exe" -u app.py >> logs\panel.log 2>&1
echo [%date% %time%] Panel parado (codigo %errorlevel%), reinicio en 10 s >> logs\panel.log
rem ping como pausa: timeout falla cuando no hay consola (tarea programada)
ping -n 11 127.0.0.1 > nul
goto bucle
