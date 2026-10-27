@echo off
chcp 65001 > nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Missing .venv\Scripts\python.exe. Create the Python 3.14.8 virtual environment first.
    exit /b 1
)

".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info[:3] == (3, 14, 8) else 1)" > nul 2>&1
if errorlevel 1 (
    echo This bot requires exactly Python 3.14.8. Recreate .venv with that version.
    exit /b 1
)

:loop
echo [%date% %time%] Starting Discord bot...
call ".venv\Scripts\python.exe" main.py
echo [%date% %time%] Bot stopped. Restarting in 3 seconds...
timeout /t 3 /nobreak > nul
goto loop
