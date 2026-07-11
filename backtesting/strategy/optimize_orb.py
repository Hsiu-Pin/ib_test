from datetime import time
from backtesting import Strategy


class OpeningRangeBreakout(Strategy):
    """
    Long-only Opening Range Breakout.

    Data assumption:
        IB 5m CSV timestamp is bar start time.

        Example:
            09:30 row = 09:30~09:35 bar
            09:35 row = 09:35~09:40 bar
            09:40 row = 09:40~09:45 bar
            09:45 row = 09:45~09:50 bar

    Therefore:
        range_end_hhmm = 945

        opening range includes:
            09:30, 09:35, 09:40

        first possible signal bar:
            09:45 bar

    Important:
        This strategy uses the signal bar close as the entry reference price.

        If you want the backtest to enter on that same close, use:

            Backtest(..., trade_on_close=True)

        If trade_on_close=False, backtesting.py may execute market orders
        on the next bar open, and SL/TP percentages may no longer match
        exactly from the real entry price.
    """

    # ------------------------------------------------------------
    # Optimizable parameters
    # ------------------------------------------------------------

    stop_loss_pct = 0.015
    take_profit_pct = 0.0275

    range_end_hhmm = 945
    exit_hhmm = 1545
    entry_deadline_hhmm = 1130

    entry_mode = "breakout"       # "breakout" or "retest"

    breakout_buffer_pct = 0.0     # 0.0005 = 0.05% above opening_high
    retest_buffer_pct = 0.003     # 0.003 = 0.3% retest band

    # ------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------

    def _hhmm_to_time(self, hhmm):
        hhmm = int(hhmm)
        hour = hhmm // 100
        minute = hhmm % 100
        return time(hour, minute)

    def _reset_day_state(self, today):
        self.current_day = today

        self.opening_high = None
        self.opening_low = None

        self.traded_today = False
        self.breakout_seen = False

    def _long_entry(self, price):
        """
        Enter long using fixed percentage stop loss and take profit.

        Because price is self.data.Close[-1], this assumes the signal bar
        close is the intended entry reference.
        """
        self.buy(
            sl=price * (1 - self.stop_loss_pct),
            tp=price * (1 + self.take_profit_pct),
        )
        self.traded_today = True

    # ------------------------------------------------------------
    # backtesting.py lifecycle
    # ------------------------------------------------------------

    def init(self):
        self.current_day = None

        self.opening_high = None
        self.opening_low = None

        self.traded_today = False
        self.breakout_seen = False

        self.market_open = time(9, 30)
        self.range_end = self._hhmm_to_time(self.range_end_hhmm)
        self.exit_time = self._hhmm_to_time(self.exit_hhmm)
        self.entry_deadline = self._hhmm_to_time(self.entry_deadline_hhmm)

    def next(self):
        now = self.data.index[-1]
        today = now.date()
        current_time = now.time()

        price = self.data.Close[-1]
        high = self.data.High[-1]
        low = self.data.Low[-1]

        # New trading day
        if self.current_day != today:
            self._reset_day_state(today)

        # Ignore pre-market data if present
        if current_time < self.market_open:
            return

        # Force intraday exit
        if current_time >= self.exit_time:
            if self.position:
                self.position.close()
            return

        # Build opening range
        #
        # With IB bar-start timestamps:
        #   09:30, 09:35, 09:40 are included when range_end = 09:45.
        #   09:45 is the first post-range bar.
        if self.market_open <= current_time < self.range_end:
            if self.opening_high is None:
                self.opening_high = high
                self.opening_low = low
            else:
                self.opening_high = max(self.opening_high, high)
                self.opening_low = min(self.opening_low, low)
            return

        # Opening range must be ready
        if self.opening_high is None or self.opening_low is None:
            return

        # Long-only, one trade per day
        if self.traded_today:
            return

        # Do not enter if already holding a position
        if self.position:
            return

        # Do not enter too late
        #
        # With bar-start timestamps:
        #   current_time = 11:25 means 11:25~11:30 bar.
        #   current_time = 11:30 means 11:30~11:35 bar.
        #
        # This condition excludes the 11:30 bar and later.
        if current_time >= self.entry_deadline:
            return

        breakout_level = self.opening_high * (1 + self.breakout_buffer_pct)

        # --------------------------------------------------------
        # Mode 1: breakout entry
        # --------------------------------------------------------
        if self.entry_mode == "breakout":
            if price > breakout_level:
                self._long_entry(price)
            return

        # --------------------------------------------------------
        # Mode 2: breakout first, then retest
        # --------------------------------------------------------
        if self.entry_mode == "retest":
            if not self.breakout_seen:
                if price > breakout_level:
                    self.breakout_seen = True
                return

            retest_lower = self.opening_high * (1 - self.retest_buffer_pct)
            retest_upper = self.opening_high * (1 + self.retest_buffer_pct)

            touched_retest_zone = retest_lower <= low <= retest_upper
            closed_back_above_range = price > self.opening_high

            if touched_retest_zone and closed_back_above_range:
                self._long_entry(price)
            return

        raise ValueError(f"Unknown entry_mode: {self.entry_mode}")