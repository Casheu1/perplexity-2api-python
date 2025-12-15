@echo off
echo ========================================
echo    Perplexity-2API Local Python Launcher
echo ========================================
echo.

REM Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.8+
    pause
    exit /b 1
)

REM Check requirements.txt
if not exist "requirements.txt" (
    echo [ERROR] requirements.txt not found
    pause
    exit /b 1
)

echo [INFO] Checking dependencies...
python -c "import botasaurus, fastapi, uvicorn, httpx" 2>nul
if errorlevel 1 (
    echo [INFO] Installing dependencies...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Failed to install dependencies
        pause
        exit /b 1
    )
    echo [SUCCESS] Dependencies installed
)

echo [INFO] Starting service...
echo [INFO] Access URL: http://127.0.0.1:8092
echo [INFO] Press Ctrl+C to stop
echo.

REM Start service in background and open browser
start /B uvicorn main:app --host 127.0.0.1 --port 8092 --reload --no-access-log
timeout /t 3 >nul
start http://127.0.0.1:8092
echo [INFO] Browser opened. Service is running in background.
echo [INFO] Press Ctrl+C to stop service.

pause