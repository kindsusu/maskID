@echo off
chcp 949 >nul
title MarkID 차단 해제
echo.
echo  MarkID 파일의 다운로드 차단 표식(Mark of the Web)을 해제합니다...
powershell -NoProfile -Command "Get-ChildItem -LiteralPath '%~dp0' -Recurse -File | Unblock-File"
if errorlevel 1 (
  echo  자동 해제에 실패했습니다. 파일 우클릭 - 속성 - '차단 해제'를 이용해 주세요.
) else (
  echo  완료되었습니다. 이제 MarkID.exe 를 실행하세요.
)
echo.
pause
