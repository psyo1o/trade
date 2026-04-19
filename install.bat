@echo off
pushd "%~dp0"

echo Installing packages for Python 3.11...
py -3.11 -m pip install -r requirements.txt

echo Installation complete! Press any key to exit.
pause
