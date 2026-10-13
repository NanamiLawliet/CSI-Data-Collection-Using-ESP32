@echo off
title ESP32 CSI Monitor
echo ========================================================
echo   Starting ESP32 CSI Real-Time Web Dashboard
echo ========================================================
echo.
py web_app.py %*
if %ERRORLEVEL% NEQ 0 (
    python web_app.py %*
)
pause

