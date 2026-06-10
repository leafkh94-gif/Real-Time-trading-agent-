"""
Pure-function technical indicators. No state, no side effects.
All functions accept plain lists or Candle sequences and return lists.
"""
from __future__ import annotations
import math
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


def adx(candles: Sequence[Candle], period: int = 14) -> list[float]:
    """
    Average Directional Index (Wilder, 14-period default).
    Returns a same-length list; leading entries are NaN until enough bars exist.

    Interpretation:
      ADX > 25 — trending market (signals more reliable)
      ADX < 20 — choppy/ranging (suppress signals)
      20-25     — transitional
    """
    n = len(candles)
    if n < 2 * period + 1:
        return [float("nan")] * n

    # +DM, -DM, and aligned TR for bars 1..n-1
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    tr_aligned: list[float] = []
    tr_vals = true_range(candles)

    for i in range(1, n):
        up   = candles[i].high - candles[i - 1].high
        down = candles[i - 1].low - candles[i].low
        plus_dm.append(up   if up > down   and up   > 0 else 0.0)
        minus_dm.append(down if down > up  and down > 0 else 0.0)
        tr_aligned.append(tr_vals[i])

    # Wilder-smooth all three (each has n-1 values)
    s_plus  = wilder_smooth(plus_dm,    period)
    s_minus = wilder_smooth(minus_dm,   period)
    s_tr    = wilder_smooth(tr_aligned, period)

    # DX for each bar in the n-1 window
    dx: list[float] = []
    for sp, sm, st in zip(s_plus, s_minus, s_tr):
        if math.isnan(sp) or math.isnan(st) or st == 0:
            dx.append(float("nan"))
        else:
            pdi   = 100.0 * sp / st
            mdi   = 100.0 * sm / st
            total = pdi + mdi
            dx.append(100.0 * abs(pdi - mdi) / total if total > 0 else 0.0)

    # Smooth DX → ADX, skipping leading NaNs
    first_ok = next((i for i, v in enumerate(dx) if not math.isnan(v)), None)
    if first_ok is None:
        return [float("nan")] * n

    adx_smoothed = wilder_smooth(dx[first_ok:], period)

    # Map back to candle-index space.
    # dx[j] belongs to candle j+1 (we skipped candle 0 when building dx).
    # adx_smoothed[k] corresponds to dx[first_ok + k] → candle first_ok + k + 1.
    result = [float("nan")] * n
    for k, val in enumerate(adx_smoothed):
        idx = first_ok + k + 1
        if idx < n:
            result[idx] = val
    return result
