import argparse
import os
from pathlib import Path
from typing import List, Tuple

import pandas as pd
from backtesting import Backtest

# Reuse the loader / cost / ranking utilities from the optimizer.
# Keep optimize_orb_ib_range_v2.py in the same project folder.
from my_optimize_orb import (
    stg,
    load_ib_csv_range_data,
    select_commission,
    calc_rank_metric,
    safe_float,
    safe_metric,
    format_hhmm,
)


DEFAULT_SYMBOL = "TSLA"
DEFAULT_IB_DATA_DIR = "./ib_data_tsla"
DEFAULT_SEC_TYPE = "STK"
DEFAULT_CASH = 10000
DEFAULT_COMMISSION_MODEL = "ibkr_fixed"
DEFAULT_SPREAD = 0.0002
DEFAULT_DD_WEIGHT = 0.70
DEFAULT_PF_WEIGHT = 5.00
DEFAULT_EXIT_HHMM = 1530
DEFAULT_TOP_N = 20


def parse_bool_value(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "t", "yes", "y"):
        return True
    if text in ("0", "false", "f", "no", "n"):
        return False
    return default


def require_columns(df: pd.DataFrame, columns: List[str], path: Path):
    missing = [col for col in columns if col not in df.columns]
    if missing:
        raise ValueError(
            f"train top CSV 缺少必要欄位: {missing}\n"
            f"檔案: {path}\n"
            f"目前欄位: {list(df.columns)}"
        )


def candidate_key(row) -> Tuple[float, float, int]:
    return (
        round(float(row["stop_loss_pct"]), 8),
        round(float(row["take_profit_pct"]), 8),
        int(row["range_end_hhmm"]),
    )


def load_train_candidates(path: str, top_n: int, dedupe: bool = True) -> pd.DataFrame:
    csv_path = Path(path)
    if not csv_path.exists():
        raise FileNotFoundError(f"找不到 train top CSV: {csv_path}")

    df = pd.read_csv(csv_path)
    require_columns(
        df,
        ["stop_loss_pct", "take_profit_pct", "range_end_hhmm"],
        csv_path,
    )

    if top_n is not None and top_n > 0:
        df = df.head(top_n).copy()
    else:
        df = df.copy()

    if df.empty:
        raise ValueError(f"train top CSV 沒有候選參數: {csv_path}")

    df["train_source_rank"] = range(1, len(df) + 1)

    if dedupe:
        seen = set()
        keep_indices = []
        for idx, row in df.iterrows():
            key = candidate_key(row)
            if key in seen:
                continue
            seen.add(key)
            keep_indices.append(idx)
        df = df.loc[keep_indices].reset_index(drop=True)

    return df


