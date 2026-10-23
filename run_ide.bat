@echo off
chcp 65001 > nul
title [DeepSeek-Antigravity IDE] 5대 협업체 실행기

cd /d "C:\Users\zkfnt\Desktop\DeepSeek_Antigravity_IDE"

echo =======================================================================
echo        🚀 DeepSeek-Antigravity IDE (5대 무인 협업 데스크톱 앱)
echo =======================================================================
echo.
echo [1/2] 파이썬 멀티에이전트 백엔드 엔진(포트 8890) 기동 중...
start /b python backend\server.py

echo [2/2] Electron 데스크톱 앱 윈도우 실행 중...
call npx -y electron@28.3.3 electron\main.js

echo.
echo [알림] IDE가 종료되었습니다.
pause
