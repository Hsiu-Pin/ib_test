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
set CASH=10000
set COMMISSION_MODEL=ibkr_fixed
set SPREAD=0.0002

set OUTTAG=final

echo ============================================================
echo Target Settings
echo ============================================================

set tar_time=945
set tar_sl=3.6
set tar_tp=6.0

echo %TRAIN_TOP_CSV%


echo ============================================================
echo 1. Train optimization: 2024 broad search
echo ============================================================

set sch_st_yr=2024
set sch_st_mn=01
set sch_ed_yr=2024
set sch_ed_mn=12

python my_optimize_orb.py --symbol %SYMBOL% --start-year %sch_st_yr% --start-month %sch_st_mn% --end-year %sch_ed_yr% --end-month %sch_ed_mn% ^
--ib-data-dir %IB_DATA_DIR% --cash %CASH% --commission-model %COMMISSION_MODEL% --spread %SPREAD% ^
--range-end-times  %tar_time%  ^
--sl-start-pct     %tar_sl%    ^
--sl-end-pct       %tar_sl%    ^
--sl-step-pct      0.25        ^
--tp-start-pct     %tar_tp%    ^
--tp-end-pct       %tar_tp%    ^
--tp-step-pct      0.25        ^
--sort-by rank_metric          ^
--top-n 1 ^
--output-tag %OUTTAG% 



echo ============================================================
echo 2. Train optimization: 2025 broad search
echo ============================================================

set sch_st_yr=2025
set sch_st_mn=01
set sch_ed_yr=2025
set sch_ed_mn=12

python my_optimize_orb.py --symbol %SYMBOL% --start-year %sch_st_yr% --start-month %sch_st_mn% --end-year %sch_ed_yr% --end-month %sch_ed_mn% ^
--ib-data-dir %IB_DATA_DIR% --cash %CASH% --commission-model %COMMISSION_MODEL% --spread %SPREAD% ^
--range-end-times  %tar_time%  ^
--sl-start-pct     %tar_sl%    ^
--sl-end-pct       %tar_sl%    ^
--sl-step-pct      0.25        ^
--tp-start-pct     %tar_tp%    ^
--tp-end-pct       %tar_tp%    ^
--tp-step-pct      0.25        ^
--sort-by rank_metric  ^
--top-n 1 ^
--output-tag %OUTTAG% 

echo ============================================================
echo 3. Train optimization: 2026 broad search
echo ============================================================

set sch_st_yr=2026
set sch_st_mn=01
set sch_ed_yr=2026
set sch_ed_mn=06

python my_optimize_orb.py --symbol %SYMBOL% --start-year %sch_st_yr% --start-month %sch_st_mn% --end-year %sch_ed_yr% --end-month %sch_ed_mn% ^
--ib-data-dir %IB_DATA_DIR% --cash %CASH% --commission-model %COMMISSION_MODEL% --spread %SPREAD% ^
--range-end-times  %tar_time%  ^
--sl-start-pct     %tar_sl%    ^
--sl-end-pct       %tar_sl%    ^
--sl-step-pct      0.25        ^
--tp-start-pct     %tar_tp%    ^
--tp-end-pct       %tar_tp%    ^
--tp-step-pct      0.25        ^
--sort-by rank_metric  ^
--top-n 1 ^
--output-tag %OUTTAG% 

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