def run_one_validation(bt: Backtest, row, args):
    stop_loss_pct = float(row["stop_loss_pct"])
    take_profit_pct = float(row["take_profit_pct"])
    range_end_hhmm = int(row["range_end_hhmm"])

    if "exit_hhmm" in row and not pd.isna(row["exit_hhmm"]):
        exit_hhmm = int(row["exit_hhmm"])
    else:
        exit_hhmm = int(args.exit_hhmm)

    #if args.force_allow_short is not None:
    #    allow_short = args.force_allow_short
    #elif "allow_short" in row:
    #    allow_short = parse_bool_value(row["allow_short"], default=False)
    #else:
    #    allow_short = False

    stats = bt.run(
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        range_end_hhmm=range_end_hhmm,
        exit_hhmm=exit_hhmm,
        #allow_short=allow_short,
    )

    trades = stats["_trades"]
    if trades.empty:
        overnight_count = 0
    else:
        overnight_count = (
            trades["EntryTime"].dt.date != trades["ExitTime"].dt.date
        ).sum()

    val_rank_metric = calc_rank_metric(stats, args.dd_weight, args.pf_weight)

    train_return = safe_float(row.get("Return [%]"), default=None)
    val_return = safe_float(stats.get("Return [%]"), default=None)

    if train_return is None or val_return is None:
        return_gap = None
    else:
        return_gap = val_return - train_return

    out = {
        "train_source_rank": int(row.get("train_source_rank", 0)),
        "train_rank_metric": safe_float(row.get("rank_metric"), default=None),
        "train_Return [%]": train_return,
        "train_Max. Drawdown [%]": safe_float(row.get("Max. Drawdown [%]"), default=None),
        "train_Profit Factor": safe_float(row.get("Profit Factor"), default=None),
        "train_# Trades": safe_float(row.get("# Trades"), default=None),

        "val_rank_metric": val_rank_metric,
        "val_Return [%]": val_return,
        "val_Buy & Hold Return [%]": safe_float(stats.get("Buy & Hold Return [%]"), default=None),
        "val_Equity Final [$]": safe_float(stats.get("Equity Final [$]"), default=None),
        "val_Max. Drawdown [%]": safe_float(stats.get("Max. Drawdown [%]"), default=None),
        "val_# Trades": safe_float(stats.get("# Trades"), default=None),
        "val_Win Rate [%]": safe_float(stats.get("Win Rate [%]"), default=None),
        "val_Profit Factor": safe_float(stats.get("Profit Factor"), default=None),
        "val_Expectancy [%]": safe_float(stats.get("Expectancy [%]"), default=None),
        "val_Sharpe Ratio": safe_float(stats.get("Sharpe Ratio"), default=None),
        "val_SQN": safe_float(stats.get("SQN"), default=None),
        "val_Commissions [$]": safe_float(stats.get("Commissions [$]"), default=None),
        "val_overnight_count": int(overnight_count),
        "return_gap_val_minus_train": return_gap,

        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
        "stop_loss_%": stop_loss_pct * 100,
        "take_profit_%": take_profit_pct * 100,
        "range_end_hhmm": range_end_hhmm,
        "range_end_time": format_hhmm(range_end_hhmm),
        #"allow_short": allow_short,
        "exit_hhmm": exit_hhmm,
    }

    return out


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "True out-of-sample validation: read train top-N parameters, "
            "apply them directly to a validation IB CSV date range without re-optimizing."
        )
    )

    parser.add_argument("--train-top-csv", required=True)
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument("--ib-data-dir", default=DEFAULT_IB_DATA_DIR)
    parser.add_argument("--sec-type", default=DEFAULT_SEC_TYPE)

    parser.add_argument("--val-start-year", type=int, required=True)
    parser.add_argument("--val-start-month", type=int, required=True)
    parser.add_argument("--val-end-year", type=int, required=True)
    parser.add_argument("--val-end-month", type=int, required=True)
    parser.add_argument("--skip-missing", action="store_true")

    parser.add_argument("--cash", type=float, default=DEFAULT_CASH)
    parser.add_argument("--exit-hhmm", type=int, default=DEFAULT_EXIT_HHMM)
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    parser.add_argument("--no-dedupe", action="store_true")

    #parser.add_argument(
    #    "--force-allow-short",
    #    choices=["true", "false"],
    #    default=None,
    #    help="預設沿用 train CSV 的 allow_short；指定 true/false 可覆蓋。",
    #)

    parser.add_argument(
        "--commission-model",
        default=DEFAULT_COMMISSION_MODEL,
        choices=["ibkr_fixed", "ibkr_tiered", "percent", "none"],
    )
    parser.add_argument("--commission-percent-rate", type=float, default=0.0005)
    parser.add_argument("--spread", type=float, default=DEFAULT_SPREAD)

    parser.add_argument("--dd-weight", type=float, default=DEFAULT_DD_WEIGHT)
    parser.add_argument("--pf-weight", type=float, default=DEFAULT_PF_WEIGHT)
    parser.add_argument(
        "--sort-by",
        default="val_rank_metric",
        choices=["val_rank_metric", "val_return", "train_rank"],
    )

    parser.add_argument("--output-tag", default="train_top_val")

    args = parser.parse_args()

    #if args.force_allow_short is not None:
    #    args.force_allow_short = parse_bool_value(args.force_allow_short)

    return args


