"""
Key support/resistance level detection for signal confluence filtering.

Levels computed:
  - Previous trading day high and low (from H1 candles)
  - Current week's opening price (first H1 candle of the week)
  - H4 50-period and 200-period Simple Moving Average

near_key_level() returns True (allow signal) if entry is within
threshold_atr × ATR of any level.  Fails open when no levels exist.
"""
from __future__ import annotations
import math
from collections import defaultdict
from typing import Sequence

from strategy.base import Candle


def _sma_last(values: Sequence[float], period: int) -> float | None:
    """SMA of the most recent `period` valid (non-NaN) values, or None."""
    valid = [v for v in values[-period:] if not math.isnan(v)]
    return sum(valid) / len(valid) if len(valid) == period else None


def key_levels(h4_candles: Sequence[Candle], h1_candles: Sequence[Candle]) -> list[float]:
    """
    Return a deduplicated list of significant price levels.
    Includes: prev-day high/low, weekly open, H4 SMA-50, H4 SMA-200.
    """
    levels: list[float] = []

    # ── Previous trading day high/low ────────────────────────────────────────
    if h1_candles:
        days: dict[str, list[Candle]] = defaultdict(list)
        for c in h1_candles:
            days[str(c.timestamp)[:10]].append(c)
        sorted_days = sorted(days.keys())
        if len(sorted_days) >= 2:
            prev = days[sorted_days[-2]]
            levels.append(max(c.high for c in prev))
            levels.append(min(c.low  for c in prev))

    # ── Weekly opening price ─────────────────────────────────────────────────
    if h1_candles:
        import datetime
        today      = datetime.date.today()
        week_start = str(today - datetime.timedelta(days=today.weekday()))
        for c in h1_candles:
            if str(c.timestamp)[:10] >= week_start:
                levels.append(c.open)
                break

    # ── H4 SMA-50 and SMA-200 ────────────────────────────────────────────────
    if h4_candles:
        closes = [c.close for c in h4_candles]
        for period in (50, 200):
            val = _sma_last(closes, period)
            if val is not None:
                levels.append(val)

    return [lv for lv in levels if lv and lv > 0]


def near_key_level(
    entry: float,
    levels: list[float],
    atr_value: float,
    threshold_atr: float = 1.0,
) -> bool:
    """
    Returns True if `entry` is within `threshold_atr × atr_value` of any level.
    Returns True (fail open) when levels is empty or atr_value is zero.
    """
    if not levels or atr_value <= 0:
        return True
    threshold = threshold_atr * atr_value
    return any(abs(entry - lv) <= threshold for lv in levels)
