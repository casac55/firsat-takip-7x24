@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Sanal ortam bulunamadi. Once PowerShell'de .\kurulum.ps1 calistirin.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" main.py --daemon
pause
