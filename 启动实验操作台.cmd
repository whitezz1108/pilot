@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Start-ExperimentConsole.ps1"
if errorlevel 1 pause
