import argparse
import os
import re
from pathlib import Path
from typing import Optional, Iterator, Tuple, List

import pandas as pd
from backtesting import Backtest

from tools.check import *
from strategy.optimize_orb import *

# ============================================================
# 1. 預設設定
# ============================================================

stg = OpeningRangeBreakout
# stg = VWAPBreakout
post_fix = "5m"

DEFAULT_SYMBOL = "TSLA"
DEFAULT_IB_DATA_DIR = "./ib_data"
DEFAULT_IB_SEC_TYPE = "STK"

DEFAULT_START_YEAR = 2025
DEFAULT_START_MONTH = 1
DEFAULT_END_YEAR = 2025
DEFAULT_END_MONTH = 12

DEFAULT_CASH = 10000
DEFAULT_ALLOW_SHORT = False
DEFAULT_EXIT_HHMM = 1530

# Gap filter default
# CLI uses percent units, e.g. -3.0 means -3%.
# Strategy receives decimal units, e.g. -0.03.
DEFAULT_USE_GAP_FILTER = False
DEFAULT_MIN_GAP_PCT = -3.0
DEFAULT_MAX_GAP_PCT = 3.0

# 建議搜索策略：
# 1. 固定 long-only ORB
# 2. 先重點測 09:45, 09:50, 10:00
# 3. SL 1.0% ~ 3.5%，TP 2.0% ~ 4.0%，每 0.25% 一格
DEFAULT_RANGE_END_TIMES = [945, 950, 1000]
DEFAULT_SL_START_PCT = 1.00
DEFAULT_SL_END_PCT = 3.50
DEFAULT_SL_STEP_PCT = 0.25
DEFAULT_TP_START_PCT = 2.00
DEFAULT_TP_END_PCT = 4.00
DEFAULT_TP_STEP_PCT = 0.25

# IBKR Pro Fixed 美股估算：$0.005/share, min $1/order, max 1% trade value
DEFAULT_COMMISSION_MODEL = "ibkr_fixed"
DEFAULT_SPREAD = 0.0002  # 0.02%，用來粗估 spread/slippage 成本

# ranking：不要只看 Return。Return 高加分，Drawdown 大扣分，PF 高加分。
DEFAULT_DD_WEIGHT = 0.70
DEFAULT_PF_WEIGHT = 5.00


def normalize_ohlcv_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        raise ValueError("input data is empty")

    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = [str(col[0]) for col in df.columns]

    col_map = {}
    for col in df.columns:
        low = str(col).strip().lower()
        if low == "open":
            col_map[col] = "Open"
        elif low == "high":
            col_map[col] = "High"
        elif low == "low":
            col_map[col] = "Low"
        elif low == "close":
            col_map[col] = "Close"
        elif low == "volume":
            col_map[col] = "Volume"

    df = df.rename(columns=col_map)

    required = ["Open", "High", "Low", "Close", "Volume"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"missing required OHLCV columns: {missing}; columns={list(df.columns)}")

    out = df[required].copy()

    for col in required:
        out[col] = pd.to_numeric(out[col], errors="coerce")

    out = out.dropna(subset=["Open", "High", "Low", "Close"])
    out["Volume"] = out["Volume"].fillna(0)

    if not isinstance(out.index, pd.DatetimeIndex):
        raise ValueError("data index must be DatetimeIndex")

    if out.index.tz is not None:
        out.index = out.index.tz_localize(None)

    out = out.sort_index()
    out = out[~out.index.duplicated(keep="last")]

    return out


def build_ib_csv_path(symbol: str, year: int, month: int, data_dir: str, sec_type: str = "STK") -> Path:
    symbol = symbol.upper().strip()
    sec_type = sec_type.upper().strip()
    filename = f"{symbol}_{sec_type}_5m_{year}_{month:02d}.csv"
    return Path(data_dir) / filename


