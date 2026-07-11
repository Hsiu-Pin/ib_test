"""
fetch_ib_5m_month_v2.py

從 Interactive Brokers TWS / IB Gateway 抓取指定標的、指定月份的 5-minute historical bars，
並存成本地 CSV。

預設連 Paper Trading TWS：127.0.0.1:7497

使用範例：
    python fetch_ib_5m_month.py --symbol SPY --year 2026 --month 6
    python fetch_ib_5m_month.py --symbol VOO --year 2025 --month 12 --out data
    python fetch_ib_5m_month.py --symbol QQQ --year 2024 --month 1 --useRTH 0

注意：
    1. TWS / IB Gateway 必須已開啟 API access。
    2. Paper Trading 預設 port 通常是 7497。
    3. Live Trading 預設 port 通常是 7496；本 script 預設拒絕 live port，除非加 --allow-live-port。
    4. 這支只抓 historical data，不下單。
"""

from __future__ import annotations

import argparse
import calendar
import csv
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from threading import Event, Lock, Thread
from typing import Optional

from ibapi.client import EClient
from ibapi.contract import Contract
from ibapi.wrapper import EWrapper


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7497          # Paper TWS default
DEFAULT_CLIENT_ID = 88
DEFAULT_EXCHANGE = "SMART"
DEFAULT_CURRENCY = "USD"
DEFAULT_SEC_TYPE = "STK"
DEFAULT_WHAT_TO_SHOW = "TRADES"
DEFAULT_BAR_SIZE = "5 mins"
DEFAULT_TIMEOUT_SEC = 120
DEFAULT_TIMEZONE = "US/Eastern"


@dataclass
class BarRecord:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    wap: float
    bar_count: int


class IBHistoricalApp(EWrapper, EClient):
    def __init__(self) -> None:
        EClient.__init__(self, self)

        self.connected_event = Event()
        self.historical_done_event = Event()
        self.error_event = Event()

        self.next_order_id: Optional[int] = None
        self.last_error_code: Optional[int] = None
        self.last_error_msg: Optional[str] = None

        self._bars: list[BarRecord] = []
        self._bars_lock = Lock()

    # ------------------------------------------------------------
    # IB connection callbacks
    # ------------------------------------------------------------
    def nextValidId(self, orderId: int) -> None:
        self.next_order_id = orderId
        print(f"nextValidId: {orderId}")
        self.connected_event.set()

    # ------------------------------------------------------------
    # 新版 / 舊版 ibapi error callback 兼容。
    #
    # 你之前遇過：
    #   TypeError: IBApp.error() takes from 4 to 5 positional arguments but 6 were given
    #
    # 所以這裡用 *args，兼容：
    #   error(reqId, errorCode, errorString)
    #   error(reqId, errorCode, errorString, advancedOrderRejectJson)
    #   error(reqId, errorTime, errorCode, errorString)
    #   error(reqId, errorTime, errorCode, errorString, advancedOrderRejectJson)
    # ------------------------------------------------------------
    def error(self, reqId, *args) -> None:  # noqa: N802 - IB callback name
        error_time = None
        error_code = None
        error_msg = None
        advanced_json = ""

        if len(args) == 2:
            error_code, error_msg = args
        elif len(args) == 3:
            # 可能是：errorCode, errorString, advancedJson
            # 也可能是：errorTime, errorCode, errorString
            if isinstance(args[1], int):
                error_time, error_code, error_msg = args
            else:
                error_code, error_msg, advanced_json = args
        elif len(args) >= 4:
            error_time, error_code, error_msg, advanced_json = args[:4]
        else:
            error_msg = str(args)

        self.last_error_code = int(error_code) if isinstance(error_code, int) else None
        self.last_error_msg = str(error_msg) if error_msg is not None else ""

        print(
            f"ERROR: reqId={reqId}, time={error_time}, "
            f"code={error_code}, msg={error_msg}"
        )

        if advanced_json:
            print(f"advancedOrderRejectJson={advanced_json}")

        # 常見非致命訊息：
        # 2104 / 2106 / 2158 = market data / historical data farm connection OK
        non_fatal_codes = {2104, 2106, 2158, 2108, 2107}

        # Historical data 相關錯誤通常要視為本次 request 失敗。
        fatal_historical_codes = {
            162,   # Historical market data Service error message
            165,   # Historical market Data Service query message
            200,   # No security definition has been found
            321,   # Error validating request
            354,   # Not subscribed to requested market data
            366,   # No historical data query found for ticker id
            420,   # Invalid real-time query / pacing violation
            10167, # Requested market data is not subscribed
        }

        if self.last_error_code in fatal_historical_codes:
            self.error_event.set()
            self.historical_done_event.set()
        elif self.last_error_code is not None and self.last_error_code not in non_fatal_codes:
            # 其他未知錯誤先記錄，但不一定立即中止，避免把 warning 當 fatal。
            pass

    # ------------------------------------------------------------
    # Historical data callbacks
    # ------------------------------------------------------------
    def historicalData(self, reqId, bar) -> None:  # noqa: N802 - IB callback name
        try:
            rec = BarRecord(
                date=str(bar.date),
                open=float(bar.open),
                high=float(bar.high),
                low=float(bar.low),
                close=float(bar.close),
                volume=float(bar.volume),
                wap=float(bar.wap),
                bar_count=int(bar.barCount),
            )
        except Exception as exc:
            print(f"Failed to parse bar: {exc}; raw bar={bar}")
            return

        with self._bars_lock:
            self._bars.append(rec)

    def historicalDataEnd(self, reqId: int, start: str, end: str) -> None:  # noqa: N802
        print(f"historicalDataEnd: reqId={reqId}, start={start}, end={end}")
        self.historical_done_event.set()

    def get_bars(self) -> list[BarRecord]:
        with self._bars_lock:
            return list(self._bars)


