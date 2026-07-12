@echo off
setlocal EnableExtensions

rem ============================================================
rem IBKR Paper Trading - ORB Runner
rem Put this BAT file in the same folder as ib_orb_paper.py
rem ============================================================

cd /d "%~dp0"

rem ---------------- Strategy settings ----------------
rem These values can also be set by a parent BAT before calling this file.
if not defined SYMBOL       set "SYMBOL=TSLA"
if not defined CASH         set "CASH=10000"
if not defined SLP          set "SLP=1.50"
if not defined TPP          set "TPP=3.25"
if not defined RANGE_END    set "RANGE_END=945"
if not defined ENTRY_CUTOFF set "ENTRY_CUTOFF=1130"
if not defined FLATTEN_TIME set "FLATTEN_TIME=1555"

rem ---------------- IB connection settings ----------------
if not defined IB_HOST      set "IB_HOST=127.0.0.1"
if not defined IB_PORT      set "IB_PORT=7497"
if not defined CLIENT_ID    set "CLIENT_ID=21"

rem 1 = detect signals only; no orders are submitted.
rem 0 = submit orders to the IBKR paper account.
if not defined DRY_RUN      set "DRY_RUN=1"

rem Python command. Change to py or a full python.exe path if necessary.
if not defined PYTHON_EXE   set "PYTHON_EXE=python"

set "SCRIPT=%~dp0ib_orb_paper.py"
set "EXTRA_ARGS="
if "%DRY_RUN%"=="1" set "EXTRA_ARGS=--dry-run"

if not exist "%SCRIPT%" (
    echo [ERROR] Cannot find:
    echo         %SCRIPT%
    echo.
    echo Put run_ib_orb_paper.bat and ib_orb_paper.py in the same folder.
    pause
    exit /b 1
)

where "%PYTHON_EXE%" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python command not found: %PYTHON_EXE%
    echo Set PYTHON_EXE to python, py, or the full path to python.exe.
    pause
    exit /b 1
)

echo ============================================================
echo IBKR PAPER ORB
echo ============================================================
echo Symbol       : %SYMBOL%
echo Cash         : %CASH%
echo Stop loss    : %SLP%%%
echo Take profit  : %TPP%%%
echo ORB end      : %RANGE_END% ET
echo Entry cutoff : %ENTRY_CUTOFF% ET
echo Flatten time : %FLATTEN_TIME% ET
echo IB endpoint  : %IB_HOST%:%IB_PORT%
echo Client ID    : %CLIENT_ID%
echo Dry run      : %DRY_RUN%
echo ============================================================
echo.

"%PYTHON_EXE%" "%SCRIPT%" ^
    --symbol "%SYMBOL%" ^
    --cash "%CASH%" ^
    --sl-pct "%SLP%" ^
    --tp-pct "%TPP%" ^
    --range-end-times "%RANGE_END%" ^
    --entry-cutoff "%ENTRY_CUTOFF%" ^
    --flatten-time "%FLATTEN_TIME%" ^
    --host "%IB_HOST%" ^
    --port "%IB_PORT%" ^
    --client-id "%CLIENT_ID%" ^
    %EXTRA_ARGS%

set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo ============================================================
echo ORB process ended. Exit code: %EXIT_CODE%
echo ============================================================
pause
exit /b %EXIT_CODE%