def load_one_ib_csv_month(symbol: str, year: int, month: int, data_dir: str, sec_type: str = "STK") -> pd.DataFrame:
    path = build_ib_csv_path(symbol, year, month, data_dir, sec_type)

    if not path.exists():
        raise FileNotFoundError(
            f"找不到 IB CSV: {path}\n"
            f"請先執行類似：\n"
            f"python fetch_ib_5m_month_v2.py --symbol {symbol.upper()} --year {year} --month {month} --out {data_dir}"
        )

    raw = pd.read_csv(path)

    if "date" not in raw.columns:
        raise ValueError(f"IB CSV 缺少 date 欄位: {path}; columns={list(raw.columns)}")

    raw["Datetime"] = raw["date"].apply(parse_ib_datetime)
    bad_dt_count = int(raw["Datetime"].isna().sum())
    if bad_dt_count > 0:
        print(f"Warning: {path.name}: {bad_dt_count} rows have unparseable datetime and will be dropped.")

    raw = raw.dropna(subset=["Datetime"])
    raw = raw.set_index("Datetime")

    data = normalize_ohlcv_columns(raw)

    if data.empty:
        raise ValueError(f"IB CSV 載入後沒有有效資料: {path}")

    return data


def load_ib_csv_range_data(
    symbol: str,
    start_year: int,
    start_month: int,
    end_year: int,
    end_month: int,
    data_dir: str = "./ib_data",
    sec_type: str = "STK",
    skip_missing: bool = False,
) -> pd.DataFrame:
    frames: List[pd.DataFrame] = []
    loaded_paths = []
    missing_paths = []

    for year, month in iter_months(start_year, start_month, end_year, end_month):
        path = build_ib_csv_path(symbol, year, month, data_dir, sec_type)
        print(f"Loading {path} ...")

        try:
            month_data = load_one_ib_csv_month(
                symbol=symbol,
                year=year,
                month=month,
                data_dir=data_dir,
                sec_type=sec_type,
            )
            frames.append(month_data)
            loaded_paths.append(path)
        except FileNotFoundError:
            missing_paths.append(path)
            if skip_missing:
                print(f"Warning: missing file skipped: {path}")
                continue
            raise

    if not frames:
        raise ValueError("沒有載入任何 IB CSV。請確認 symbol / 年月區間 / ib-data-dir 是否正確。")

    data = pd.concat(frames, axis=0)
    data = data.sort_index()
    data = data[~data.index.duplicated(keep="last")]

    start_dt = pd.Timestamp(year=start_year, month=start_month, day=1)
    end_exclusive = next_month_start(end_year, end_month)
    data = data[(data.index >= start_dt) & (data.index < end_exclusive)]

    if data.empty:
        raise ValueError(
            f"合併後資料為空。區間={start_year}-{start_month:02d} 到 {end_year}-{end_month:02d}"
        )

    print(f"Loaded files: {len(loaded_paths)}")
    if missing_paths:
        print(f"Missing skipped files: {len(missing_paths)}")

    return data


def add_gap_pct_column(data: pd.DataFrame) -> pd.DataFrame:
    """
    Add GapPct column for optional gap filter.

    GapPct = today regular-session open / previous regular-session close - 1

    Notes:
        - Uses only the loaded 5m data.
        - The first loaded trading day has no previous close, so GapPct is NaN.
        - If use_gap_filter=True, the first loaded trading day will be skipped.
        - If use_gap_filter=False, this column has no effect.
    """
    if data is None or data.empty:
        return data

    df = data.copy()
    trade_dates = pd.Series(df.index.date, index=df.index, name="TradeDate")

    daily = df.groupby(trade_dates).agg(
        day_open=("Open", "first"),
        day_close=("Close", "last"),
    )
    daily["prev_day_close"] = daily["day_close"].shift(1)
    daily["gap_pct"] = daily["day_open"] / daily["prev_day_close"] - 1.0

    gap_map = daily["gap_pct"].to_dict()
    df["GapPct"] = [gap_map.get(d) for d in df.index.date]

    return df


