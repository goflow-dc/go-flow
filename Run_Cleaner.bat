@echo off
rem Opens Go Flow in its own window (no console). Close the window to stop it.
cd /d "%~dp0"
start "" pythonw "%~dp0cleaner.py"
