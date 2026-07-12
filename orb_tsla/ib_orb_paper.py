#!/usr/bin/env python3
"""
IBKR TWS API paper-trading ORB bot (long only).

Strategy
--------
1. Build the opening range from 09:30 ET up to --range-end-times.
2. After the range ends, wait for a COMPLETE 5-minute bar to close above
   the opening-range high.
3. Buy at market immediately after that bar closes.
4. Attach take-profit and stop-loss sell orders.
5. Do not open a new position after --entry-cutoff (default 12:00 ET).
6. Flatten any remaining position at --flatten-time (default 15:55 ET).

Safety
------
- Only accepts the standard paper ports: TWS 7497 or IB Gateway 4002.
- Refuses accounts whose account ID does not begin with "D" (normally DU...).
- Refuses to trade with delayed/frozen market data.
- Refuses to trade if the symbol already has a position in the selected account.
- One entry at most per trading day.

Examples
--------
python ib_orb_paper.py ^
  --symbol TSLA ^
  --cash 10000 ^
  --sl-pct 0.0325 ^
  --tp-pct 0.04 ^
  --range-end-times 945

Percent arguments accept either decimal form or percentage-point form:
  --sl-pct 0.0325   -> 3.25%
  --sl-pct 3.25     -> 3.25%
  --sl-pct 3.25%    -> 3.25%
"""

from __future__ import annotations

import argparse
import math
import sys
import threading
import time as time_module
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo

try:
    from ibapi.client import EClient
    from ibapi.contract import Contract
    from ibapi.order import Order
    from ibapi.wrapper import EWrapper
except ImportError as exc:
    raise SystemExit(
        "Cannot import ibapi. Install the official IBKR TWS API Python package "
        "and run its setup.py before starting this script."
    ) from exc


NY = ZoneInfo("America/New_York")
PAPER_PORTS = {7497, 4002}
OPEN_TIME = time(9, 30)
BAR_MINUTES = 5

CONTRACT_REQ_ID = 1001
MARKET_DATA_REQ_ID = 1002
HISTORICAL_REQ_ID = 2001

BENIGN_ERROR_CODES = {
    2104,  # Market data farm connection is OK
    2106,  # Historical data farm connection is OK
    2107,  # Historical data farm connection inactive
    2108,  # Market data farm connection inactive
    2158,  # Sec-def data farm connection is OK
}
MARKET_DATA_PERMISSION_ERRORS = {354, 10167, 10168}


@dataclass(frozen=True)
class Candle:
    start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=BAR_MINUTES)


@dataclass
class Config:
    symbol: str
    cash: float
    sl_pct: float
    tp_pct: float
    range_end: time
    entry_cutoff: time
    flatten_time: time
    host: str
    port: int
    client_id: int
    account: Optional[str]
    primary_exchange: Optional[str]
    cash_buffer_pct: float
    dry_run: bool