# ============================================================
# 3. IBKR commission / 成本模型
# ============================================================

def ibkr_us_stock_fixed_commission(order_size, price):
    """
    IBKR Pro Fixed 美股/ETF 簡化模型：
        $0.005 / share
        min $1.00 / order
        max 1% of trade value
    """
    try:
        shares = abs(float(order_size))
        px = float(price)
    except Exception:
        return 0.0

    if shares <= 0 or px <= 0:
        return 0.0

    trade_value = shares * px
    commission = shares * 0.005
    commission = max(commission, 1.00)
    commission = min(commission, trade_value * 0.01)
    
    # Slippage
    #slippage_pct = 0.0005
    slippage_pct = 0
    slippage_cost = shares * px * slippage_pct
    
    return commission + slippage_cost



def ibkr_us_stock_tiered_commission(order_size, price):
    """
    IBKR Pro Tiered 美股/ETF 簡化模型：
        $0.0035 / share
        min $0.35 / order
        max 1% of trade value

    注意：真實 Tiered 還可能有 exchange / clearing / regulatory fees。
    這裡只做近似回測成本。
    """
    try:
        shares = abs(float(order_size))
        px = float(price)
    except Exception:
        return 0.0

    if shares <= 0 or px <= 0:
        return 0.0

    trade_value = shares * px
    commission = shares * 0.0035
    commission = max(commission, 0.35)
    commission = min(commission, trade_value * 0.01)
    return commission


def make_percent_commission(rate: float):
    if rate is None or rate < 0:
        raise ValueError(f"percent commission rate 不合理: {rate}")
    return float(rate)


def select_commission(model: str, percent_rate: float):
    model = str(model).lower().strip()
    if model == "ibkr_fixed":
        return ibkr_us_stock_fixed_commission
    if model == "ibkr_tiered":
        return ibkr_us_stock_tiered_commission
    if model == "percent":
        return make_percent_commission(percent_rate)
    if model == "none":
        return 0.0
    raise ValueError(f"Unknown commission model: {model}")


# ============================================================
# 4. ranking
# ============================================================

def calc_rank_metric(stats, dd_weight: float, pf_weight: float) -> float:
    ret = safe_metric(stats.get("Return [%]"), default=0.0)
    max_dd = safe_metric(stats.get("Max. Drawdown [%]"), default=0.0)
    pf = safe_float(stats.get("Profit Factor"), default=1.0)

    # Profit Factor 可能是 inf；避免讓單一異常值支配排序。
    if pf is None:
        pf = 1.0
    if pf == float("inf"):
        pf = 5.0
    pf = min(float(pf), 5.0)

    return ret - float(dd_weight) * abs(max_dd) + float(pf_weight) * (pf - 1.0)


# ============================================================
# 5. CLI
# ============================================================

