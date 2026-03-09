@echo off
pushd "%~dp0"
chcp 65001 > nul
echo.
echo ==========================================
echo  GitHub 업로드 스크립트
echo ==========================================
echo.
:: 현재 깃 상태 보여주기
git status
echo.

:: 1. 커밋 메시지 입력받기
echo.
set /p COMMIT_MSG=">> 커밋 메시지를 입력하세요 (없으면 Enter): "

:: 2. 입력이 없으면 자동 메시지 생성
if not defined COMMIT_MSG (
    FOR /f "tokens=1-3 delims=/: " %%a IN ('TIME /T') DO SET CUR_TIME=%%a%%b
    FOR /f "tokens=1-3 delims=/. " %%a IN ('DATE /T') DO SET CUR_DATE=%%a-%%b-%%c
    SET COMMIT_MSG="Auto-commit at %CUR_DATE% %CUR_TIME%"
)

echo.
echo ==========================================
echo  Add -> Commit -> Push를 시작합니다...
echo ==========================================
echo.

:: 3. 모든 변경사항 추가
echo [1/3] git add .
git add .
echo.

:: 4. 커밋 실행
echo [2/3] git commit -m %COMMIT_MSG%
git commit -m %COMMIT_MSG%
echo.

:: 5. 원격 저장소로 푸시
echo [3/3] git push
git push
echo.

echo ==========================================
echo  ✅ GitHub 업로드 시도 완료!
echo  (결과를 확인하고 아무 키나 눌러 창을 닫으세요)
echo ==========================================
echo.

popd
pause
git push

echo.
echo ==========================================
echo  GitHub 업로드 완료!
echo ==========================================
echo.

popd
pause
