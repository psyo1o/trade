@echo off
setlocal
pushd "%~dp0"
title Bot GUI Auto-Restart

REM run_gui.py quits after ~36s without internet (see run_gui.py). Then this loop restarts the GUI.
REM To disable that behavior: set BOT_DISABLE_NET_WATCH=1 before starting this bat.

set "QT_LOGGING_RULES=*.debug=false;*.warning=false"

:loop
echo =========================================
echo [%date% %time%] Starting run_gui.py ...
echo =========================================

if exist "__pycache__" rmdir /s /q "__pycache__"
if exist "strategy\__pycache__" rmdir /s /q "strategy\__pycache__"

py -3.11 -B -W ignore run_gui.py

echo.
echo =========================================
echo [%date% %time%] GUI process has stopped.
echo Restarting in 10 seconds...
echo =========================================
timeout /t 10 /nobreak >nul
goto loop