def parse_args():
    parser = argparse.ArgumentParser(description="ORB optimization using local IB 5m CSV range only, v2.")

    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument("--ib-data-dir", default=DEFAULT_IB_DATA_DIR)
    parser.add_argument("--sec-type", default=DEFAULT_IB_SEC_TYPE)

    parser.add_argument("--start-year", type=int, default=DEFAULT_START_YEAR)
    parser.add_argument("--start-month", type=int, default=DEFAULT_START_MONTH)
    parser.add_argument("--end-year", type=int, default=DEFAULT_END_YEAR)
    parser.add_argument("--end-month", type=int, default=DEFAULT_END_MONTH)

    parser.add_argument("--skip-missing", action="store_true")

    parser.add_argument("--cash", type=float, default=DEFAULT_CASH)
    parser.add_argument("--allow-short", action="store_true", default=DEFAULT_ALLOW_SHORT)
    parser.add_argument("--exit-hhmm", type=int, default=DEFAULT_EXIT_HHMM)

    parser.add_argument(
        "--use-gap-filter",
        action="store_true",
        default=DEFAULT_USE_GAP_FILTER,
        help="啟用 gap filter。啟用後只交易 min/max gap pct 範圍內的日子。",
    )
    parser.add_argument(
        "--min-gap-pct",
        type=float,
        default=DEFAULT_MIN_GAP_PCT,
        help="gap 下限，單位是百分比。例如 -3.0 表示 -3%。",
    )
    parser.add_argument(
        "--max-gap-pct",
        type=float,
        default=DEFAULT_MAX_GAP_PCT,
        help="gap 上限，單位是百分比。例如 3.0 表示 +3%。",
    )

    parser.add_argument("--range-end-times", default=",".join(str(x) for x in DEFAULT_RANGE_END_TIMES))

    parser.add_argument("--sl-start-pct", type=float, default=DEFAULT_SL_START_PCT)
    parser.add_argument("--sl-end-pct", type=float, default=DEFAULT_SL_END_PCT)
    parser.add_argument("--sl-step-pct", type=float, default=DEFAULT_SL_STEP_PCT)
    parser.add_argument("--tp-start-pct", type=float, default=DEFAULT_TP_START_PCT)
    parser.add_argument("--tp-end-pct", type=float, default=DEFAULT_TP_END_PCT)
    parser.add_argument("--tp-step-pct", type=float, default=DEFAULT_TP_STEP_PCT)

    parser.add_argument(
        "--commission-model",
        default=DEFAULT_COMMISSION_MODEL,
        choices=["ibkr_fixed", "ibkr_tiered", "percent", "none"],
    )
    parser.add_argument(
        "--commission-percent-rate",
        type=float,
        default=0.0005,
        help="只有 --commission-model percent 時使用，例如 0.0005 = 0.05%。",
    )
    parser.add_argument("--spread", type=float, default=DEFAULT_SPREAD)

    parser.add_argument("--dd-weight", type=float, default=DEFAULT_DD_WEIGHT)
    parser.add_argument("--pf-weight", type=float, default=DEFAULT_PF_WEIGHT)
    parser.add_argument(
        "--sort-by",
        default="rank_metric",
        choices=["rank_metric", "return"],
        help="排序方式。建議 rank_metric，不要只看 return。",
    )

    parser.add_argument("--output-tag", default="")
    parser.add_argument("--top-n", type=int, default=20)

    return parser.parse_args()


# ============================================================
# 6. 主程式：跑所有排列組合
# ============================================================