# ------------------------------------------------------------
# Utility functions
# ------------------------------------------------------------
def make_stock_contract(
    symbol: str,
    sec_type: str = DEFAULT_SEC_TYPE,
    exchange: str = DEFAULT_EXCHANGE,
    currency: str = DEFAULT_CURRENCY,
) -> Contract:
    contract = Contract()
    contract.symbol = symbol.upper().strip()
    contract.secType = sec_type.upper().strip()
    contract.exchange = exchange.upper().strip()
    contract.currency = currency.upper().strip()
    return contract


def wait_event(event: Event, timeout: Optional[float], label: str) -> bool:
    if timeout is None or timeout <= 0:
        timeout = DEFAULT_TIMEOUT_SEC

    ok = event.wait(timeout=timeout)
    if not ok:
        print(f"Timeout waiting for {label}. timeout={timeout}s")
    return ok


def wait_for_ib_ready(app: IBHistoricalApp, timeout: float = 10) -> bool:
    """
    避免 serverVersion 還是 None 時就送 request。
    你之前遇過：'<=' not supported between instances of 'int' and 'NoneType'
    所以這裡明確等 serverVersion ready。
    """
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            server_version = app.serverVersion()
        except Exception:
            server_version = None

        if app.isConnected() and isinstance(server_version, int) and server_version > 0:
            print(f"IB serverVersion: {server_version}")
            return True

        time.sleep(0.1)

    print("IB connection is not fully ready. serverVersion is still unavailable.")
    return False


def month_date_range(year: int, month: int) -> tuple[datetime, datetime, int]:
    if month < 1 or month > 12:
        raise ValueError("month 必須是 1~12")

    first_day = datetime(year, month, 1)
    days_in_month = calendar.monthrange(year, month)[1]

    if month == 12:
        next_month_first_day = datetime(year + 1, 1, 1)
    else:
        next_month_first_day = datetime(year, month + 1, 1)

    return first_day, next_month_first_day, days_in_month


def ib_end_datetime_for_month(year: int, month: int, timezone_name: str) -> str:
    """
    用下個月 1 號 00:00:00 當 endDateTime，duration 往前抓。
    後面再用 Python filter，只保留指定月份。
    """
    _, next_month_first_day, _ = month_date_range(year, month)
    return next_month_first_day.strftime("%Y%m%d 00:00:00") + f" {timezone_name}"


