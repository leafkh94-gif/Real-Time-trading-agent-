"""
scoring_strategy.py — the strategy engine described in the strategy document.

Pipeline per instrument:
  1. detect a pattern on the M15 (entry) timeframe   -> Factor 1
  2. score technical confirmation (RSI/MACD/EMA)      -> Factor 2
  3. score daily bias (EMA50/200)                     -> Factor 3
  4. score MA20 alignment on M15                      -> Factor 4
  5. score session timing (per instrument)            -> Factor 5
  6. additional factors (round number, volume, vol/chop penalties)
  -> total score -> WATCH / A+ / nothing
  -> build entry / SL / TP1 / TP2 per pattern type and instrument ATR
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from . import indicators as ind
from . import strategy_config as C
from .market_sessions import session_score


# ── Data contract ──────────────────────────────────────────────────
@dataclass
class MarketData:
    epic:     str
    m15:      pd.DataFrame   # columns: open, high, low, close, volume (oldest→newest)
    daily:    pd.DataFrame
    now_utc:  dt.datetime


@dataclass
class Signal:
    epic:          str
    name:          str
    direction:     str            # "buy" | "sell"
    pattern:       str            # key in C.PATTERNS
    pattern_label: str
    score:         int
    tier:          str            # "WATCH" | "A+"
    entry:         float
    stop_loss:     float
    take_profit:   float
    take_profit2:  float
    rr:            float
    reasons:       list[str] = field(default_factory=list)
    components:    dict       = field(default_factory=dict)
    expiry_utc:    Optional[dt.datetime] = None


# ── Pattern detectors ──────────────────────────────────────────────────
def _detect_sweep_bos(df: pd.DataFrame, a: pd.Series) -> Optional[dict]:
    """Liquidity sweep of a swing extreme, reclaim, then BOS."""
    highs, lows = ind.swings(df)
    n    = len(df)
    look = min(C.SIGNAL_LOOKBACK, n - 1)
    last = n - 1
    close = df["close"].to_numpy()
    low   = df["low"].to_numpy()
    high  = df["high"].to_numpy()

    recent_lows  = [(i, p) for i, p in lows  if i >= last - look]
    recent_highs = [(i, p) for i, p in highs if i >= last - look]

    # Bullish
    if recent_lows and recent_highs:
        li, lp = recent_lows[-1]
        later_highs = [(i, p) for i, p in recent_highs if i > li]
        if later_highs:
            hi, hp = later_highs[-1]
            swept = any(low[j] < lp and close[j] > lp for j in range(li, min(li + 8, n)))
            bos   = close[last] > hp and close[last - 1] <= hp
            if swept and bos:
                sweep_low = min(low[li:last + 1])
                impulse   = close[last] - sweep_low
                clean     = (close[last] - hp) / max(impulse, 1e-9)
                bonus     = float(np.clip(4 + clean * 60, 0, C.PATTERNS["sweep_bos"]["max_bonus"]))
                return {"direction": "buy", "pattern": "sweep_bos", "bonus": bonus,
                        "broken_level": hp, "ref_low": sweep_low, "ref_high": close[last],
                        "confirm_price": close[last]}

    # Bearish
    if recent_highs and recent_lows:
        hi, hp = recent_highs[-1]
        later_lows = [(i, p) for i, p in recent_lows if i > hi]
        if later_lows:
            li, lp = later_lows[-1]
            swept = any(high[j] > hp and close[j] < hp for j in range(hi, min(hi + 8, n)))
            bos   = close[last] < lp and close[last - 1] >= lp
            if swept and bos:
                sweep_high = max(high[hi:last + 1])
                impulse    = sweep_high - close[last]
                clean      = (lp - close[last]) / max(impulse, 1e-9)
                bonus      = float(np.clip(4 + clean * 60, 0, C.PATTERNS["sweep_bos"]["max_bonus"]))
                return {"direction": "sell", "pattern": "sweep_bos", "bonus": bonus,
                        "broken_level": lp, "ref_low": close[last], "ref_high": sweep_high,
                        "confirm_price": close[last]}
    return None


def _detect_flag(df: pd.DataFrame, a: pd.Series) -> Optional[dict]:
    """Strong impulse then a shallow consolidation, breakout in impulse direction."""
    n = len(df)
    if n < 25:
        return None
    close   = df["close"]
    atr_now = float(a.iloc[-1])
    impulse = float(close.iloc[-15:-5].iloc[-1] - close.iloc[-15:-5].iloc[0])
    if abs(impulse) < 3 * atr_now:
        return None
    consolidation = df.iloc[-5:]
    cons_range    = consolidation["high"].max() - consolidation["low"].min()
    if cons_range > 1.5 * atr_now:
        return None
    direction = "buy" if impulse > 0 else "sell"
    last      = float(close.iloc[-1])
    prev      = float(close.iloc[-2])
    flag_hi   = float(consolidation["high"].iloc[:-1].max())
    flag_lo   = float(consolidation["low"].iloc[:-1].min())
    if direction == "buy" and last > flag_hi >= prev:
        bonus = float(np.clip(abs(impulse) / atr_now, 0, C.PATTERNS["flag"]["max_bonus"]))
        return {"direction": "buy", "pattern": "flag", "bonus": bonus,
                "broken_level": flag_hi, "ref_low": flag_lo, "ref_high": last,
                "confirm_price": last}
    if direction == "sell" and last < flag_lo <= prev:
        bonus = float(np.clip(abs(impulse) / atr_now, 0, C.PATTERNS["flag"]["max_bonus"]))
        return {"direction": "sell", "pattern": "flag", "bonus": bonus,
                "broken_level": flag_lo, "ref_low": last, "ref_high": flag_hi,
                "confirm_price": last}
    return None


def _detect_sd_rejection(df: pd.DataFrame, a: pd.Series) -> Optional[dict]:
    """Price reaches a prior swing level and prints a rejection wick."""
    highs, lows = ind.swings(df)
    n       = len(df)
    last    = n - 1
    o       = float(df["open"].iloc[-1])
    c       = float(df["close"].iloc[-1])
    h       = float(df["high"].iloc[-1])
    l       = float(df["low"].iloc[-1])
    rng     = max(h - l, 1e-9)
    body    = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    atr_now    = float(a.iloc[-1])

    for i, lp in reversed(lows[-12:] if lows else []):
        if i >= last:
            continue
        if abs(l - lp) <= 0.5 * atr_now and lower_wick > 0.5 * rng and c > o:
            bonus = float(np.clip(lower_wick / rng * 10, 0, C.PATTERNS["sd_rejection"]["max_bonus"]))
            return {"direction": "buy", "pattern": "sd_rejection", "bonus": bonus,
                    "broken_level": lp, "ref_low": l, "ref_high": c, "confirm_price": c}
    for i, hp in reversed(highs[-12:] if highs else []):
        if i >= last:
            continue
        if abs(h - hp) <= 0.5 * atr_now and upper_wick > 0.5 * rng and c < o:
            bonus = float(np.clip(upper_wick / rng * 10, 0, C.PATTERNS["sd_rejection"]["max_bonus"]))
            return {"direction": "sell", "pattern": "sd_rejection", "bonus": bonus,
                    "broken_level": hp, "ref_low": c, "ref_high": h, "confirm_price": c}
    return None


def _detect_reversal(df: pd.DataFrame, a: pd.Series) -> Optional[dict]:
    """Double bottom/top (and simplified H&S as a triple variant)."""
    highs, lows = ind.swings(df)
    n       = len(df)
    last    = n - 1
    atr_now = float(a.iloc[-1])
    c       = float(df["close"].iloc[-1])
    prev    = float(df["close"].iloc[-2])

    rl = [(i, p) for i, p in lows  if i >= last - C.SIGNAL_LOOKBACK]
    if len(rl) >= 2:
        (i1, p1), (i2, p2) = rl[-2], rl[-1]
        if abs(p1 - p2) <= 0.6 * atr_now:
            necks = [p for i, p in highs if i1 < i < i2]
            if necks:
                neck = max(necks)
                if c > neck >= prev:
                    return {"direction": "buy", "pattern": "reversal", "bonus": 6.0,
                            "broken_level": neck, "ref_low": min(p1, p2), "ref_high": c,
                            "confirm_price": c}
    rh = [(i, p) for i, p in highs if i >= last - C.SIGNAL_LOOKBACK]
    if len(rh) >= 2:
        (i1, p1), (i2, p2) = rh[-2], rh[-1]
        if abs(p1 - p2) <= 0.6 * atr_now:
            necks = [p for i, p in lows if i1 < i < i2]
            if necks:
                neck = min(necks)
                if c < neck <= prev:
                    return {"direction": "sell", "pattern": "reversal", "bonus": 6.0,
                            "broken_level": neck, "ref_low": c, "ref_high": max(p1, p2),
                            "confirm_price": c}
    return None


def _detect_news_retest(df: pd.DataFrame, a: pd.Series) -> Optional[dict]:
    """After a strong directional break, price retests the broken level and holds."""
    n = len(df)
    if n < 20:
        return None
    atr_now = float(a.iloc[-1])
    window  = df.iloc[-12:]
    move    = float(window["close"].iloc[-1] - window["close"].iloc[0])
    if abs(move) < 2.5 * atr_now:
        return None
    level = float(window["close"].iloc[0])
    c = float(df["close"].iloc[-1])
    l = float(df["low"].iloc[-1])
    h = float(df["high"].iloc[-1])
    if move > 0 and l <= level <= c and abs(l - level) <= 0.8 * atr_now:
        return {"direction": "buy", "pattern": "news_retest", "bonus": 4.0,
                "broken_level": level, "ref_low": l, "ref_high": c, "confirm_price": c}
    if move < 0 and c <= level <= h and abs(h - level) <= 0.8 * atr_now:
        return {"direction": "sell", "pattern": "news_retest", "bonus": 4.0,
                "broken_level": level, "ref_low": c, "ref_high": h, "confirm_price": c}
    return None


_DETECTORS = [_detect_sweep_bos, _detect_reversal, _detect_sd_rejection,
              _detect_flag, _detect_news_retest]


# ── Factor scoring ───────────────────────────────────────────────────
def _technical_confirmation(df: pd.DataFrame, direction: str) -> tuple[int, list[str]]:
    close   = df["close"]
    r       = float(ind.rsi(close).iloc[-1])
    _, _, hist = ind.macd(close)
    macd_h  = float(hist.iloc[-1])
    ema50   = float(ind.ema(close, 50).iloc[-1])
    price   = float(close.iloc[-1])
    want_buy = direction == "buy"
    agree = []
    if (r > 50) == want_buy:       agree.append(f"RSI {r:.0f}")
    if (macd_h > 0) == want_buy:   agree.append("MACD")
    if (price > ema50) == want_buy: agree.append("EMA50")
    k   = len(agree)
    pts = C.CONF_2_OR_3 if k >= 2 else (C.CONF_1 if k == 1 else C.CONF_0)
    return pts, agree


def _daily_bias(daily: pd.DataFrame, direction: str) -> tuple[int, str]:
    close  = daily["close"]
    e50    = float(ind.ema(close, C.EMA_FAST_BIAS).iloc[-1])
    e200   = float(ind.ema(close, C.EMA_SLOW_BIAS).iloc[-1])
    price  = float(close.iloc[-1])
    spread = abs(e50 - e200) / max(price, 1e-9)
    if spread < 0.001:
        return C.BIAS_NEUTRAL, "neutral"
    up   = e50 > e200 and price > e200
    down = e50 < e200 and price < e200
    if up   and direction == "buy":  return C.BIAS_ALIGNED, "aligned-up"
    if down and direction == "sell": return C.BIAS_ALIGNED, "aligned-down"
    if (up and direction == "sell") or (down and direction == "buy"):
        return C.BIAS_COUNTER, "counter-trend"
    return C.BIAS_NEUTRAL, "neutral"


def _ma20_factor(df: pd.DataFrame, direction: str) -> tuple[int, str]:
    price = float(df["close"].iloc[-1])
    ma20  = float(ind.sma(df["close"], C.MA20_PERIOD).iloc[-1])
    rel   = (price - ma20) / max(price, 1e-9)
    if abs(rel) < C.MA20_NEUTRAL_BAND:
        return C.MA20_NEUTRAL, "neutral"
    aligned = (rel > 0) == (direction == "buy")
    return (C.MA20_ALIGNED, "aligned") if aligned else (C.MA20_COUNTER, "counter")


def _choppy(df: pd.DataFrame, a: pd.Series) -> bool:
    e     = ind.ema(df["close"], 50)
    slope = float(e.iloc[-1] - e.iloc[-10]) if len(e) > 10 else 0.0
    return abs(slope) < 0.5 * float(a.iloc[-1])


def _high_atr(a: pd.Series) -> bool:
    recent = a.dropna().iloc[-100:]
    if len(recent) < 20:
        return False
    return float(a.iloc[-1]) >= float(recent.quantile(C.HIGH_ATR_PCTILE))


# ── Entry / SL / TP construction ─────────────────────────────────────────────
def _build_levels(epic: str, det: dict, atr_now: float) -> Optional[dict]:
    cfg       = C.INSTRUMENTS[epic]
    ptype     = C.PATTERNS[det["pattern"]]["type"]
    direction = det["direction"]
    lvl       = det["broken_level"]
    ref_low, ref_high = det["ref_low"], det["ref_high"]

    if ptype == "breakout":
        extreme = ref_high if direction == "buy" else ref_low
        half    = (lvl + 0.5 * (extreme - lvl) if direction == "buy"
                   else lvl - 0.5 * (lvl - extreme))
        if abs(half - lvl) > 1.5 * atr_now:
            entry = lvl + np.sign(extreme - lvl) * 0.3 * atr_now
        else:
            entry = half
    else:
        entry = det["confirm_price"]

    if direction == "buy":
        raw_sl = min(ref_low, entry) - 0.1 * atr_now
        dist   = entry - raw_sl
    else:
        raw_sl = max(ref_high, entry) + 0.1 * atr_now
        dist   = raw_sl - entry
    dist = float(np.clip(dist, cfg["atr_min"] * atr_now, cfg["atr_max"] * atr_now))
    sl   = entry - dist if direction == "buy" else entry + dist

    tp1 = entry + dist * C.MIN_RR       if direction == "buy" else entry - dist * C.MIN_RR
    tp2 = entry + dist * (C.MIN_RR + 1) if direction == "buy" else entry - dist * (C.MIN_RR + 1)
    rr  = abs(tp1 - entry) / max(abs(entry - sl), 1e-9)
    return {"entry": entry, "stop_loss": sl, "take_profit": tp1,
            "take_profit2": tp2, "rr": rr}


# ── Engine ───────────────────────────────────────────────────────────
class ScoringStrategy:
    def __init__(self, epic: str, a_plus_threshold: float | None = None):
        self.epic = epic
        self.cfg  = C.INSTRUMENTS[epic]
        self.a_plus_threshold = a_plus_threshold if a_plus_threshold is not None else C.A_PLUS_BASE

    def evaluate(self, md: MarketData) -> Optional[Signal]:
        df = md.m15
        if df is None or len(df) < 60 or md.daily is None or len(md.daily) < 60:
            return None
        a       = ind.atr(df)
        atr_now = float(a.iloc[-1])
        if not np.isfinite(atr_now) or atr_now <= 0:
            return None

        det = next((d for d in (f(df, a) for f in _DETECTORS) if d), None)
        if det is None:
            return None
        direction = det["direction"]
        pat       = C.PATTERNS[det["pattern"]]

        reasons: list[str] = []
        comp:    dict      = {}

        # Factor 1
        f1 = pat["base"] + min(det["bonus"], pat["max_bonus"])
        comp["pattern"] = round(f1, 1)
        reasons.append(f"{pat['label']} ({direction.upper()})")

        # Factor 2
        f2, agree = _technical_confirmation(df, direction)
        comp["confirmation"] = f2
        if agree:
            reasons.append("Confirm: " + ", ".join(agree))

        # Factor 3
        f3, bias_state = _daily_bias(md.daily, direction)
        comp["daily_bias"] = f3
        reasons.append(f"Daily bias: {bias_state} ({f3:+d})")

        # Factor 4
        f4, ma_state = _ma20_factor(df, direction)
        comp["ma20"] = f4

        # Factor 5
        f5 = session_score(self.cfg["session"], md.now_utc)
        comp["session"] = f5

        # Additional
        add   = 0
        price = float(df["close"].iloc[-1])
        if ind.round_number_near(price, self.cfg["round_step"], self.cfg["round_prox"]):
            add += C.ROUND_NUMBER_BONUS
            reasons.append("Near round number (+5)")
        vol = df["volume"]
        if len(vol) >= 2 and float(vol.iloc[-1]) > float(vol.iloc[-2]):
            add += C.VOLUME_CONFIRM_BONUS
            comp["volume_confirm"] = C.VOLUME_CONFIRM_BONUS
        if _high_atr(a):
            add += C.HIGH_ATR_PENALTY
            reasons.append("High volatility (-10)")
        if _choppy(df, a):
            add += C.CHOPPY_PENALTY
            reasons.append("Choppy market (-10)")
        comp["additional"] = add

        score = int(round(f1 + f2 + f3 + f4 + f5 + add))
        if score < C.WATCH_MIN:
            return None
        tier = "A+" if score >= self.a_plus_threshold else "WATCH"

        levels = _build_levels(self.epic, det, atr_now)
        if levels is None:
            return None
        if tier == "A+" and levels["rr"] < C.MIN_RR - 1e-6:
            tier = "WATCH"

        expiry = md.now_utc + dt.timedelta(minutes=C.SETUP_EXPIRY_MIN)
        return Signal(
            epic=self.epic, name=self.cfg["name"], direction=direction,
            pattern=det["pattern"], pattern_label=pat["label"],
            score=score, tier=tier, reasons=reasons, components=comp,
            expiry_utc=expiry, **levels,
        )
