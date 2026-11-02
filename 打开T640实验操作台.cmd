@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Open-T640ExperimentConsole.ps1"
if errorlevel 1 pause