def parse_ib_bar_datetime(date_text: str) -> Optional[datetime]:
    """
    Parse IB historical bar datetime safely.

    IB may return several formats depending on TWS/API version and settings:
        20260626 09:40:00
        20260626 09:40:00 US/Eastern
        20260626
        1783696226  # epoch-like value in some cases

    We return a naive datetime in the exchange timezone context.
    For month filtering, timezone awareness is not needed because IB already labels
    the bar in the requested exchange timezone.
    """
    if date_text is None:
        return None

    text = str(date_text).strip()
    if not text:
        return None

    # Newer IB/TWS can append a timezone token, e.g.
    # "20260626 09:40:00 US/Eastern".
    # Keep only the date + time part for parsing.
    match = re.match(r"^(\d{8})(?:\s+(\d{2}:\d{2}:\d{2}))?(?:\s+[A-Za-z_]+/[A-Za-z_]+)?$", text)
    if match:
        yyyymmdd = match.group(1)
        hhmmss = match.group(2)
        cleaned = f"{yyyymmdd} {hhmmss}" if hhmmss else yyyymmdd

        for fmt in ("%Y%m%d %H:%M:%S", "%Y%m%d"):
            try:
                return datetime.strptime(cleaned, fmt)
            except ValueError:
                continue

    # Defensive fallback for epoch-like timestamps.
    # Avoid interpreting YYYYMMDD as epoch.
    if text.isdigit() and len(text) not in (8,):
        try:
            return datetime.fromtimestamp(int(text))
        except Exception:
            return None

    # Additional tolerant formats, just in case TWS output changes.
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue

    return None


def filter_bars_to_month(bars: list[BarRecord], year: int, month: int) -> list[BarRecord]:
    filtered: list[BarRecord] = []

    for bar in bars:
        dt = parse_ib_bar_datetime(bar.date)
        if dt is None:
            # 解析不到就先保留，避免誤刪資料；但會提醒。
            print(f"Warning: cannot parse bar datetime: {bar.date}")
            filtered.append(bar)
            continue

        if dt.year == year and dt.month == month:
            filtered.append(bar)

    return filtered


def safe_filename(text: str) -> str:
    text = text.strip().upper()
    return re.sub(r"[^A-Z0-9_.-]+", "_", text)


def save_bars_csv(bars: list[BarRecord], filepath: str) -> None:
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "wap",
            "barCount",
        ])

        for bar in bars:
            writer.writerow([
                bar.date,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                bar.wap,
                bar.bar_count,
            ])


def build_output_path(args) -> str:
    symbol = safe_filename(args.symbol)
    sec_type = safe_filename(args.secType)
    filename = f"{symbol}_{sec_type}_5m_{args.year}_{args.month:02d}.csv"
    return os.path.join(args.out, filename)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch one calendar month of IBKR 5-minute historical bars and save as CSV."
    )

    parser.add_argument("--symbol", required=True, help="標的代號，例如 SPY、VOO、AAPL、QQQ")
    parser.add_argument("--year", type=int, required=True, help="年份，例如 2026")
    parser.add_argument("--month", type=int, required=True, help="月份，1~12")

    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--clientId", type=int, default=DEFAULT_CLIENT_ID)

    parser.add_argument("--secType", default=DEFAULT_SEC_TYPE, help="預設 STK；美股 ETF 也用 STK")
    parser.add_argument("--exchange", default=DEFAULT_EXCHANGE, help="預設 SMART")
    parser.add_argument("--currency", default=DEFAULT_CURRENCY, help="預設 USD")
    parser.add_argument("--whatToShow", default=DEFAULT_WHAT_TO_SHOW, help="預設 TRADES，也可用 MIDPOINT/BID/ASK")
    parser.add_argument("--useRTH", type=int, default=1, choices=[0, 1], help="1=只抓正常交易時段；0=包含盤前盤後")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE, help="預設 US/Eastern")

    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SEC)
    parser.add_argument("--out", default="data", help="輸出資料夾，預設 data")
    parser.add_argument(
        "--allow-live-port",
        action="store_true",
        help="允許連到非 7497 port。避免不小心連到真實交易 port。這支只抓資料，不下單，但仍預設保守。",
    )

    return parser.parse_args()


