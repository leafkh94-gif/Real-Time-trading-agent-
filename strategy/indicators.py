"""
indicators.py — indicator math + swing/pivot detection (pure pandas/numpy).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .strategy_config import (
    RSI_PERIOD, ATR_PERIOD, ADX_PERIOD, MACD_FAST, MACD_SLOW, MACD_SIGNAL,
    SWING_LEFT, SWING_RIGHT, VWAP_PERIOD,
    VP_LOOKBACK, VP_BINS, VP_VALUE_AREA,
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


def vwap(df: pd.DataFrame, period: int = VWAP_PERIOD) -> pd.Series:
    """Rolling VWAP over `period` bars (directional reference, not a volume indicator).
    Falls back to a simple price-average when volume is all-zero (e.g. CFD tick volume)."""
    tp  = (df["high"] + df["low"] + df["close"]) / 3
    vol = df["volume"].replace(0, np.nan)
    cum_tp_vol = (tp * vol).rolling(period, min_periods=1).sum()
    cum_vol    = vol.rolling(period, min_periods=1).sum()
    result = cum_tp_vol / cum_vol
    return result.fillna(tp.rolling(period, min_periods=1).mean())


def anchored_vwap(df: pd.DataFrame, anchor_idx: int) -> pd.Series:
    """VWAP anchored at a specific bar (e.g. the most recent major swing).
    Cumulative from the anchor forward — the classic order-flow reference:
    price above it means the average participant since the anchor is long in profit.
    Falls back to an expanding price-average when volume is all-zero."""
    sub = df.iloc[max(0, anchor_idx):]
    tp  = (sub["high"] + sub["low"] + sub["close"]) / 3
    vol = sub["volume"].replace(0, np.nan)
    result = (tp * vol).cumsum() / vol.cumsum()
    return result.fillna(tp.expanding().mean())


def volume_profile(df: pd.DataFrame, lookback: int = VP_LOOKBACK,
                   bins: int = VP_BINS, va_pct: float = VP_VALUE_AREA):
    """Volume profile over the last `lookback` bars: POC + value area.
    Each bar's volume is spread uniformly across the price bins its range covers.
    NOTE: on CFD feeds volume is tick count — treat the result as an
    approximation of activity concentration, not true traded volume.
    Returns {"poc", "vah", "val"} or None if the range is degenerate."""
    sub = df.iloc[-lookback:]
    lo  = float(sub["low"].min())
    hi  = float(sub["high"].max())
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        return None
    edges    = np.linspace(lo, hi, bins + 1)
    vol_bins = np.zeros(bins)
    lows  = sub["low"].to_numpy()
    highs = sub["high"].to_numpy()
    vols  = sub["volume"].to_numpy(dtype=float)
    for i in range(len(sub)):
        b_lo = min(bins - 1, max(0, int(np.searchsorted(edges, lows[i],  side="right")) - 1))
        b_hi = min(bins - 1, max(0, int(np.searchsorted(edges, highs[i], side="right")) - 1))
        v = vols[i] if vols[i] > 0 else 1.0     # tick volume can be 0 — count the bar itself
        vol_bins[b_lo:b_hi + 1] += v / (b_hi - b_lo + 1)
    poc_i = int(vol_bins.argmax())
    poc   = float((edges[poc_i] + edges[poc_i + 1]) / 2)
    # Expand the value area from the POC, always absorbing the heavier neighbour.
    total   = float(vol_bins.sum())
    covered = vol_bins[poc_i]
    lo_i = hi_i = poc_i
    while covered < va_pct * total and (lo_i > 0 or hi_i < bins - 1):
        below = vol_bins[lo_i - 1] if lo_i > 0 else -1.0
        above = vol_bins[hi_i + 1] if hi_i < bins - 1 else -1.0
        if above >= below:
            hi_i += 1; covered += vol_bins[hi_i]
        else:
            lo_i -= 1; covered += vol_bins[lo_i]
    return {"poc": poc, "vah": float(edges[hi_i + 1]), "val": float(edges[lo_i])}


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


def adx(df: pd.DataFrame, n: int = ADX_PERIOD) -> pd.Series:
    """Average Directional Index — measures trend strength (not direction).
    ADX < 20 indicates a choppy / directionless market (v3.1 rule on H1)."""
    high = df["high"]; low = df["low"]
    up_move   = high - high.shift(1)
    down_move = low.shift(1) - low
    plus_dm  = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=df.index)
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=df.index)
    tr_smooth   = true_range(df).ewm(alpha=1 / n, adjust=False).mean()
    plus_di  = 100 * plus_dm.ewm(alpha=1 / n, adjust=False).mean() / tr_smooth.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1 / n, adjust=False).mean() / tr_smooth.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean().fillna(0.0)


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
