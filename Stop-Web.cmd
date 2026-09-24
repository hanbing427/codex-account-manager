@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-web.ps1" --stop
if errorlevel 1 pause
