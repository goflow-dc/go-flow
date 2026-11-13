@echo off
rem Opens Go Flow as administrator: also cleans Windows system folders and makes restore points.
net session >nul 2>&1
if %errorlevel% neq 0 (
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
cd /d "%~dp0"
start "" pythonw "%~dp0cleaner.py"
