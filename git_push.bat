@echo off
pushd "%~dp0"
chcp 65001 >nul
echo.
echo ==========================================
echo  Git add / commit / push (origin main)
echo ==========================================
echo.

git remote get-url origin >nul 2>&1
if errorlevel 1 (
  echo [안내] origin 이 없습니다. 먼저 github_trade_remote.bat 을 실행하세요.
  popd
  pause
  exit /b 1
)

git status
echo.

set /p COMMIT_MSG=">> 커밋 메시지 (한국어, 비우면 자동): "

if not defined COMMIT_MSG (
  FOR /f "tokens=1-3 delims=/: " %%a IN ('TIME /T') DO SET CUR_TIME=%%a%%b
  FOR /f "tokens=1-3 delims=/. " %%a IN ('DATE /T') DO SET CUR_DATE=%%a-%%b-%%c
  SET COMMIT_MSG=자동 커밋 %CUR_DATE% %CUR_TIME%
)

echo.
echo [1/3] git add -A
git add -A
if errorlevel 1 (
  echo [오류] git add 실패
  popd
  pause
  exit /b 1
)

echo.
echo [2/3] git commit
git commit -m "%COMMIT_MSG%"
if errorlevel 1 (
  echo [안내] 커밋할 변경이 없거나 메시지 오류일 수 있습니다.
  popd
  pause
  exit /b 0
)

echo.
echo [3/3] git push -u origin main
git push -u origin main
if errorlevel 1 (
  echo.
  echo [힌트] 첫 푸시 거절 시 GitHub 에서 빈 trade 저장소를 만들었는지,
  echo        SSH 대신 HTTPS URL 을 썼는지 확인하세요.
)

echo.
echo ==========================================
echo  완료 (위 메시지 확인)
echo ==========================================
popd
pause