def main():
    args = parse_args()

    symbol = args.symbol.upper().strip()
    range_end_times = parse_int_list(args.range_end_times)

    stop_loss_values = pct_range_values(args.sl_start_pct, args.sl_end_pct, args.sl_step_pct)
    take_profit_values = pct_range_values(args.tp_start_pct, args.tp_end_pct, args.tp_step_pct)

    commission = select_commission(args.commission_model, args.commission_percent_rate)

    print("========== 載入 IB CSV 資料 ==========")
    print(f"Symbol: {symbol}")
    print(f"IB data dir: {args.ib_data_dir}")
    print(f"Date range: {args.start_year}-{args.start_month:02d} to {args.end_year}-{args.end_month:02d}")
    print(f"Skip missing: {args.skip_missing}")

    data = load_ib_csv_range_data(
        symbol=symbol,
        start_year=args.start_year,
        start_month=args.start_month,
        end_year=args.end_year,
        end_month=args.end_month,
        data_dir=args.ib_data_dir,
        sec_type=args.sec_type,
        skip_missing=args.skip_missing,
    )

    data = add_gap_pct_column(data)

    print(data.head())
    print("...")
    print(data.tail())
    print(f"資料筆數：{len(data)}")
    print(f"第一筆時間：{data.index[0]}")
    print(f"最後一筆時間：{data.index[-1]}")

    total_runs = len(stop_loss_values) * len(take_profit_values) * len(range_end_times)
    print("\n========== 開始參數搜尋 ==========")
    print(f"策略：{stg.__name__}")
    print(f"標的：{symbol}")
    print(f"資料來源：IB CSV")
    print(f"回測區間：{args.start_year}-{args.start_month:02d} 到 {args.end_year}-{args.end_month:02d}")
    print(f"總共會跑 {total_runs} 組參數")
    print(f"SL: {args.sl_start_pct:.2f}% ~ {args.sl_end_pct:.2f}%, step {args.sl_step_pct:.2f}% ({len(stop_loss_values)} 組)")
    print(f"TP: {args.tp_start_pct:.2f}% ~ {args.tp_end_pct:.2f}%, step {args.tp_step_pct:.2f}% ({len(take_profit_values)} 組)")
    print(f"判斷方向時間：{[format_hhmm(x) for x in range_end_times]}")
    print(f"是否允許做空：{args.allow_short}")
    print(f"Gap filter: {args.use_gap_filter}")
    if args.use_gap_filter:
        print(f"Gap range: {args.min_gap_pct:.2f}% ~ {args.max_gap_pct:.2f}%")
    print(f"Cash: {args.cash}")
    print(f"Commission model: {args.commission_model}")
    print(f"Spread: {args.spread}")
    print(f"Rank metric: Return - {args.dd_weight} * abs(MaxDD) + {args.pf_weight} * (PF - 1)")
    print("=" * 50)

    bt = Backtest(
        data,
        stg,
        cash=args.cash,
        commission=commission,
        spread=args.spread,
        exclusive_orders=True,
        trade_on_close=True,
        finalize_trades=True,
    )

    results = []
    run_id = 0

    for stop_loss_pct in stop_loss_values:
        for take_profit_pct in take_profit_values:
            for range_end_hhmm in range_end_times:
                run_id += 1

                try:
                    stats = bt.run(
                        stop_loss_pct=stop_loss_pct,
                        take_profit_pct=take_profit_pct,
                        range_end_hhmm=range_end_hhmm,
                        exit_hhmm=args.exit_hhmm,
                        use_gap_filter=args.use_gap_filter,
                        min_gap_pct=args.min_gap_pct / 100.0,
                        max_gap_pct=args.max_gap_pct / 100.0,
                        #allow_short=args.allow_short,
                    )

                    trades = stats["_trades"]

                    if trades.empty:
                        overnight_count = 0
                    else:
                        overnight_count = (
                            trades["EntryTime"].dt.date != trades["ExitTime"].dt.date
                        ).sum()

                    rank_metric = calc_rank_metric(stats, args.dd_weight, args.pf_weight)

                    row = {
                        "rank_metric": rank_metric,

                        "data_source": "ib_csv",
                        "symbol": symbol,
                        "start_year": args.start_year,
                        "start_month": args.start_month,
                        "end_year": args.end_year,
                        "end_month": args.end_month,

                        "commission_model": args.commission_model,
                        "spread": args.spread,
                        "cash": args.cash,

                        "stop_loss_pct": stop_loss_pct,
                        "take_profit_pct": take_profit_pct,
                        "stop_loss_%": stop_loss_pct * 100,
                        "take_profit_%": take_profit_pct * 100,

                        "range_end_hhmm": range_end_hhmm,
                        "range_end_time": format_hhmm(range_end_hhmm),

                        "allow_short": args.allow_short,
                        "exit_hhmm": args.exit_hhmm,

                        "use_gap_filter": args.use_gap_filter,
                        "min_gap_pct": args.min_gap_pct / 100.0,
                        "max_gap_pct": args.max_gap_pct / 100.0,
                        "min_gap_%": args.min_gap_pct,
                        "max_gap_%": args.max_gap_pct,

                        "Return [%]": safe_float(stats["Return [%]"]),
                        "Buy & Hold Return [%]": safe_float(stats["Buy & Hold Return [%]"]),
                        "Strategy - BuyHold [%]": (
                            safe_metric(stats["Return [%]"], default=0.0)
                            - safe_metric(stats["Buy & Hold Return [%]"], default=0.0)
                        ),
                        "Equity Final [$]": safe_float(stats["Equity Final [$]"]),
                        "Max. Drawdown [%]": safe_float(stats["Max. Drawdown [%]"]),
                        "# Trades": safe_float(stats["# Trades"]),
                        "Win Rate [%]": safe_float(stats["Win Rate [%]"]),
                        "Profit Factor": safe_float(stats["Profit Factor"]),
                        "Expectancy [%]": safe_float(stats["Expectancy [%]"]),
                        "Sharpe Ratio": safe_float(stats["Sharpe Ratio"]),
                        "SQN": safe_float(stats["SQN"]),
                        "Commissions [$]": safe_float(stats["Commissions [$]"]),
                        "overnight_count": int(overnight_count),
                    }

                    results.append(row)

                    if run_id % 50 == 0 or run_id == total_runs:
                        print(f"已完成 {run_id}/{total_runs}")

                except Exception as e:
                    print(
                        f"第 {run_id} 組失敗："
                        f"SL={stop_loss_pct}, TP={take_profit_pct}, "
                        f"range_end={range_end_hhmm}, error={e}"
                    )

    if not results:
        print("沒有任何成功結果。")
        return

    df = pd.DataFrame(results)

    if args.sort_by == "rank_metric":
        sort_col = "rank_metric"
    else:
        sort_col = "Return [%]"

    df = df.sort_values(sort_col, ascending=False).reset_index(drop=True)

    os.makedirs("./result", exist_ok=True)

    range_tag = f"{args.start_year}_{args.start_month:02d}_to_{args.end_year}_{args.end_month:02d}"
    user_tag = f"_{args.output_tag}" if args.output_tag else ""
    output_postfix = f"{symbol}_{range_tag}_{post_fix}{user_tag}"

    full_path = f"./result/orb_{output_postfix}.csv"
    top_path = f"./result/orb_top{args.top_n}_{output_postfix}.csv"

    df.to_csv(full_path, index=False, encoding="utf-8-sig")
    topn = df.head(args.top_n)
    topn.to_csv(top_path, index=False, encoding="utf-8-sig")

    print("\n========== 最佳結果 ==========")
    best = df.iloc[0]

    print(f"Sort by: {sort_col}")
    print(f"Rank metric: {best['rank_metric']:.2f}")
    print(f"Return [%]: {best['Return [%]']:.2f}")
    print(f"Buy & Hold Return [%]: {best['Buy & Hold Return [%]']:.2f}")
    print(f"Strategy - BuyHold [%]: {best['Strategy - BuyHold [%]']:.2f}")
    print(f"Stop Loss: {best['stop_loss_%']:.2f}%")
    print(f"Take Profit: {best['take_profit_%']:.2f}%")
    print(f"判斷方向時間: {best['range_end_time']}")
    print(f"Max Drawdown [%]: {best['Max. Drawdown [%]']:.2f}")
    print(f"# Trades: {best['# Trades']}")
    print(f"Win Rate [%]: {best['Win Rate [%]']:.2f}")
    print(f"Profit Factor: {best['Profit Factor']:.2f}")
    print(f"Commissions [$]: {best['Commissions [$]']:.2f}")
    print(f"跨日持倉數: {best['overnight_count']}")

    print(f"\n========== 前 {args.top_n} 名 ==========")
    print(topn[[
        "rank_metric",
        "Return [%]",
        "Buy & Hold Return [%]",
        "Strategy - BuyHold [%]",
        "stop_loss_%",
        "take_profit_%",
        "range_end_time",
        "use_gap_filter",
        "min_gap_%",
        "max_gap_%",
        "Max. Drawdown [%]",
        "# Trades",
        "Win Rate [%]",
        "Profit Factor",
        "Commissions [$]",
        "overnight_count",
    ]])

    print("\n已輸出：")
    print(full_path)
    print(top_path)


if __name__ == "__main__":
    main()