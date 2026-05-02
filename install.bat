@echo off
pushd "%~dp0"

echo Installing 64-bit environment and packages...
pip install -r requirements.txt

echo Installation complete! Press any key to exit.
pause