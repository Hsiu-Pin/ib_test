@echo off

python -m venv .venv
call .venv\Scripts\activate.bat

python -m pip install --upgrade pip
python -m pip install backtesting yfinance pandas

echo.
echo Setup done.
pause