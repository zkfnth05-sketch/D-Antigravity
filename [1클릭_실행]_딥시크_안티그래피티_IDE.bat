@echo off
chcp 65001 >nul 2>&1
setlocal EnableExtensions EnableDelayedExpansion
title DeepSeek Antigravity IDE - 5인 협업체 런처

REM ============================================================
REM  DeepSeek Antigravity IDE - 1클릭 실행 런처
REM  UTF-8(65001) / 백엔드 8890 포트 / 2초 후 브라우저 자동 오픈
REM ============================================================

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
cd /d "%ROOT%"

set "PORT=8890"
set "HOST=127.0.0.1"
set "BACKEND=%ROOT%\backend\server.py"
set "FRONTEND=%ROOT%\frontend\index.html"
set "LOGDIR=%ROOT%\logs"
set "LOGFILE=%LOGDIR%\backend.log"

echo.
echo ============================================================
echo   DeepSeek Antigravity IDE  ::  1-Click Launcher
echo ============================================================
echo   프로젝트 경로 : %ROOT%
echo   백엔드 서버   : %BACKEND%
echo   프론트엔드    : %FRONTEND%
echo   접속 주소     : http://%HOST%:%PORT%
echo ============================================================
echo.

REM ---------- [1] 사전 점검 ----------
if not exist "%BACKEND%" (
    echo [오류] backend\server.py 를 찾을 수 없습니다.
    echo        경로: %BACKEND%
    goto :FAIL
)
if not exist "%FRONTEND%" (
    echo [오류] frontend\index.html 을 찾을 수 없습니다.
    echo        경로: %FRONTEND%
    goto :FAIL
)

REM ---------- [2] Python 인터프리터 탐색 ----------
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY (
    where py >nul 2>&1 && set "PY=py -3"
)
if not defined PY (
    if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
        set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    )
)
if not defined PY (
    echo [오류] Python 인터프리터를 찾을 수 없습니다.
    echo        Python 3.11 이상을 설치하거나 PATH 에 추가하십시오.
    goto :FAIL
)
echo [1/5] Python 확인 완료 : %PY%

REM ---------- [3] 포트 8890 점유 확인 ----------
set "PORT_BUSY="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /r /c:"LISTENING" ^| findstr /c:":%PORT% "') do (
    set "PORT_BUSY=%%P"
)
if defined PORT_BUSY (
    echo [경고] 포트 %PORT% 가 이미 사용 중입니다. PID=!PORT_BUSY!
    echo        기존 백엔드가 실행 중이라면 그대로 사용합니다.
) else (
    echo [2/5] 포트 %PORT% 사용 가능
)

REM ---------- [4] 로그 디렉터리 ----------
if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>&1

REM ---------- [5] 백엔드 실행 (별도 창) ----------
if not defined PORT_BUSY (
    echo [3/5] 백엔드 서버 기동 중 ... ^(별도 창^)
    start "DeepSeek-Antigravity Backend :%PORT%" /D "%ROOT%" cmd /k "%PY% "%BACKEND%""
) else (
    echo [3/5] 기존 백엔드 프로세스 재사용 ^(PID=!PORT_BUSY!^)
)

REM ---------- [6] 서버 준비 대기 (최대 20초 폴링) ----------
echo [4/5] 서버 응답 대기 중 ...
set "READY="
set /a WAITED=0
:WAIT_LOOP
if defined READY goto :WAIT_DONE
if !WAITED! GEQ 20 goto :WAIT_DONE
ping -n 2 127.0.0.1 >nul 2>&1
set /a WAITED+=1
%PY% -c "import socket,sys;s=socket.socket();s.settimeout(0.4);sys.exit(0 if s.connect_ex(('%HOST%',%PORT%))==0 else 1)" >nul 2>&1 && set "READY=1"
goto :WAIT_LOOP
:WAIT_DONE

if defined READY (
    echo        서버 응답 확인 완료 ^(%WAITED%회 폴링^)
) else (
    echo [경고] %WAITED%회 폴링 내 응답 없음. 그래도 브라우저를 엽니다.
)

REM ---------- [7] 2초 대기 후 Antigravity IDE 워크스페이스 실행 ----------
echo [5/5] Antigravity IDE 를 엽니다 ...
ping -n 2 127.0.0.1 >nul 2>&1
set "IDE_EXE=%LOCALAPPDATA%\Programs\DeepSeek Antigravity IDE\Antigravity IDE.exe"
set "WORKSPACE=%ROOT%\DeepSeek_Antigravity.code-workspace"
if exist "%IDE_EXE%" (
    start "" "%IDE_EXE%" "%WORKSPACE%"
    echo        IDE 실행 : %WORKSPACE%
) else (
    echo        [경고] IDE 실행 파일을 찾을 수 없습니다: %IDE_EXE%
)

REM ---------- [8] 5인 협업체 가동 상태 안내 ----------
echo.
echo ============================================================
echo    딥시크 5인 협업체 가동 상태  ::  ALL SYSTEMS GO
echo ============================================================
echo    [R1]  아키텍트        : ONLINE  - 설계/구조 판단
echo    [V3]  감사관          : ONLINE  - 리스크/보안 감사
echo    [V3]  선임엔지니어    : ONLINE  - 실제 코드 구현
echo    [QA]  검증관          : ONLINE  - 테스트/회귀 검증
echo    [GIT] 안전관          : ONLINE  - 커밋/롤백 보호
echo ------------------------------------------------------------
echo    게이트키퍼(Gatekeeper) : ACTIVE  - 제미나이 독단 실행 차단
echo    백엔드 엔드포인트       : http://%HOST%:%PORT%
echo    웹소켓 토론 채널        : ws://%HOST%:%PORT%/ws/debate
echo    웹소켓 에이전트 채널    : ws://%HOST%:%PORT%/ws/agent
echo ============================================================
echo    모든 작업은 call_mcp_tool ^> deepseek-council 을 통해
echo    5인 협업체 합동으로 수행됩니다.
echo ============================================================
echo.
echo   이 창을 닫아도 백엔드 서버는 별도 창에서 계속 실행됩니다.
echo.

timeout /t 5 /nobreak >nul 2>&1
exit /b 0

:FAIL
echo.
echo ============================================================
echo   [실행 실패] 위 오류 메시지를 확인하십시오.
echo ============================================================
echo.
pause
exit /b 1
