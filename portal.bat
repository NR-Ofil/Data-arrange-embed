@echo off
chcp 65001 >nul
title R:\ Search Portal

:: Check if port 8765 is already in use
netstat -ano | findstr ":8765 " | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
    echo Portal is already running at http://localhost:8765/portal/search.html
    echo Opening in browser...
    start http://localhost:8765/portal/search.html
    exit /b 0
)

echo Starting R:\ Search Portal...
echo URL: http://localhost:8765/portal/search.html
echo Press Ctrl-C to stop.
echo.

:loop
.venv\Scripts\python.exe portal/serve.py
echo.
echo Portal stopped. Restarting in 2 seconds... (Ctrl-C to quit)
timeout /t 2 /nobreak >nul
goto loop
