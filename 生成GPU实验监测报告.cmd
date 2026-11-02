@echo off
setlocal
cd /d "%~dp0"
if "%~1"=="" (
  echo 请把 gpu-telemetry.csv 拖到这个 CMD 文件上，或在命令行传入 CSV 路径。
  pause
  exit /b 2
)
set "INPUT=%~f1"
set "OUTPUT=%~dpn1-report"
python scripts\generate_gpu_monitor_report.py --input "%INPUT%" --output "%OUTPUT%" --png --open
if errorlevel 1 (
  echo 报告生成失败。
  pause
  exit /b 1
)
echo 报告已生成：%OUTPUT%
endlocal

