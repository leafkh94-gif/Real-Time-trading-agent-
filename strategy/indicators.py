"""
Pure-function technical indicators. No state, no side effects.
All functions accept plain lists or Candle sequences and return lists.
"""
from __future__ import annotations
from typing import Sequence
from strategy.base import Candle


def ema(prices: Sequence[float], period: int) -> list[float]:
    """
    Exponential moving average. Returns a list the same length as prices;
    leading values (before the first full period) are seeded from the SMA.
    """
    if len(prices) < period:
        return []
    k = 2.0 / (period + 1)
    result: list[float] = []
    seed = sum(prices[:period]) / period
    result.append(seed)
    for p in prices[period:]:
        result.append(p * k + result[-1] * (1 - k))
    # Pad the front so indices align with the input
    return [float("nan")] * (period - 1) + result


def wilder_smooth(values: Sequence[float], period: int) -> list[float]:
    """Wilder's smoothing (used for ATR). Same index-alignment contract as ema()."""
    if len(values) < period:
        return []
    seed = sum(values[:period]) / period
    result = [seed]
    for v in values[period:]:
        result.append((result[-1] * (period - 1) + v) / period)
    return [float("nan")] * (period - 1) + result


def true_range(candles: Sequence[Candle]) -> list[float]:
    """True range for each bar (first bar has no previous close — uses high-low)."""
    tr = []
    for i, c in enumerate(candles):
        if i == 0:
            tr.append(c.high - c.low)
        else:
            prev_close = candles[i - 1].close
            tr.append(max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close)))
    return tr


def atr(candles: Sequence[Candle], period: int = 14) -> list[float]:
    """Average True Range using Wilder's smoothing."""
    return wilder_smooth(true_range(candles), period)


def swing_highs(candles: Sequence[Candle], lookback: int = 5) -> list[float | None]:
    """
    Returns a parallel list; index i holds the swing-high price if candle i is a
    confirmed pivot high (higher than `lookback` bars on each side), else None.
    Only indices [lookback .. len-lookback-1] can be pivots.
    """
    n = len(candles)
    result: list[float | None] = [None] * n
    for i in range(lookback, n - lookback):
        if all(candles[i].high > candles[j].high for j in range(i - lookback, i + lookback + 1) if j != i):
            result[i] = candles[i].high
    return result


def swing_lows(candles: Sequence[Candle], lookback: int = 5) -> list[float | None]:
    """Parallel list of confirmed pivot lows."""
    n = len(candles)
    result: list[float | None] = [None] * n
    for i in range(lookback, n - lookback):
        if all(candles[i].low < candles[j].low for j in range(i - lookback, i + lookback + 1) if j != i):
            result[i] = candles[i].low
    return result
