@echo off
chcp 65001 > nul
echo.
echo ==========================================
echo  GitHub 자동 업로드 (Add -> Commit -> Push)
echo ==========================================
echo.

:: 1. 변경된 파일 스테이징
git add .

:: 2. 커밋 메시지 입력 (엔터치면 기본값)
set /p commit_msg="커밋 메시지를 입력하세요 (기본값: Update): "
if "%commit_msg%"=="" set commit_msg="Update"

:: 3. 커밋 생성
git commit -m "%commit_msg%"

:: 4. 깃허브로 푸시
echo.
echo 깃허브로 업로드 중...
git push origin main

echo.
echo ==========================================
echo  업로드 완료! (아무 키나 누르면 종료)
echo ==========================================
pause > nul
