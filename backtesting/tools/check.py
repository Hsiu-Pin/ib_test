import argparse
import os
import re
from pathlib import Path
from typing import Optional, Iterator, Tuple, List

import pandas as pd

# ============================================================
# 2. 小工具
# ============================================================

def format_hhmm(hhmm):
    hhmm = int(hhmm)
    hour = hhmm // 100
    minute = hhmm % 100
    return f"{hour:02d}:{minute:02d}"


def safe_float(value, default=None):
    try:
        if value is None:
            return default
        if pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def safe_metric(value, default=0.0):
    parsed = safe_float(value, default=None)
    if parsed is None:
        return default
    return parsed


def validate_year_month(year: int, month: int, label: str):
    if year is None or month is None:
        raise ValueError(f"{label} year/month 不可為 None")
    if not isinstance(year, int) or not isinstance(month, int):
        raise ValueError(f"{label} year/month 必須是 int")
    if year < 1900 or year > 2100:
        raise ValueError(f"{label} year 不合理: {year}")
    if month < 1 or month > 12:
        raise ValueError(f"{label} month 必須在 1~12: {month}")


def month_key(year: int, month: int) -> int:
    return year * 12 + month


def iter_months(start_year: int, start_month: int, end_year: int, end_month: int) -> Iterator[Tuple[int, int]]:
    validate_year_month(start_year, start_month, "start")
    validate_year_month(end_year, end_month, "end")

    if month_key(start_year, start_month) > month_key(end_year, end_month):
        raise ValueError(
            f"起點月份不可晚於終點月份: "
            f"{start_year}-{start_month:02d} > {end_year}-{end_month:02d}"
        )

    year = start_year
    month = start_month

    while month_key(year, month) <= month_key(end_year, end_month):
        yield year, month
        month += 1
        if month == 13:
            month = 1
            year += 1


def next_month_start(year: int, month: int) -> pd.Timestamp:
    if month == 12:
        return pd.Timestamp(year=year + 1, month=1, day=1)
    return pd.Timestamp(year=year, month=month + 1, day=1)


def pct_range_values(start_pct: float, end_pct: float, step_pct: float) -> List[float]:
    """
    輸入百分比單位，輸出小數單位。

    例如：
        start_pct=1.0, end_pct=2.0, step_pct=0.25
    回傳：
        [0.01, 0.0125, 0.015, 0.0175, 0.02]
    """
    if start_pct is None or end_pct is None or step_pct is None:
        raise ValueError("pct range 不可為 None")
    if step_pct <= 0:
        raise ValueError(f"step_pct 必須 > 0: {step_pct}")
    if start_pct > end_pct:
        raise ValueError(f"start_pct 不可大於 end_pct: {start_pct} > {end_pct}")

    values = []
    x = start_pct
    # 用整數 tick 避免 float 累積誤差
    ticks = 0
    max_ticks = 10000
    while x <= end_pct + 1e-9:
        values.append(round(x / 100.0, 8))
        ticks += 1
        if ticks > max_ticks:
            raise RuntimeError("pct range 產生太多值，請檢查 step")
        x = start_pct + ticks * step_pct
    return values


def parse_int_list(text: str) -> List[int]:
    if text is None:
        return []
    values = []
    for token in str(text).split(","):
        token = token.strip()
        if not token:
            continue
        values.append(int(token))
    if not values:
        raise ValueError(f"無法解析 int list: {text}")
    return values


def parse_ib_datetime(value) -> Optional[pd.Timestamp]:
    """
    解析 IB historical CSV 的 date 欄位。

    可能格式：
        20260626 09:40:00 US/Eastern
        20260626 09:40:00
        20260626

    backtesting.py 通常使用 naive DatetimeIndex 最省事。
    這裡會把 US/Eastern 字串去掉，但不做時區轉換。
    """
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    match = re.match(
        r"^(\d{8})(?:\s+(\d{2}:\d{2}:\d{2}))?(?:\s+[A-Za-z_]+/[A-Za-z_]+)?$",
        text,
    )
    if match:
        yyyymmdd = match.group(1)
        hhmmss = match.group(2)
        cleaned = f"{yyyymmdd} {hhmmss}" if hhmmss else yyyymmdd

        for fmt in ("%Y%m%d %H:%M:%S", "%Y%m%d"):
            try:
                return pd.Timestamp(pd.to_datetime(cleaned, format=fmt))
            except Exception:
                continue

    try:
        ts = pd.Timestamp(pd.to_datetime(text))
        if ts.tzinfo is not None:
            ts = ts.tz_localize(None)
        return ts
    except Exception:
        return None

