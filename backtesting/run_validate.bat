@echo off
setlocal

REM ============================================================
REM True OOS ORB validation runner
REM 1. 用 2025 train 做 broad optimization
REM 2. 讀取 2025 top20，直接套到 2026H1 validation
REM    注意：第二步不會重新最佳化參數
REM ============================================================

set SYMBOL=TSLA
set sch_st_yr=2024
set sch_st_mn=1
set sch_ed_yr=2025
set sch_ed_mn=12

set vld_st_yr=2026
set vld_st_mn=1
set vld_ed_yr=2026
set vld_ed_mn=6

set IB_DATA_DIR=./ib_data_tsla
set CASH=10000
set COMMISSION_MODEL=ibkr_fixed
set SPREAD=0.0002

set OUTTAG=v1

echo ============================================================
echo 1. Train optimization: 2025 broad search
echo ============================================================
python my_optimize_orb.py --symbol %SYMBOL% ^
--start-year %sch_st_yr% --start-month %sch_st_mn% ^
--end-year %sch_ed_yr% --end-month %sch_ed_mn% ^
--ib-data-dir %IB_DATA_DIR% ^
--cash %CASH% ^
--commission-model %COMMISSION_MODEL% ^
--spread %SPREAD% ^
--range-end-times 945,1000 ^
--sl-start-pct    0.0  ^
--sl-end-pct      4.0  ^
--sl-step-pct     0.25 ^
--tp-start-pct    1.0  ^
--tp-end-pct      6.0  ^
--tp-step-pct     0.25 ^
--sort-by rank_metric  ^
--top-n 20 ^
--output-tag %OUTTAG% 

if errorlevel 1 goto error

set TRAIN_TOP_CSV=./result/orb_top20_%SYMBOL%_%sch_st_yr%_%sch_st_mn%_to_%sch_ed_yr%_%sch_ed_mn%_5m_%OUTTAG%.csv

echo.
echo Train top CSV:
echo %TRAIN_TOP_CSV%
if not exist %TRAIN_TOP_CSV% goto missing_train_csv

echo ============================================================
echo 2. True OOS validation: apply 2025 top%TOP_N% to 2026H1
echo    No re-optimization here.
echo ============================================================
python validate_orb_train_topn.py ^
--train-top-csv %TRAIN_TOP_CSV% --symbol %SYMBOL% --val-start-year %vld_st_yr% --val-start-month %vld_st_mn% --val-end-year %vld_ed_yr% --val-end-month %vld_ed_mn% --ib-data-dir %IB_DATA_DIR% ^
--cash %CASH% --commission-model %COMMISSION_MODEL% --spread %SPREAD% --top-n 20 --sort-by val_rank_metric --output-tag _5m_%OUTTAG%_val
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