def main():
    args = parse_args()
    symbol = args.symbol.upper().strip()

    print("========== 讀取 train top 參數 ==========")
    print(f"Train top CSV: {args.train_top_csv}")
    candidates = load_train_candidates(
        path=args.train_top_csv,
        top_n=args.top_n,
        dedupe=not args.no_dedupe,
    )
    print(f"候選參數組數：{len(candidates)}")
    print(candidates[["train_source_rank", "stop_loss_%", "take_profit_%", "range_end_time"]].head(20)
          if "stop_loss_%" in candidates.columns and "take_profit_%" in candidates.columns and "range_end_time" in candidates.columns
          else candidates[["train_source_rank", "stop_loss_pct", "take_profit_pct", "range_end_hhmm"]].head(20))

    print("\n========== 載入 validation IB CSV 資料 ==========")
    print(f"Symbol: {symbol}")
    print(f"Validation range: {args.val_start_year}-{args.val_start_month:02d} "
          f"to {args.val_end_year}-{args.val_end_month:02d}")

    data = load_ib_csv_range_data(
        symbol=symbol,
        start_year=args.val_start_year,
        start_month=args.val_start_month,
        end_year=args.val_end_year,
        end_month=args.val_end_month,
        data_dir=args.ib_data_dir,
        sec_type=args.sec_type,
        skip_missing=args.skip_missing,
    )

    print(data.head())
    print("...")
    print(data.tail())
    print(f"資料筆數：{len(data)}")
    print(f"第一筆時間：{data.index[0]}")
    print(f"最後一筆時間：{data.index[-1]}")

    commission = select_commission(args.commission_model, args.commission_percent_rate)

    print("\n========== 套用 train top 參數到 validation，不重新最佳化 ==========")
    print(f"策略：{stg.__name__}")
    print(f"Cash: {args.cash}")
    print(f"Commission model: {args.commission_model}")
    print(f"Spread: {args.spread}")
    print(f"Rank metric: Return - {args.dd_weight} * abs(MaxDD) + {args.pf_weight} * (PF - 1)")

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
    for i, (_, row) in enumerate(candidates.iterrows(), start=1):
        try:
            result = run_one_validation(bt, row, args)
            result.update({
                "symbol": symbol,
                "data_source": "ib_csv",
                "train_top_csv": str(args.train_top_csv),
                "val_start_year": args.val_start_year,
                "val_start_month": args.val_start_month,
                "val_end_year": args.val_end_year,
                "val_end_month": args.val_end_month,
                "commission_model": args.commission_model,
                "spread": args.spread,
                "cash": args.cash,
            })
            results.append(result)
            print(
                f"[{i}/{len(candidates)}] "
                f"train_rank={result['train_source_rank']} "
                f"SL={result['stop_loss_%']:.2f}% "
                f"TP={result['take_profit_%']:.2f}% "
                f"range={result['range_end_time']} "
                f"val_return={result['val_Return [%]']:.2f}% "
                f"val_pf={result['val_Profit Factor']:.2f}"
            )
        except Exception as exc:
            print(f"候選參數第 {i} 組失敗: {exc}")

    if not results:
        print("沒有任何成功驗證結果。")
        return

    out = pd.DataFrame(results)

    if args.sort_by == "val_rank_metric":
        out = out.sort_values("val_rank_metric", ascending=False).reset_index(drop=True)
    elif args.sort_by == "val_return":
        out = out.sort_values("val_Return [%]", ascending=False).reset_index(drop=True)
    else:
        out = out.sort_values("train_source_rank", ascending=True).reset_index(drop=True)

    out["val_rank_sort"] = range(1, len(out) + 1)

    os.makedirs("./result", exist_ok=True)

    range_tag = (
        f"{args.val_start_year}_{args.val_start_month:02d}"
        f"_to_{args.val_end_year}_{args.val_end_month:02d}"
    )
    train_name = Path(args.train_top_csv).stem
    output_tag = args.output_tag.strip() or "train_top_val"
    output_path = f"./result/orb_oos_val_{symbol}_{range_tag}_{output_tag}_from_{train_name}.csv"

    out.to_csv(output_path, index=False, encoding="utf-8-sig")

    print("\n========== OOS validation 結果 ==========")
    display_cols = [
        "val_rank_sort",
        "train_source_rank",
        "val_rank_metric",
        "train_Return [%]",
        "val_Return [%]",
        "return_gap_val_minus_train",
        "val_Max. Drawdown [%]",
        "val_Profit Factor",
        "val_# Trades",
        "stop_loss_%",
        "take_profit_%",
        "range_end_time",
    ]
    print(out[display_cols].head(20))

    print("\n已輸出：")
    print(output_path)


if __name__ == "__main__":
    main()
