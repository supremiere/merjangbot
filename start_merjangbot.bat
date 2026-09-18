@echo off
setlocal
cd /d "%~dp0"

if not exist "main.py" (
    echo [ERROR] Put this file in the merjangbot folder with main.py.
    goto failed
)

if not exist ".env" (
    echo [ERROR] The .env file is missing from this folder.
    echo Copy your existing .env here. Do not upload it to GitHub.
    goto failed
)

if not exist ".venv\Scripts\python.exe" (
    echo [SETUP] Creating a local Python environment...
    py -3 -m venv ".venv" >nul 2>&1
    if errorlevel 1 python -m venv ".venv"
    if errorlevel 1 (
        echo [ERROR] Python 3 is not installed or could not create .venv.
        goto failed
    )
)

echo [SETUP] Installing required packages...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r "requirements.txt"
if errorlevel 1 (
    echo [ERROR] Package installation failed. See the messages above.
    goto failed
)

echo [RUN] Starting merjangbot. Keep this window open.
".venv\Scripts\python.exe" "main.py"
if errorlevel 1 (
    echo [ERROR] The bot stopped with an error. See the messages above.
    goto failed
)

echo [STOP] The bot has stopped.
pause
exit /b 0

:failed
pause
exit /b 1
