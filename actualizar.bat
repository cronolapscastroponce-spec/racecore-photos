@echo off
title Actualizar panel Racecore
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\actualizar.ps1"
pause