# ------------------------------------------------------------
# Main flow
# ------------------------------------------------------------
def main() -> None:
    args = parse_args()

    if args.port != 7497 and not args.allow_live_port:
        print(f"Refusing to connect to port={args.port}.")
        print("Paper TWS default is 7497. If you really want this port, add --allow-live-port.")
        return

    first_day, next_month_first_day, days_in_month = month_date_range(args.year, args.month)

    # 多抓 3 天 buffer，最後再 filter 到指定月份。
    # 這樣避免 endDateTime / 時區 / 交易日邊界導致少資料。
    duration_days = days_in_month + 3
    duration_str = f"{duration_days} D"
    end_datetime = ib_end_datetime_for_month(args.year, args.month, args.timezone)
    output_path = build_output_path(args)

    print("=" * 72)
    print("IBKR 5-minute Historical Data Fetcher")
    print("=" * 72)
    print(f"symbol       : {args.symbol.upper()}")
    print(f"secType      : {args.secType.upper()}")
    print(f"exchange     : {args.exchange.upper()}")
    print(f"currency     : {args.currency.upper()}")
    print(f"month        : {args.year}-{args.month:02d}")
    print(f"target range : {first_day.date()} to {next_month_first_day.date()} exclusive")
    print(f"endDateTime  : {end_datetime}")
    print(f"durationStr  : {duration_str}")
    print(f"barSize      : {DEFAULT_BAR_SIZE}")
    print(f"whatToShow   : {args.whatToShow}")
    print(f"useRTH       : {args.useRTH}")
    print(f"output       : {output_path}")
    print("=" * 72)

    app = IBHistoricalApp()
    api_thread: Optional[Thread] = None

    try:
        print("Connecting to IBKR...")
        app.connect(args.host, args.port, clientId=args.clientId)

        api_thread = Thread(target=app.run, daemon=True)
        api_thread.start()

        if not wait_event(app.connected_event, timeout=10, label="nextValidId"):
            print("Connection failed. Make sure TWS / IB Gateway API is enabled.")
            return

        if not wait_for_ib_ready(app, timeout=10):
            return

        contract = make_stock_contract(
            symbol=args.symbol,
            sec_type=args.secType,
            exchange=args.exchange,
            currency=args.currency,
        )

        req_id = 1

        print("Requesting historical data...")
        app.reqHistoricalData(
            reqId=req_id,
            contract=contract,
            endDateTime=end_datetime,
            durationStr=duration_str,
            barSizeSetting=DEFAULT_BAR_SIZE,
            whatToShow=args.whatToShow.upper(),
            useRTH=args.useRTH,
            formatDate=1,
            keepUpToDate=False,
            chartOptions=[],
        )

        wait_event(app.historical_done_event, timeout=args.timeout, label="historicalDataEnd")

        raw_bars = app.get_bars()
        month_bars = filter_bars_to_month(raw_bars, args.year, args.month)

        if app.error_event.is_set() and not raw_bars:
            print("Historical data request failed.")
            print(f"Last error: code={app.last_error_code}, msg={app.last_error_msg}")
            return

        if not raw_bars:
            print("No bars received.")
            print("Possible reasons:")
            print("1. No historical data permission for this symbol / market")
            print("2. Wrong contract definition")
            print("3. Target month has no available data")
            print("4. TWS / Gateway historical data farm issue")
            return

        if not month_bars:
            print(f"Received {len(raw_bars)} raw bars, but none matched {args.year}-{args.month:02d}.")
            print("Try checking timezone, endDateTime, or symbol.")
            return

        save_bars_csv(month_bars, output_path)

        first_bar = month_bars[0]
        last_bar = month_bars[-1]

        print()
        print("Saved CSV successfully.")
        print(f"raw bars      : {len(raw_bars)}")
        print(f"month bars    : {len(month_bars)}")
        print(f"first bar     : {first_bar.date}, close={first_bar.close}")
        print(f"last bar      : {last_bar.date}, close={last_bar.close}")
        print(f"filepath      : {output_path}")

    except Exception as exc:
        print(f"Exception occurred: {exc}")

    finally:
        print("Final cleanup...")
        try:
            if app.isConnected():
                app.disconnect()
        except Exception as exc:
            print(f"disconnect failed: {exc}")

        if api_thread is not None:
            api_thread.join(timeout=2)

        print("Done.")


if __name__ == "__main__":
    main()
