from datetime import time
from backtesting import Strategy


class OpeningRangeBreakoutC1(Strategy):
    """
    Long-only Opening Range Breakout.

    entry_mode:
        "breakout" = 突破 opening_high 直接買
        "retest"   = 先突破 opening_high，之後回踩 opening_high 附近再買
    """

    stop_loss_pct = 0.015
    take_profit_pct = 0.0275

    range_end_hhmm = 945
    exit_hhmm = 1545
    entry_deadline_hhmm = 1130

    allow_short = False

    entry_mode = "breakout"

    breakout_buffer_pct = 0.0
    retest_buffer_pct = 0.003

    def _hhmm_to_time(self, hhmm):
        hhmm = int(hhmm)
        hour = hhmm // 100
        minute = hhmm % 100
        return time(hour, minute)

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

        # 新的一天：重置狀態
        if self.current_day != today:
            self.current_day = today
            self.opening_high = None
            self.opening_low = None
            self.traded_today = False
            self.breakout_seen = False

        # 還沒開盤
        if current_time < self.market_open:
            return

        # 收盤前強制平倉
        if current_time >= self.exit_time:
            if self.position:
                self.position.close()
            return

        # 建立 opening range
        if self.market_open <= current_time < self.range_end:
            if self.opening_high is None:
                self.opening_high = high
                self.opening_low = low
            else:
                self.opening_high = max(self.opening_high, high)
                self.opening_low = min(self.opening_low, low)
            return

        # opening range 還沒建立好
        if self.opening_high is None or self.opening_low is None:
            return

        # 今天已經交易過
        if self.traded_today:
            return

        # 已經有持倉
        if self.position:
            return

        # 超過最晚進場時間
        if current_time >= self.entry_deadline:
            return

        breakout_level = self.opening_high * (1 + self.breakout_buffer_pct)
        retest_level = self.opening_high * (1 + self.retest_buffer_pct)

        # ======================================================
        # 模式 1：直接突破進場
        # ======================================================
        if self.entry_mode == "breakout":
            if price > breakout_level:
                self.buy(
                    sl=price * (1 - self.stop_loss_pct),
                    tp=price * (1 + self.take_profit_pct)
                )
                self.traded_today = True

        # ======================================================
        # 模式 2：突破後等待回踩
        # ======================================================
        elif self.entry_mode == "retest":

            # 先看到突破
            if not self.breakout_seen:
                if price > breakout_level:
                    self.breakout_seen = True
                return

            # 突破後回踩 opening_high 附近，且收盤仍站上 opening_high
            if low <= retest_level and price > self.opening_high:
                self.buy(
                    sl=price * (1 - self.stop_loss_pct),
                    tp=price * (1 + self.take_profit_pct)
                )
                self.traded_today = True

        else:
            raise ValueError(f"未知 entry_mode: {self.entry_mode}")
