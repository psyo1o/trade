@echo off
chcp 65001 >nul
pushd "%~dp0"
echo.
echo ==========================================
echo  GitHub 저장소 trade 와 origin 연결
echo ==========================================
echo.
echo 브라우저에서 빈 저장소를 만든 뒤 진행하세요.
echo   https://github.com/내아이디/trade
echo  (README 추가 없이 만들면 첫 푸시가 수월합니다.)
echo.
set /p GH_USER="GitHub 사용자명 또는 조직 이름: "
if "%GH_USER%"=="" (
  echo [취소] 사용자명이 비었습니다.
  popd
  exit /b 1
)

git remote remove origin 2>nul
git remote add origin "https://github.com/%GH_USER%/trade.git"
if errorlevel 1 (
  echo [오류] remote add 실패. Git 설치·경로를 확인하세요.
  popd
  exit /b 1
)

echo.
echo [등록된 remote]
git remote -v
echo.
echo 다음 단계:
echo   1) 변경사항 커밋:  git_push.bat  실행
echo   2) 또는 수동:     git add -A
echo                    git commit -m "메시지"
echo                    git push -u origin main
echo.
popd
pause
