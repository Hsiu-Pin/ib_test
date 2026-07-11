@echo off
setlocal

REM ============================================================
REM True OOS ORB validation runner
REM 1. 用 2025 train 做 broad optimization
REM 2. 讀取 2025 top20，直接套到 2026H1 validation
REM    注意：第二步不會重新最佳化參數
REM ============================================================
set SYMBOL=TSLA
set IB_DATA_DIR=./ib_data_tsla
set COMMISSION_MODEL=ibkr_fixed
set SPREAD=0.0002
set CASH=10000
set TOP_N=20


set TRAIN_TOP_CSV=./result/orb_top20_TSLA_2025_01_to_2025_12_5m_broad_train_2025.csv

echo.
echo Train top CSV:
echo %TRAIN_TOP_CSV%
if not exist %TRAIN_TOP_CSV% goto missing_train_csv

echo ============================================================
echo 2. True OOS validation: apply 2025 top%TOP_N% to 2026H1
echo    No re-optimization here.
echo ============================================================
python validate_orb_train_topn.py --train-top-csv %TRAIN_TOP_CSV% --symbol %SYMBOL% --val-start-year 2024 --val-start-month 1 --val-end-year 2024 --val-end-month 12 --ib-data-dir %IB_DATA_DIR% --cash %CASH% --commission-model %COMMISSION_MODEL% --spread %SPREAD% --top-n %TOP_N% --sort-by val_rank_metric --output-tag train2025_top%TOP_N%_val2026H1
if errorlevel 1 goto error

echo.
echo Done. Check the ./result folder.
goto end

:missing_train_csv
echo.
echo Missing train top CSV:
echo %TRAIN_TOP_CSV%
goto error

:error
echo.
echo Error occurred. Script stopped.

:end
endlocal
pause
