@echo off
title Bot Auto-Restart

:loop
echo =========================================
echo [%date% %time%] Starting the bot process...
echo =========================================

if exist __pycache__ rmdir /s /q __pycache__
if exist strategy\__pycache__ rmdir /s /q strategy\__pycache__

python -B -W ignore gui_main.py

echo.
echo =========================================
echo [%date% %time%] Bot process has stopped.
echo Restarting in 10 seconds...
echo =========================================
timeout /t 10 > nul
goto loop