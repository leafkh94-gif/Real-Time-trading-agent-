"""
indicators.py — indicator math + swing/pivot detection (pure pandas/numpy).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .strategy_config import (
    RSI_PERIOD, ATR_PERIOD, MACD_FAST, MACD_SLOW, MACD_SIGNAL,
    SWING_LEFT, SWING_RIGHT,
)


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def rsi(close: pd.Series, n: int = RSI_PERIOD) -> pd.Series:
    delta    = close.diff()
    gain     = delta.clip(lower=0.0)
    loss     = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False).mean()
    rs  = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50.0)


def macd(close: pd.Series):
    macd_line   = ema(close, MACD_FAST) - ema(close, MACD_SLOW)
    signal_line = ema(macd_line, MACD_SIGNAL)
    hist        = macd_line - signal_line
    return macd_line, signal_line, hist


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"]  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr


def atr(df: pd.DataFrame, n: int = ATR_PERIOD) -> pd.Series:
    return true_range(df).ewm(alpha=1 / n, adjust=False).mean()


def swings(df: pd.DataFrame, left: int = SWING_LEFT, right: int = SWING_RIGHT):
    """Return (highs, lows) as lists of (index_position, price), oldest→newest."""
    highs, lows = [], []
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    n = len(df)
    for i in range(left, n - right):
        win_h = h[i - left:i + right + 1]
        win_l = l[i - left:i + right + 1]
        if h[i] == win_h.max() and (win_h == h[i]).sum() == 1:
            highs.append((i, h[i]))
        if l[i] == win_l.min() and (win_l == l[i]).sum() == 1:
            lows.append((i, l[i]))
    return highs, lows


def round_number_near(price: float, step: float, prox_frac: float) -> bool:
    if step <= 0:
        return False
    nearest = round(price / step) * step
    return abs(price - nearest) <= price * prox_frac
