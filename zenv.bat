@echo off

python -m venv .venv
call .venv\Scripts\activate.bat

python -m pip install --upgrade pip
python -m pip install backtesting yfinance pandas

cd C:\TWS API\source\pythonclient
python -m pip install .
cd C:\Users\hsiup\ib_test
python -m pip show ibapi
python -c "import ibapi; print('ibapi ok')"

echo.
echo Setup done.
pause