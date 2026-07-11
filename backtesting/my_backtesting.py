import pandas as pd
import yfinance as yf

from datetime import time
from backtesting import Backtest, Strategy
from backtesting.lib import crossover

def load_data(symbol, interval="1d", start=None, end=None, period=None):
    """
    下載 yfinance 資料。

    日線範例：
        interval="1d", start="2026-01-02", end="2026-07-09"

    日內範例：
        interval="5m", period="30d"
    """

    kwargs = {
        "tickers": symbol,
        "interval": interval,
        "auto_adjust": False,
        "progress": False,
        "prepost": False,
    }

    if period is not None:
        kwargs["period"] = period
    else:
        kwargs["start"] = start
        kwargs["end"] = end

    data = yf.download(**kwargs)

    if data.empty:
        raise ValueError("沒有下載到資料。請檢查 symbol、period、interval、start/end。")

    # yfinance 有時候會回傳 MultiIndex 欄位，例如 ('Close', 'TSLA')
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)

    required_columns = ["Open", "High", "Low", "Close", "Volume"]
    data = data[required_columns]
    data = data.dropna()

    # 日內資料通常有時區，轉成紐約時間，再拿掉 timezone
    # 這樣 OpeningRangeBreakout 才能正確用 09:30、10:00、15:55 判斷
    if isinstance(data.index, pd.DatetimeIndex):
        if data.index.tz is not None:
            data.index = data.index.tz_convert("America/New_York").tz_localize(None)

    data = data.sort_index()

    return data


# ============================================================
# 7. 跑回測
# ============================================================

def run_backtest(data, strategy_class, cash, commission, plot=True):
    bt = Backtest(
        data,
        strategy_class,
        cash=cash,
        commission=commission,
        exclusive_orders=True,
        trade_on_close=True,
        finalize_trades=True,
    )

    stats = bt.run()

    print("\n========== 回測結果 ==========")
    print(stats)

    # 取出交易紀錄
    trades = stats["_trades"].copy()

    print("\n========== 交易紀錄 ==========")
    print(trades)

    # 如果完全沒有交易，後面統計就不要跑
    if trades.empty:
        print("\n沒有任何交易。")
        return stats

    # 計算每筆持倉時間
    trades["Duration"] = trades["ExitTime"] - trades["EntryTime"]

    print("\n========== 重點交易欄位 ==========")
    print(trades[[
        "EntryTime",
        "ExitTime",
        "Duration",
        "Size",
        "EntryPrice",
        "ExitPrice",
        "ReturnPct",
        "PnL"
    ]])

    # 多單 / 空單拆開
    long_trades = trades[trades["Size"] > 0]
    short_trades = trades[trades["Size"] < 0]

    print("\n========== 多單統計 ==========")
    if long_trades.empty:
        print("沒有多單交易。")
    else:
        print(long_trades["ReturnPct"].describe())
        print("多單筆數:", len(long_trades))
        print("多單勝率:", round((long_trades["ReturnPct"] > 0).mean() * 100, 2), "%")
        print("多單總損益:", round(long_trades["PnL"].sum(), 2))

    print("\n========== 空單統計 ==========")
    if short_trades.empty:
        print("沒有空單交易。")
    else:
        print(short_trades["ReturnPct"].describe())
        print("空單筆數:", len(short_trades))
        print("空單勝率:", round((short_trades["ReturnPct"] > 0).mean() * 100, 2), "%")
        print("空單總損益:", round(short_trades["PnL"].sum(), 2))

    # 檢查有沒有跨日持倉
    print("\n========== 跨日持倉檢查 ==========")
    overnight_trades = trades[
        trades["EntryTime"].dt.date != trades["ExitTime"].dt.date
    ]

    if overnight_trades.empty:
        print("沒有跨日持倉。")
    else:
        print("有跨日持倉：")
        print(overnight_trades[[
            "EntryTime",
            "ExitTime",
            "Duration",
            "Size",
            "ReturnPct",
            "PnL"
        ]])

    if plot:
        bt.plot()

    return stats
