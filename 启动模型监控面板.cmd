@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  py -3 tools\model-ops-dashboard\server.py
) else (
  python tools\model-ops-dashboard\server.py
)
if %ERRORLEVEL% NEQ 0 pause
