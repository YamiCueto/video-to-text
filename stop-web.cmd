@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" run_web.py --stop
if errorlevel 1 pause
