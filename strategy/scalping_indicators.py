"""
Lightweight indicator helpers for Plan B (M15 scalping strategy).

Pure pandas/numpy — no TA-Lib dependency.
All functions expect a pandas DataFrame with columns: open, high, low, close, volume
indexed chronologically (oldest → newest).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()  # Wilder smoothing


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta    = series.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs  = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(100)   # avg_loss == 0 → RSI 100


def stochastic(
    df: pd.DataFrame,
    k_period: int = 14,
    smooth_k: int = 3,
    smooth_d: int = 3,
) -> tuple[pd.Series, pd.Series]:
    low_min  = df["low"].rolling(k_period).min()
    high_max = df["high"].rolling(k_period).max()
    raw_k = 100 * (df["close"] - low_min) / (high_max - low_min).replace(0, np.nan)
    k = raw_k.rolling(smooth_k).mean()
    d = k.rolling(smooth_d).mean()
    return k, d


def ema_slope_pct(series: pd.Series, lookback: int = 3) -> float:
    """Percent slope of `series` over the last `lookback` bars."""
    if len(series) < lookback + 1:
        return 0.0
    start = series.iloc[-(lookback + 1)]
    end   = series.iloc[-1]
    if start == 0 or pd.isna(start) or pd.isna(end):
        return 0.0
    return (end - start) / abs(start) * 100.0


def swing_high(df: pd.DataFrame, lookback: int, confirm_bars: int) -> float | None:
    """Most recent confirmed swing high in the last `lookback` bars."""
    highs  = df["high"]
    window = highs.iloc[-lookback:]
    n = len(window)
    for i in range(n - confirm_bars - 1, confirm_bars - 1, -1):
        left   = window.iloc[i - confirm_bars : i]
        right  = window.iloc[i + 1 : i + 1 + confirm_bars]
        center = window.iloc[i]
        if center >= left.max() and center >= right.max():
            return float(center)
    return None


def swing_low(df: pd.DataFrame, lookback: int, confirm_bars: int) -> float | None:
    """Most recent confirmed swing low in the last `lookback` bars."""
    lows   = df["low"]
    window = lows.iloc[-lookback:]
    n = len(window)
    for i in range(n - confirm_bars - 1, confirm_bars - 1, -1):
        left   = window.iloc[i - confirm_bars : i]
        right  = window.iloc[i + 1 : i + 1 + confirm_bars]
        center = window.iloc[i]
        if center <= left.min() and center <= right.min():
            return float(center)
    return None