class ORBPaperTrader(EWrapper, EClient):
    def __init__(self, cfg: Config) -> None:
        EWrapper.__init__(self)
        EClient.__init__(self, self)

        self.cfg = cfg
        self.state_lock = threading.RLock()
        self.connected_event = threading.Event()
        self.stop_event = threading.Event()
        self.fatal_reason: Optional[str] = None

        self.next_order_id: Optional[int] = None
        self.managed_accounts: List[str] = []
        self.selected_account: Optional[str] = None
        self.paper_guard_ok = False

        self.base_contract = self._make_stock_contract()
        self.contract_candidates: List[object] = []
        self.contract: Optional[Contract] = None
        self.min_tick = Decimal("0.01")
        self.requests_started = False

        self.market_data_type: Optional[int] = None
        self.market_data_ok = False
        self.last_trade_price: Optional[float] = None

        self.initial_positions_loading = True
        self.initial_symbol_position = Decimal("0")
        self.current_symbol_position = Decimal("0")
        self.position_snapshot_complete = False

        self.initial_bars: Dict[datetime, Candle] = {}
        self.history_ready = False
        self.live_bar: Optional[Candle] = None

        self.trade_date: Optional[date] = None
        self.orb_high: Optional[float] = None
        self.orb_low: Optional[float] = None
        self.orb_bar_count = 0
        self.orb_finalized = False
        self.historical_breakout_seen = False
        self.traded_today = False
        self.cutoff_logged_for_date: Optional[date] = None

        self.entry_order_id: Optional[int] = None
        self.tp_order_id: Optional[int] = None
        self.sl_order_id: Optional[int] = None
        self.flatten_order_id: Optional[int] = None
        self.entry_qty = 0
        self.entry_trigger_price: Optional[float] = None
        self.entry_fill_price: Optional[float] = None
        self.exit_prices_adjusted = False
        self.in_position = False
        self.flatten_sent = False
        self.order_objects: Dict[int, Order] = {}

    # ------------------------------------------------------------------
    # Logging and lifecycle
    # ------------------------------------------------------------------
    @staticmethod
    def log(message: str) -> None:
        now = datetime.now(NY)
        print(f"[{now:%Y-%m-%d %H:%M:%S %Z}] {message}", flush=True)

    def fatal(self, message: str) -> None:
        self.fatal_reason = message
        self.log(f"FATAL: {message}")
        self.stop_event.set()

    def start_requests(self) -> None:
        self.log(
            f"Connected to {self.cfg.host}:{self.cfg.port}, clientId={self.cfg.client_id}."
        )
        self.reqManagedAccts()
        self.reqPositions()
        self.reqContractDetails(CONTRACT_REQ_ID, self.base_contract)

    def shutdown(self) -> None:
        self.stop_event.set()
        for call in (
            lambda: self.cancelHistoricalData(HISTORICAL_REQ_ID),
            lambda: self.cancelMktData(MARKET_DATA_REQ_ID),
            self.cancelPositions,
        ):
            try:
                call()
            except Exception:
                pass
        try:
            if self.isConnected():
                self.disconnect()
        except Exception:
            pass

    def _clock_loop(self) -> None:
        while not self.stop_event.wait(1.0):
            try:
                self._check_time_controls()
            except Exception as exc:
                self.log(f"Clock monitor error: {exc!r}")

    # ------------------------------------------------------------------
    # IB connection callbacks
    # ------------------------------------------------------------------
    def nextValidId(self, orderId: int) -> None:  # noqa: N802 (IB callback name)
        with self.state_lock:
            self.next_order_id = orderId
        self.connected_event.set()
        self.log(f"Next valid order ID: {orderId}")

    def managedAccounts(self, accountsList: str) -> None:  # noqa: N802
        accounts = [item.strip() for item in accountsList.split(",") if item.strip()]
        with self.state_lock:
            self.managed_accounts = accounts

            if self.cfg.account:
                if self.cfg.account not in accounts:
                    self.fatal(
                        f"Requested account {self.cfg.account!r} is not in managed accounts: "
                        f"{accounts}"
                    )
                    return
                selected = self.cfg.account
            elif len(accounts) == 1:
                selected = accounts[0]
            elif len(accounts) == 0:
                self.fatal("IB returned no managed accounts.")
                return
            else:
                self.fatal(
                    "Multiple accounts are available. Restart with --account ACCOUNT_ID. "
                    f"Available accounts: {accounts}"
                )
                return

            self.selected_account = selected
            if not selected.upper().startswith("D"):
                self.fatal(
                    f"Account {selected} does not look like an IBKR paper account. "
                    "This script intentionally refuses live accounts."
                )
                return

            self.paper_guard_ok = True
            self.log(f"Paper account selected: {selected}")

    def connectionClosed(self) -> None:  # noqa: N802
        self.log("IB connection closed.")
        self.stop_event.set()

    # Supports both older and newer ibapi callback signatures.
    def error(self, reqId, *args) -> None:  # noqa: N802, ANN001
        error_time = None
        error_code = None
        error_string = ""
        advanced_reject = ""

        if len(args) >= 4:
            error_time, error_code, error_string, advanced_reject = args[:4]
        elif len(args) == 3:
            # Usually old form: errorCode, errorString, advancedOrderRejectJson
            error_code, error_string, advanced_reject = args
        elif len(args) == 2:
            error_code, error_string = args
        elif len(args) == 1:
            self.log(f"IB error: reqId={reqId}, detail={args[0]!r}")
            return
        else:
            self.log(f"IB error: reqId={reqId}, no details")
            return

        try:
            code_int = int(error_code)
        except (TypeError, ValueError):
            # Newer form may be (errorTime, errorCode, errorString) if the
            # advanced reject JSON parameter is omitted.
            if len(args) == 3:
                error_time, error_code, error_string = args
                advanced_reject = ""
                try:
                    code_int = int(error_code)
                except (TypeError, ValueError):
                    code_int = -1
            else:
                code_int = -1

        if code_int in BENIGN_ERROR_CODES:
            self.log(f"IB status {code_int}: {error_string}")
            return

        extra = f", errorTime={error_time}" if error_time is not None else ""
        if advanced_reject:
            extra += f", reject={advanced_reject}"
        self.log(
            f"IB error {code_int}: reqId={reqId}, message={error_string}{extra}"
        )

        if code_int in MARKET_DATA_PERMISSION_ERRORS:
            with self.state_lock:
                self.market_data_ok = False
            self.log("Trading disabled: live market-data permission is unavailable.")
        elif code_int == 1100:
            with self.state_lock:
                self.market_data_ok = False
            self.log("Trading disabled while IB connectivity is lost.")
        elif code_int in {502, 503, 504}:
            self.fatal(f"IB connection failure ({code_int}): {error_string}")

    # ------------------------------------------------------------------
    # Contract and market data
    # ------------------------------------------------------------------
    def _make_stock_contract(self) -> Contract:
        contract = Contract()
        contract.symbol = self.cfg.symbol
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.currency = "USD"
        if self.cfg.primary_exchange:
            contract.primaryExchange = self.cfg.primary_exchange
        return contract

    def contractDetails(self, reqId, contractDetails) -> None:  # noqa: N802, ANN001
        if reqId == CONTRACT_REQ_ID:
            self.contract_candidates.append(contractDetails)

    def contractDetailsEnd(self, reqId: int) -> None:  # noqa: N802
        if reqId != CONTRACT_REQ_ID:
            return
        if not self.contract_candidates:
            self.fatal(
                f"No matching US stock contract was found for {self.cfg.symbol}."
            )
            return

        # Prefer an exact symbol/USD stock candidate; the optional
        # --primary-exchange argument resolves ambiguous tickers.
        candidate = self.contract_candidates[0]
        for details in self.contract_candidates:
            c = details.contract
            if (
                c.symbol.upper() == self.cfg.symbol.upper()
                and c.secType == "STK"
                and c.currency == "USD"
            ):
                candidate = details
                break

        self.contract = candidate.contract
        try:
            tick = Decimal(str(candidate.minTick))
            if tick > 0:
                self.min_tick = tick
        except Exception:
            self.min_tick = Decimal("0.01")

        self.log(
            "Qualified contract: "
            f"{self.contract.symbol} conId={self.contract.conId} "
            f"exchange={self.contract.exchange} "
            f"primaryExchange={getattr(self.contract, 'primaryExchange', '')} "
            f"minTick={self.min_tick}"
        )
        self._start_market_requests()

    def _start_market_requests(self) -> None:
        with self.state_lock:
            if self.requests_started or self.contract is None:
                return
            self.requests_started = True

        # Force a live-data request. Entry logic requires marketDataType == 1.
        self.reqMarketDataType(1)
        self.reqMktData(
            MARKET_DATA_REQ_ID,
            self.contract,
            "",
            False,
            False,
            [],
        )

        # formatDate=2 returns epoch timestamps for intraday bars.
        # keepUpToDate=True sends updates through historicalDataUpdate().
        self.reqHistoricalData(
            HISTORICAL_REQ_ID,
            self.contract,
            "",
            "1 D",
            "5 mins",
            "TRADES",
            1,
            2,
            True,
            [],
        )
        self.log("Requested live market data and streaming 5-minute bars.")

    def marketDataType(self, reqId: int, marketDataType: int) -> None:  # noqa: N802
        if reqId != MARKET_DATA_REQ_ID:
            return
        names = {1: "live", 2: "frozen", 3: "delayed", 4: "delayed-frozen"}
        with self.state_lock:
            self.market_data_type = marketDataType
            self.market_data_ok = marketDataType == 1
        self.log(
            f"Market data type: {marketDataType} "
            f"({names.get(marketDataType, 'unknown')})."
        )
        if marketDataType != 1:
            self.log("No entries will be sent unless market data type becomes live (1).")

    def tickPrice(self, reqId, tickType, price, attrib) -> None:  # noqa: N802, ANN001
        if reqId != MARKET_DATA_REQ_ID or price is None or price <= 0:
            return
        # Tick types 4=LAST, 68=DELAYED_LAST. Delayed data is still rejected by
        # marketDataType(), but retaining the latest price helps diagnostics.
        if tickType in {4, 68}:
            with self.state_lock:
                self.last_trade_price = float(price)

    # ------------------------------------------------------------------
    # Position callbacks and safety
    # ------------------------------------------------------------------
    def position(self, account, contract, position, avgCost) -> None:  # noqa: N802, ANN001
        if contract.secType != "STK" or contract.symbol.upper() != self.cfg.symbol:
            return
        if self.selected_account and account != self.selected_account:
            return

        pos = Decimal(str(position))
        with self.state_lock:
            self.current_symbol_position = pos
            if self.initial_positions_loading:
                self.initial_symbol_position = pos
            self.in_position = pos != 0

    def positionEnd(self) -> None:  # noqa: N802
        with self.state_lock:
            self.initial_positions_loading = False
            self.position_snapshot_complete = True
            initial_pos = self.initial_symbol_position

        if initial_pos != 0:
            self.fatal(
                f"Pre-existing {self.cfg.symbol} position detected: {initial_pos}. "
                "Close it or use a separate paper account before running this bot."
            )
        else:
            self.log(f"Position safety check passed: no existing {self.cfg.symbol} position.")

    # ------------------------------------------------------------------
    # Historical/live bar callbacks
    # ------------------------------------------------------------------
    def historicalData(self, reqId, bar) -> None:  # noqa: N802, ANN001
        if reqId != HISTORICAL_REQ_ID:
            return
        try:
            candle = self._to_candle(bar)
        except Exception as exc:
            self.log(f"Could not parse historical bar {getattr(bar, 'date', None)!r}: {exc}")
            return
        self.initial_bars[candle.start] = candle

    def historicalDataEnd(self, reqId, start, end) -> None:  # noqa: N802, ANN001
        if reqId != HISTORICAL_REQ_ID:
            return

        bars = sorted(self.initial_bars.values(), key=lambda item: item.start)
        if not bars:
            self.fatal("IB returned no historical bars for the current request.")
            return

        # The last keepUpToDate bar is normally the current unfinished bar.
        for candle in bars[:-1]:
            self._process_closed_bar(candle, historical=True)

        with self.state_lock:
            self.live_bar = bars[-1]
            self.history_ready = True

        self.log(
            f"Initial bar load complete: {len(bars)} bars. "
            f"Current bar starts {bars[-1].start:%Y-%m-%d %H:%M ET}."
        )

    def historicalDataUpdate(self, reqId, bar) -> None:  # noqa: N802, ANN001
        if reqId != HISTORICAL_REQ_ID:
            return
        try:
            candle = self._to_candle(bar)
        except Exception as exc:
            self.log(f"Could not parse live bar {getattr(bar, 'date', None)!r}: {exc}")
            return

        closed_bar: Optional[Candle] = None
        with self.state_lock:
            if not self.history_ready:
                self.initial_bars[candle.start] = candle
                return

            if self.live_bar is None:
                self.live_bar = candle
                return

            if candle.start == self.live_bar.start:
                self.live_bar = candle
                return

            if candle.start > self.live_bar.start:
                closed_bar = self.live_bar
                self.live_bar = candle
            else:
                # Ignore stale/out-of-order updates.
                return

        if closed_bar is not None:
            self._process_closed_bar(closed_bar, historical=False)

    @staticmethod
    def _bar_timestamp_to_et(raw_value) -> datetime:  # noqa: ANN001
        raw = str(raw_value).strip()
        if raw.isdigit():
            # formatDate=2 intraday bars normally arrive as Unix epoch seconds.
            value = int(raw)
            if value > 10_000_000_000:  # defensive support for milliseconds
                value //= 1000
            return datetime.fromtimestamp(value, tz=NY)

        normalized = raw.replace("  ", " ")
        formats: Sequence[str] = (
            "%Y%m%d %H:%M:%S",
            "%Y%m%d-%H:%M:%S",
            "%Y%m%d %H:%M:%S %Z",
        )
        for fmt in formats:
            try:
                parsed = datetime.strptime(normalized, fmt)
                return parsed.replace(tzinfo=NY)
            except ValueError:
                continue
        raise ValueError(f"unsupported IB bar timestamp: {raw!r}")

    def _to_candle(self, bar) -> Candle:  # noqa: ANN001
        return Candle(
            start=self._bar_timestamp_to_et(bar.date),
            open=float(bar.open),
            high=float(bar.high),
            low=float(bar.low),
            close=float(bar.close),
            volume=float(bar.volume),
        )

    # ------------------------------------------------------------------
    # Strategy state machine
    # ------------------------------------------------------------------
    def _reset_for_date(self, session_date: date) -> None:
        with self.state_lock:
            if self.trade_date == session_date:
                return

            if self.current_symbol_position != 0 or self.in_position:
                self.fatal(
                    "A new trading date began while a position was still open. "
                    "Manual review is required."
                )
                return

            self.trade_date = session_date
            self.orb_high = None
            self.orb_low = None
            self.orb_bar_count = 0
            self.orb_finalized = False
            self.historical_breakout_seen = False
            self.traded_today = False
            self.cutoff_logged_for_date = None
            self.entry_order_id = None
            self.tp_order_id = None
            self.sl_order_id = None
            self.flatten_order_id = None
            self.entry_qty = 0
            self.entry_trigger_price = None
            self.entry_fill_price = None
            self.exit_prices_adjusted = False
            self.flatten_sent = False
            self.order_objects.clear()

        self.log(f"New ORB session: {session_date.isoformat()}")

    def _process_closed_bar(self, candle: Candle, historical: bool) -> None:
        self._reset_for_date(candle.start.date())
        if self.stop_event.is_set():
            return

        bar_start_t = candle.start.time().replace(tzinfo=None)
        bar_end_t = candle.end.time().replace(tzinfo=None)

        # Opening-range bars are [09:30, range_end).
        if OPEN_TIME <= bar_start_t < self.cfg.range_end:
            with self.state_lock:
                self.orb_high = (
                    candle.high
                    if self.orb_high is None
                    else max(self.orb_high, candle.high)
                )
                self.orb_low = (
                    candle.low
                    if self.orb_low is None
                    else min(self.orb_low, candle.low)
                )
                self.orb_bar_count += 1

            self.log(
                f"{'HIST' if historical else 'LIVE'} closed bar "
                f"{candle.start:%H:%M}-{candle.end:%H:%M}: "
                f"O={candle.open:.2f} H={candle.high:.2f} "
                f"L={candle.low:.2f} C={candle.close:.2f}"
            )

        if (
            not self.orb_finalized
            and bar_end_t >= self.cfg.range_end
            and self.orb_bar_count > 0
        ):
            with self.state_lock:
                self.orb_finalized = True
                orb_high = self.orb_high
                orb_low = self.orb_low
                count = self.orb_bar_count
            self.log(
                f"ORB finalized at {self.cfg.range_end:%H:%M}: "
                f"high={orb_high:.2f}, low={orb_low:.2f}, bars={count}."
            )

        if not self.orb_finalized or self.orb_high is None:
            return
        if bar_start_t < self.cfg.range_end:
            return

        broke_high = candle.close > self.orb_high
        if not broke_high:
            if not historical:
                self.log(
                    f"LIVE closed bar {candle.start:%H:%M}-{candle.end:%H:%M}: "
                    f"close={candle.close:.2f}; no breakout above {self.orb_high:.2f}."
                )
            return

        if historical:
            if not self.historical_breakout_seen:
                self.historical_breakout_seen = True
                self.log(
                    f"Historical breakout already occurred at {candle.end:%H:%M} "
                    f"(close={candle.close:.2f} > ORB high={self.orb_high:.2f}). "
                    "The bot will not chase this missed setup today."
                )
            return

        if self.historical_breakout_seen:
            self.log("Breakout ignored because today's first breakout was already missed.")
            return
        if bar_end_t > self.cfg.entry_cutoff:
            self.log(
                f"Breakout ignored: bar closed at {bar_end_t:%H:%M}, after entry "
                f"cutoff {self.cfg.entry_cutoff:%H:%M}."
            )
            return

        self.log(
            f"BREAKOUT: {candle.start:%H:%M}-{candle.end:%H:%M} close "
            f"{candle.close:.2f} > ORB high {self.orb_high:.2f}."
        )
        self._submit_entry(candle.close)

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------
    def _allocate_order_ids(self, count: int) -> List[int]:
        with self.state_lock:
            if self.next_order_id is None:
                raise RuntimeError("nextValidId has not been received")
            start = self.next_order_id
            self.next_order_id += count
        return list(range(start, start + count))

    def _round_to_tick(self, price: float) -> float:
        p = Decimal(str(price))
        ticks = (p / self.min_tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        rounded = ticks * self.min_tick
        return float(rounded)

    def _base_order(self) -> Order:
        order = Order()
        order.tif = "DAY"
        order.outsideRth = False
        order.account = self.selected_account or ""
        order.orderRef = f"ORB_{self.cfg.symbol}_{self.trade_date or 'NA'}"
        return order

    def _submit_entry(self, trigger_price: float) -> None:
        with self.state_lock:
            if self.traded_today:
                self.log("Entry ignored: this strategy has already traded today.")
                return
            if not self.paper_guard_ok:
                self.log("Entry blocked: paper-account validation is incomplete.")
                return
            if not self.position_snapshot_complete:
                self.log("Entry blocked: initial position safety check is incomplete.")
                return
            if self.initial_symbol_position != 0 or self.current_symbol_position != 0:
                self.log("Entry blocked: a position already exists in this symbol.")
                return
            if not self.market_data_ok or self.market_data_type != 1:
                self.log("Entry blocked: IB market data is not confirmed live.")
                return
            if self.contract is None:
                self.log("Entry blocked: contract qualification is incomplete.")
                return

            sizing_price = trigger_price * (1.0 + self.cfg.cash_buffer_pct)
            qty = math.floor(self.cfg.cash / sizing_price)
            if qty < 1:
                self.log(
                    f"Entry blocked: cash ${self.cfg.cash:,.2f} is insufficient for "
                    f"one share near ${trigger_price:.2f}."
                )
                self.traded_today = True
                return

            self.traded_today = True
            self.entry_qty = qty
            self.entry_trigger_price = trigger_price

        sl_price = self._round_to_tick(trigger_price * (1.0 - self.cfg.sl_pct))
        tp_price = self._round_to_tick(trigger_price * (1.0 + self.cfg.tp_pct))

        if self.cfg.dry_run:
            self.log(
                "DRY RUN — would submit: "
                f"BUY MKT {qty} {self.cfg.symbol}, "
                f"TP={tp_price:.2f}, SL={sl_price:.2f}."
            )
            return

        try:
            parent_id, tp_id, sl_id = self._allocate_order_ids(3)
        except RuntimeError as exc:
            self.log(f"Entry blocked: {exc}")
            return

        oca_group = f"ORB_EXIT_{self.cfg.symbol}_{parent_id}"

        parent = self._base_order()
        parent.action = "BUY"
        parent.orderType = "MKT"
        parent.totalQuantity = qty
        parent.transmit = False

        take_profit = self._base_order()
        take_profit.action = "SELL"
        take_profit.orderType = "LMT"
        take_profit.totalQuantity = qty
        take_profit.lmtPrice = tp_price
        take_profit.parentId = parent_id
        take_profit.ocaGroup = oca_group
        take_profit.ocaType = 1
        take_profit.transmit = False

        stop_loss = self._base_order()
        stop_loss.action = "SELL"
        stop_loss.orderType = "STP"
        stop_loss.totalQuantity = qty
        stop_loss.auxPrice = sl_price
        stop_loss.parentId = parent_id
        stop_loss.ocaGroup = oca_group
        stop_loss.ocaType = 1
        stop_loss.transmit = True

        with self.state_lock:
            self.entry_order_id = parent_id
            self.tp_order_id = tp_id
            self.sl_order_id = sl_id
            self.order_objects[parent_id] = parent
            self.order_objects[tp_id] = take_profit
            self.order_objects[sl_id] = stop_loss

        # Transmitting the final child releases the full bracket together.
        self.placeOrder(parent_id, self.contract, parent)
        self.placeOrder(tp_id, self.contract, take_profit)
        self.placeOrder(sl_id, self.contract, stop_loss)

        self.log(
            f"Submitted bracket: BUY MKT {qty} {self.cfg.symbol}; "
            f"initial TP={tp_price:.2f}, SL={sl_price:.2f}; "
            f"orderIds={parent_id}/{tp_id}/{sl_id}."
        )

    def orderStatus(  # noqa: N802
        self,
        orderId,
        status,
        filled,
        remaining,
        avgFillPrice,
        permId,
        parentId,
        lastFillPrice,
        clientId,
        whyHeld,
        mktCapPrice=0.0,
    ) -> None:  # ANN001
        self.log(
            f"Order {orderId}: status={status}, filled={filled}, "
            f"remaining={remaining}, avgFill={avgFillPrice}."
        )

        with self.state_lock:
            entry_id = self.entry_order_id
            tp_id = self.tp_order_id
            sl_id = self.sl_order_id
            flatten_id = self.flatten_order_id

        if orderId == entry_id and status == "Filled":
            fill_price = float(avgFillPrice or lastFillPrice or 0.0)
            with self.state_lock:
                self.in_position = True
                self.current_symbol_position = Decimal(str(self.entry_qty))
                if fill_price > 0:
                    self.entry_fill_price = fill_price
            if fill_price > 0:
                self._adjust_exits_to_fill(fill_price)

        elif orderId in {tp_id, sl_id} and status == "Filled":
            with self.state_lock:
                self.in_position = False
                self.current_symbol_position = Decimal("0")
            exit_name = "take-profit" if orderId == tp_id else "stop-loss"
            self.log(f"Position closed by {exit_name} order {orderId}.")

        elif orderId == flatten_id and status == "Filled":
            with self.state_lock:
                self.in_position = False
                self.current_symbol_position = Decimal("0")
            self.log("End-of-day flatten order filled.")

        elif orderId == sl_id and status == "Inactive":
            self.log("Protective stop became inactive; sending emergency flatten order.")
            self._flatten_position("protective stop inactive")

    def _adjust_exits_to_fill(self, fill_price: float) -> None:
        with self.state_lock:
            if self.exit_prices_adjusted:
                return
            if self.contract is None or self.tp_order_id is None or self.sl_order_id is None:
                return
            tp_order = self.order_objects.get(self.tp_order_id)
            sl_order = self.order_objects.get(self.sl_order_id)
            if tp_order is None or sl_order is None:
                return
            self.exit_prices_adjusted = True

        tp_price = self._round_to_tick(fill_price * (1.0 + self.cfg.tp_pct))
        sl_price = self._round_to_tick(fill_price * (1.0 - self.cfg.sl_pct))
        tp_order.lmtPrice = tp_price
        sl_order.auxPrice = sl_price
        # Modification requests must be transmitted immediately.
        tp_order.transmit = True
        sl_order.transmit = True

        try:
            self.placeOrder(self.tp_order_id, self.contract, tp_order)
            self.placeOrder(self.sl_order_id, self.contract, sl_order)
            self.log(
                f"Adjusted exits to actual fill ${fill_price:.2f}: "
                f"TP={tp_price:.2f}, SL={sl_price:.2f}."
            )
        except Exception as exc:
            self.log(f"Could not adjust exits to fill price: {exc!r}")

    def _cancel_order_compat(self, order_id: Optional[int]) -> None:
        if order_id is None:
            return
        try:
            self.cancelOrder(order_id, "")
        except TypeError:
            self.cancelOrder(order_id)
        except Exception as exc:
            self.log(f"Could not cancel order {order_id}: {exc!r}")

    def _flatten_position(self, reason: str) -> None:
        with self.state_lock:
            if self.flatten_sent or self.cfg.dry_run:
                return
            if self.contract is None:
                return

            pos = self.current_symbol_position
            if pos == 0 and self.in_position:
                pos = Decimal(str(self.entry_qty))
            if pos == 0:
                return

            # This long-only strategy should never hold a short position.
            action = "SELL" if pos > 0 else "BUY"
            qty = int(abs(pos))
            if qty <= 0:
                return
            self.flatten_sent = True

        self._cancel_order_compat(self.tp_order_id)
        self._cancel_order_compat(self.sl_order_id)

        try:
            flatten_id = self._allocate_order_ids(1)[0]
        except RuntimeError as exc:
            self.log(f"Cannot flatten position: {exc}")
            return

        order = self._base_order()
        order.action = action
        order.orderType = "MKT"
        order.totalQuantity = qty
        order.transmit = True
        order.orderRef = f"ORB_FLATTEN_{self.cfg.symbol}_{self.trade_date or 'NA'}"

        with self.state_lock:
            self.flatten_order_id = flatten_id
            self.order_objects[flatten_id] = order

        self.placeOrder(flatten_id, self.contract, order)
        self.log(
            f"Submitted emergency/EOD flatten: {action} MKT {qty} "
            f"{self.cfg.symbol}; reason={reason}; orderId={flatten_id}."
        )

    # ------------------------------------------------------------------
    # Time controls
    # ------------------------------------------------------------------
    def _check_time_controls(self) -> None:
        now = datetime.now(NY)
        now_t = now.time().replace(tzinfo=None)

        with self.state_lock:
            trade_date = self.trade_date
            should_flatten = (
                trade_date == now.date()
                and now_t >= self.cfg.flatten_time
                and (self.current_symbol_position != 0 or self.in_position)
                and not self.flatten_sent
            )
            should_log_cutoff = (
                trade_date == now.date()
                and now_t >= self.cfg.entry_cutoff
                and not self.traded_today
                and self.cutoff_logged_for_date != now.date()
            )
            if should_log_cutoff:
                self.cutoff_logged_for_date = now.date()

        if should_log_cutoff:
            self.log(
                f"Entry cutoff {self.cfg.entry_cutoff:%H:%M} reached with no valid "
                "breakout. No new trade will be opened today."
            )
        if should_flatten:
            self._flatten_position(f"flatten time {self.cfg.flatten_time:%H:%M} ET")


# ----------------------------------------------------------------------
# CLI helpers
# ----------------------------------------------------------------------
def parse_hhmm(value: str) -> time:
    raw = str(value).strip().replace(":", "")
    if not raw.isdigit() or len(raw) not in {3, 4}:
        raise argparse.ArgumentTypeError(
            f"invalid HHMM value {value!r}; examples: 945, 1200, 15:55"
        )
    raw = raw.zfill(4)
    hour = int(raw[:2])
    minute = int(raw[2:])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise argparse.ArgumentTypeError(f"invalid time {value!r}")
    return time(hour, minute)


def parse_pct(value: str) -> float:
    raw = str(value).strip()
    explicit_percent = raw.endswith("%")
    if explicit_percent:
        raw = raw[:-1].strip()
    try:
        number = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid percentage {value!r}") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("percentage must be positive")
    if explicit_percent or number >= 1.0:
        number /= 100.0
    if not (0 < number < 1):
        raise argparse.ArgumentTypeError(
            "normalized percentage must be between 0 and 1"
        )
    return number


def positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid number {value!r}") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return number


def nonnegative_pct(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid number {value!r}") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("value cannot be negative")
    if number >= 1:
        number /= 100.0
    if number >= 1:
        raise argparse.ArgumentTypeError("value must be less than 100%")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="IBKR paper-trading ORB bot: long breakout on closed 5-minute bars."
    )
    parser.add_argument("--symbol", required=True, help="US stock symbol, e.g. TSLA")
    parser.add_argument("--cash", required=True, type=positive_float)
    parser.add_argument(
        "--sl-pct",
        required=True,
        type=parse_pct,
        help="Stop loss: 0.0325, 3.25, or 3.25%% all mean 3.25%%.",
    )
    parser.add_argument(
        "--tp-pct",
        required=True,
        type=parse_pct,
        help="Take profit: 0.04, 4, or 4%% all mean 4%%.",
    )
    parser.add_argument(
        "--range-end-times",
        required=True,
        nargs="+",
        type=parse_hhmm,
        help="Opening-range end time. Live version currently accepts one value, e.g. 945.",
    )
    parser.add_argument(
        "--entry-cutoff",
        type=parse_hhmm,
        default=parse_hhmm("1200"),
        help="No new entries after this ET time; default 1200.",
    )
    parser.add_argument(
        "--flatten-time",
        type=parse_hhmm,
        default=parse_hhmm("1555"),
        help="Close any remaining position at this ET time; default 1555.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--port",
        type=int,
        default=7497,
        help="Paper TWS=7497; paper IB Gateway=4002.",
    )
    parser.add_argument("--client-id", type=int, default=21)
    parser.add_argument(
        "--account",
        default=None,
        help="Paper account ID. Required only when IB exposes multiple accounts.",
    )
    parser.add_argument(
        "--primary-exchange",
        default=None,
        help="Optional ambiguity resolver, e.g. NASDAQ or NYSE.",
    )
    parser.add_argument(
        "--cash-buffer-pct",
        type=nonnegative_pct,
        default=0.005,
        help="Sizing reserve for market-order slippage; default 0.005 = 0.5%%.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Evaluate live signals but do not submit orders.",
    )
    return parser


def config_from_args(argv: Optional[Sequence[str]] = None) -> Config:
    parser = build_parser()
    args = parser.parse_args(argv)

    if len(args.range_end_times) != 1:
        parser.error(
            "the live trader currently supports exactly one --range-end-times value"
        )
    range_end = args.range_end_times[0]

    range_minutes = range_end.hour * 60 + range_end.minute
    open_minutes = OPEN_TIME.hour * 60 + OPEN_TIME.minute
    if range_minutes <= open_minutes:
        parser.error("--range-end-times must be later than 09:30 ET")
    if (range_minutes - open_minutes) % BAR_MINUTES != 0:
        parser.error("--range-end-times must align to a 5-minute boundary")
    if args.entry_cutoff <= range_end:
        parser.error("--entry-cutoff must be later than --range-end-times")
    if args.flatten_time <= args.entry_cutoff:
        parser.error("--flatten-time must be later than --entry-cutoff")
    if args.port not in PAPER_PORTS:
        parser.error(
            f"port {args.port} is not an allowed paper port; use 7497 (TWS) "
            "or 4002 (IB Gateway)"
        )

    return Config(
        symbol=args.symbol.strip().upper(),
        cash=args.cash,
        sl_pct=args.sl_pct,
        tp_pct=args.tp_pct,
        range_end=range_end,
        entry_cutoff=args.entry_cutoff,
        flatten_time=args.flatten_time,
        host=args.host,
        port=args.port,
        client_id=args.client_id,
        account=args.account,
        primary_exchange=args.primary_exchange,
        cash_buffer_pct=args.cash_buffer_pct,
        dry_run=args.dry_run,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    cfg = config_from_args(argv)
    app = ORBPaperTrader(cfg)

    app.log(
        "Configuration: "
        f"symbol={cfg.symbol}, cash=${cfg.cash:,.2f}, "
        f"SL={cfg.sl_pct:.2%}, TP={cfg.tp_pct:.2%}, "
        f"rangeEnd={cfg.range_end:%H:%M} ET, "
        f"entryCutoff={cfg.entry_cutoff:%H:%M} ET, "
        f"flatten={cfg.flatten_time:%H:%M} ET, dryRun={cfg.dry_run}."
    )

    try:
        app.connect(cfg.host, cfg.port, clientId=cfg.client_id)
    except Exception as exc:
        app.log(f"Could not connect to IB: {exc!r}")
        return 2

    api_thread = threading.Thread(target=app.run, name="ib-api", daemon=True)
    api_thread.start()

    if not app.connected_event.wait(timeout=15.0):
        app.log(
            "Did not receive nextValidId within 15 seconds. Check TWS/IB Gateway, "
            "API socket settings, port, and client ID."
        )
        app.shutdown()
        return 3

    app.start_requests()
    clock_thread = threading.Thread(
        target=app._clock_loop, name="orb-clock", daemon=True
    )
    clock_thread.start()

    try:
        while not app.stop_event.wait(0.5):
            if not app.isConnected():
                break
    except KeyboardInterrupt:
        app.log("Keyboard interrupt received; shutting down.")
    finally:
        app.shutdown()
        api_thread.join(timeout=3.0)

    return 1 if app.fatal_reason else 0


if __name__ == "__main__":
    sys.exit(main())
