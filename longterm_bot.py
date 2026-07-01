"""
Capital.com Alert Bot — US100 / US500 / US30 / BTC
5-Pattern SMC Scoring System  |  H1 + Daily timeframes

PATTERNS (base pts):
  1. Liquidity Sweep + BOS  38 pts
  2. Supply / Demand Zone   37 pts
  3. Double Top / Bottom    37 pts
  4. Bull / Bear Flag       36 pts

SCORING FACTORS:
  Pattern quality    0–15 pts
  Technical (RSI+MACD+EMA)  0/+4/+10
  Daily bias (EMA50/200)  -8/+5/+15
  MA20 filter          -3/0/+4
  Session bonus         +1–6
  Additional (round#, volume, choppy)  -10..+8

THRESHOLDS:
  <62     → no alert
  62–74   → WATCH ⚡
  ≥75     → A+ 🟢  (adaptive, adjusts if signals are too rare/frequent)

ENTRY: Limit at 50% retracement after BOS; falls back to zone level if
       pullback exceeds 1.5×ATR.

CREDENTIALS (.env or GitHub Secrets):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  CAPITAL_API_KEY, CAPITAL_IDENTIFIER, CAPITAL_PASSWORD
  CAPITAL_DEMO=true   (false for live account)
"""

import json
import logging
import math
import os
import random
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Credentials & tuning ──────────────────────────────────────────────────────

BOT_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID       = os.getenv("TELEGRAM_CHAT_ID", "")
CAP_API_KEY   = os.getenv("CAPITAL_API_KEY", "")
CAP_ID        = os.getenv("CAPITAL_IDENTIFIER", "")
CAP_PASSWORD  = os.getenv("CAPITAL_PASSWORD", "")
CAP_DEMO      = os.getenv("CAPITAL_DEMO", "true").lower() != "false"
COOLDOWN_FILE = os.getenv("COOLDOWN_FILE", ".alert_cooldown.json")

SCAN_INTERVAL_S    = 5 * 60
ALERT_COOLDOWN_S   = 60 * 60
THRESHOLD_A_PLUS   = 75
THRESHOLD_WATCH    = 62
MAX_DAILY_SIGNALS  = 4

_DEMO_BASE = "https://demo-api-capital.backend-capital.com/api/v1"
_LIVE_BASE = "https://api-capital.backend-capital.com/api/v1"
_BASE      = _DEMO_BASE if CAP_DEMO else _LIVE_BASE
_TIMEOUT   = 15

US_INDEX_EPICS = frozenset({"US500", "US100", "US30"})


# ── Instruments ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Instrument:
    epic:         str
    name:         str
    sl_atr_mult:  float = 0.50
    sweep_tol:    float = 0.20
    max_sl_atr:   float = 1.8


INSTRUMENTS: list[Instrument] = [
    Instrument("US500", "S&P 500",    sl_atr_mult=0.50, sweep_tol=0.20, max_sl_atr=1.8),
    Instrument("US100", "Nasdaq 100", sl_atr_mult=0.60, sweep_tol=0.25, max_sl_atr=2.0),
    Instrument("US30",  "Dow Jones",  sl_atr_mult=0.55, sweep_tol=0.20, max_sl_atr=1.8),
    Instrument("BTC",   "Bitcoin",    sl_atr_mult=0.60, sweep_tol=0.25, max_sl_atr=2.2),
]


# ── Candle ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Candle:
    ts:     str
    open:   float
    high:   float
    low:    float
    close:  float
    volume: float = 0.0


# ── Capital.com session ───────────────────────────────────────────────────────

_cst:              str = ""
_sec_token:        str = ""
_session_lock          = threading.Lock()
_keepalive_started     = False

logger = logging.getLogger(__name__)


def _login() -> None:
    global _cst, _sec_token
    for attempt in range(1, 6):
        try:
            r = requests.post(
                f"{_BASE}/session",
                headers={"X-CAP-API-KEY": CAP_API_KEY, "Content-Type": "application/json"},
                json={"identifier": CAP_ID, "password": CAP_PASSWORD, "encryptedPassword": False},
                timeout=_TIMEOUT,
            )
            if r.status_code in (400, 401, 403):
                raise RuntimeError(f"Credentials rejected (HTTP {r.status_code}): {r.text[:200]}")
            r.raise_for_status()
            _cst       = r.headers["CST"]
            _sec_token = r.headers["X-SECURITY-TOKEN"]
            logger.info("Capital.com session created (attempt %d)", attempt)
            return
        except RuntimeError:
            raise
        except Exception as exc:
            wait = min(60, 2 ** attempt) + random.uniform(0, 2)
            logger.warning("Login attempt %d failed: %s — retrying in %.1fs", attempt, exc, wait)
            time.sleep(wait)
    raise RuntimeError("Capital.com login failed after 5 attempts")


def _auth_headers() -> dict:
    with _session_lock:
        return {"CST": _cst, "X-SECURITY-TOKEN": _sec_token,
                "Content-Type": "application/json"}


def _api_request(method: str, path: str, **kwargs) -> requests.Response:
    hdrs = _auth_headers()
    r = requests.request(method, f"{_BASE}{path}", headers=hdrs, timeout=_TIMEOUT, **kwargs)
    if r.status_code == 401:
        with _session_lock:
            if _cst == hdrs["CST"]:
                _login()
        r = requests.request(method, f"{_BASE}{path}",
                             headers=_auth_headers(), timeout=_TIMEOUT, **kwargs)
    r.raise_for_status()
    return r


def fetch_candles(epic: str, resolution: str, max_count: int = 200) -> list[Candle]:
    try:
        r = _api_request("GET", f"/prices/{epic}",
                         params={"resolution": resolution, "max": max_count})
        out = []
        for p in r.json().get("prices", []):
            def mid(s): return (s["bid"] + s["ask"]) / 2
            out.append(Candle(
                ts=p["snapshotTime"],
                open=mid(p["openPrice"]),
                high=mid(p["highPrice"]),
                low=mid(p["lowPrice"]),
                close=mid(p["closePrice"]),
                volume=float(p.get("lastTradedVolume", 0)),
            ))
        return out
    except Exception as exc:
        logger.error("Candle fetch (%s %s): %s", epic, resolution, exc)
        return []


def _keepalive() -> None:
    while True:
        time.sleep(8 * 60)
        try:
            _api_request("GET", "/ping")
        except Exception as exc:
            logger.warning("Keepalive failed: %s", exc)


# ── Indicator helpers ─────────────────────────────────────────────────────────

def _ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return [math.nan] * len(values)
    k = 2.0 / (period + 1)
    result: list[float] = [math.nan] * (period - 1)
    result.append(sum(values[:period]) / period)
    for v in values[period:]:
        result.append(result[-1] + k * (v - result[-1]))
    return result


def _atr(candles: list[Candle], period: int = 14) -> list[float]:
    trs = []
    for i, c in enumerate(candles):
        if i == 0:
            trs.append(c.high - c.low)
        else:
            p = candles[i - 1]
            trs.append(max(c.high - c.low,
                           abs(c.high - p.close),
                           abs(c.low  - p.close)))
    if len(trs) < period:
        return [math.nan] * len(trs)
    result: list[float] = [math.nan] * (period - 1)
    result.append(sum(trs[:period]) / period)
    for tr in trs[period:]:
        result.append((result[-1] * (period - 1) + tr) / period)
    return result


def _rsi(values: list[float], period: int = 14) -> list[float]:
    if len(values) < period + 1:
        return [math.nan] * len(values)
    result: list[float] = [math.nan] * period
    gains, losses = [], []
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains) / period
    avg_l = sum(losses) / period
    result.append(100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l))
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0.0))  / period
        avg_l = (avg_l * (period - 1) + max(-d, 0.0)) / period
        result.append(100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l))
    return result


def _macd(values: list[float]) -> tuple[float, float, float]:
    """Return (macd_line, signal_line, histogram) scalars from the last bar."""
    ema12 = _ema(values, 12)
    ema26 = _ema(values, 26)
    ml = [
        (a - b) if not (math.isnan(a) or math.isnan(b)) else math.nan
        for a, b in zip(ema12, ema26)
    ]
    valid_ml = [x for x in ml if not math.isnan(x)]
    if len(valid_ml) < 9:
        return math.nan, math.nan, math.nan
    sig_vals = _ema(valid_ml, 9)
    sig      = sig_vals[-1]
    macd_v   = ml[-1]
    return macd_v, sig, macd_v - sig


# ── Swing detection ───────────────────────────────────────────────────────────

def _swing_highs(candles: list[Candle], window: int = 5) -> list[tuple[int, float]]:
    result = []
    n = len(candles)
    for i in range(window, n - window):
        h = candles[i].high
        if all(candles[j].high <= h for j in range(i - window, i + window + 1) if j != i):
            result.append((i, h))
    return result


def _swing_lows(candles: list[Candle], window: int = 5) -> list[tuple[int, float]]:
    result = []
    n = len(candles)
    for i in range(window, n - window):
        l = candles[i].low
        if all(candles[j].low >= l for j in range(i - window, i + window + 1) if j != i):
            result.append((i, l))
    return result


# ── Pattern result ─────────────────────────────────────────────────────────────

@dataclass
class PatternResult:
    name:        str
    base_pts:    int
    quality_pts: int
    direction:   str
    entry:       float
    sl_ref:      float
    zone_level:  float


# ── Pattern 1: Liquidity Sweep + BOS ─────────────────────────────────────────

def _detect_sweep_bos(
    candles: list[Candle], direction: str, atr: float, sweep_tol: float,
    lookback: int = 35,
) -> Optional[PatternResult]:
    if len(candles) < lookback + 5:
        return None
    recent = candles[-lookback:]
    tol = sweep_tol * atr

    if direction == "buy":
        lows  = _swing_lows(recent[:-3],  window=4)
        highs = _swing_highs(recent[:-3], window=4)
        if not lows or not highs:
            return None
        swing_low  = min(v for _, v in lows)
        swing_high = max(v for _, v in highs)

        # Find last sweep candle (wicks below swing_low+tol, closes above)
        sweep_idx     = None
        sweep_extreme = swing_low
        for i, c in enumerate(recent[:-3]):
            if c.low < swing_low + tol and c.close > swing_low:
                sweep_idx     = i
                sweep_extreme = c.low

        if sweep_idx is None:
            return None

        # BOS: close above swing_high within 3 candles after sweep
        bos_close = None
        for c in recent[sweep_idx + 1: sweep_idx + 4]:
            if c.close > swing_high:
                bos_close = c.close
                break
        if bos_close is None:
            return None

        # 50% retracement entry
        move     = bos_close - swing_high
        entry_50 = bos_close - 0.5 * move
        entry    = entry_50 if (entry_50 - swing_high) <= 1.5 * atr else swing_high

        # Wick quality
        qual = 0
        for c in recent:
            if abs(c.low - sweep_extreme) < 0.01 * atr:
                body = abs(c.close - c.open)
                wick = min(c.close, c.open) - c.low
                qual = 8 if (body > 0 and wick / body > 0.5) else 4
                break

        return PatternResult("Liquidity Sweep + BOS", 38, qual,
                             "buy", entry, sweep_extreme, swing_high)

    else:  # sell
        highs = _swing_highs(recent[:-3], window=4)
        lows  = _swing_lows(recent[:-3],  window=4)
        if not highs or not lows:
            return None
        swing_high = max(v for _, v in highs)
        swing_low  = min(v for _, v in lows)

        sweep_idx     = None
        sweep_extreme = swing_high
        for i, c in enumerate(recent[:-3]):
            if c.high > swing_high - tol and c.close < swing_high:
                sweep_idx     = i
                sweep_extreme = c.high

        if sweep_idx is None:
            return None

        bos_close = None
        for c in recent[sweep_idx + 1: sweep_idx + 4]:
            if c.close < swing_low:
                bos_close = c.close
                break
        if bos_close is None:
            return None

        move     = swing_low - bos_close
        entry_50 = bos_close + 0.5 * move
        entry    = entry_50 if (swing_low - entry_50) <= 1.5 * atr else swing_low

        qual = 0
        for c in recent:
            if abs(c.high - sweep_extreme) < 0.01 * atr:
                body = abs(c.close - c.open)
                wick = c.high - max(c.close, c.open)
                qual = 8 if (body > 0 and wick / body > 0.5) else 4
                break

        return PatternResult("Liquidity Sweep + BOS", 38, qual,
                             "sell", entry, sweep_extreme, swing_low)


# ── Pattern 2: Supply / Demand Zone ──────────────────────────────────────────

def _detect_supply_demand(
    candles: list[Candle], direction: str, atr: float, lookback: int = 40,
) -> Optional[PatternResult]:
    if len(candles) < lookback:
        return None
    recent      = candles[-lookback:]
    current_cl  = recent[-1].close

    for i in range(5, len(recent) - 5):
        c    = recent[i]
        body = abs(c.close - c.open)
        if body < 1.5 * atr:
            continue

        if direction == "buy" and c.close > c.open:
            z_bot = min(c.open, c.close) - 0.1 * atr
            z_top = max(c.open, c.close) + 0.1 * atr
            if z_bot <= current_cl <= z_top + atr:
                entry = (z_bot + z_top) / 2
                return PatternResult("Demand Zone", 37, 7,
                                     "buy", entry, z_bot - 0.2 * atr, z_bot)

        elif direction == "sell" and c.close < c.open:
            z_top = max(c.open, c.close) + 0.1 * atr
            z_bot = min(c.open, c.close) - 0.1 * atr
            if z_bot - atr <= current_cl <= z_top:
                entry = (z_bot + z_top) / 2
                return PatternResult("Supply Zone", 37, 7,
                                     "sell", entry, z_top + 0.2 * atr, z_top)

    return None


# ── Pattern 3: Double Top / Bottom ────────────────────────────────────────────

def _detect_double_formation(
    candles: list[Candle], direction: str, atr: float, lookback: int = 50,
) -> Optional[PatternResult]:
    if len(candles) < lookback:
        return None
    recent = candles[-lookback:]

    if direction == "buy":
        lows = _swing_lows(recent[:-5], window=5)
        if len(lows) < 2:
            return None
        (i1, l1), (i2, l2) = lows[-2], lows[-1]
        if i2 <= i1 or abs(l1 - l2) > 0.5 * atr:
            return None
        neckline = max(c.close for c in recent[i1:i2 + 1])
        if recent[-1].close > neckline:
            return PatternResult("Double Bottom", 37, 6,
                                 "buy", neckline, min(l1, l2), neckline)

    else:
        highs = _swing_highs(recent[:-5], window=5)
        if len(highs) < 2:
            return None
        (i1, h1), (i2, h2) = highs[-2], highs[-1]
        if i2 <= i1 or abs(h1 - h2) > 0.5 * atr:
            return None
        neckline = min(c.close for c in recent[i1:i2 + 1])
        if recent[-1].close < neckline:
            return PatternResult("Double Top", 37, 6,
                                 "sell", neckline, max(h1, h2), neckline)

    return None


# ── Pattern 4: Flag / Channel ──────────────────────────────────────────────────

def _detect_flag(
    candles: list[Candle], direction: str, atr: float, lookback: int = 40,
) -> Optional[PatternResult]:
    if len(candles) < lookback:
        return None
    recent = candles[-lookback:]

    if direction == "buy":
        for start in range(0, len(recent) - 18):
            impulse = recent[start:start + 6]
            if recent[start + 5].close - recent[start].low < 3 * atr:
                continue
            consol = recent[start + 6:start + 14]
            if len(consol) < 5:
                continue
            if (max(c.high for c in consol) - min(c.low for c in consol)) > 2 * atr:
                continue
            c_high = max(c.high for c in consol)
            post   = recent[start + 14:]
            if any(c.close > c_high for c in post[-3:]):
                return PatternResult("Bull Flag", 36, 5,
                                     "buy", c_high,
                                     min(c.low for c in consol), c_high)

    else:
        for start in range(0, len(recent) - 18):
            if recent[start].high - recent[start + 5].close < 3 * atr:
                continue
            consol = recent[start + 6:start + 14]
            if len(consol) < 5:
                continue
            if (max(c.high for c in consol) - min(c.low for c in consol)) > 2 * atr:
                continue
            c_low = min(c.low for c in consol)
            post  = recent[start + 14:]
            if any(c.close < c_low for c in post[-3:]):
                return PatternResult("Bear Flag", 36, 5,
                                     "sell", c_low,
                                     max(c.high for c in consol), c_low)

    return None


# ── Scoring factors ───────────────────────────────────────────────────────────

def _score_technical(h1: list[Candle], direction: str) -> int:
    """RSI14 + MACD + EMA20 vs price: 2+ aligned → +10, 1 → +4, 0 → 0."""
    if len(h1) < 35:
        return 0
    closes = [c.close for c in h1]
    rsi_v  = _rsi(closes, 14)[-1]
    mv, sv, _ = _macd(closes)
    ema20  = _ema(closes, 20)[-1]
    cur    = closes[-1]

    count = 0
    if not math.isnan(rsi_v):
        if direction == "buy"  and rsi_v < 60: count += 1
        if direction == "sell" and rsi_v > 40: count += 1
    if not (math.isnan(mv) or math.isnan(sv)):
        if direction == "buy"  and mv > sv: count += 1
        if direction == "sell" and mv < sv: count += 1
    if not math.isnan(ema20):
        if direction == "buy"  and cur > ema20: count += 1
        if direction == "sell" and cur < ema20: count += 1

    return 10 if count >= 2 else (4 if count == 1 else 0)


def _score_daily_bias(daily: list[Candle], direction: str) -> int:
    """EMA50 vs EMA200 on daily: aligned +15, neutral +5, against -8."""
    if len(daily) < 52:
        return 5
    closes = [c.close for c in daily]
    e50    = _ema(closes, 50)[-1]
    e200   = _ema(closes, 200)[-1] if len(daily) >= 202 else math.nan
    cur    = closes[-1]

    if math.isnan(e50):
        return 5

    bull = (not math.isnan(e200) and e50 > e200 and cur > e50) or \
           (math.isnan(e200) and cur > e50)
    bear = (not math.isnan(e200) and e50 < e200 and cur < e50) or \
           (math.isnan(e200) and cur < e50)

    if   direction == "buy"  and bull: return 15
    elif direction == "buy"  and bear: return -8
    elif direction == "sell" and bear: return 15
    elif direction == "sell" and bull: return -8
    return 5


def _score_ma20(h1: list[Candle], direction: str) -> int:
    """Price vs MA20 on H1: aligned +4, against -3, neither 0."""
    if len(h1) < 22:
        return 0
    closes = [c.close for c in h1]
    ma20   = _ema(closes, 20)[-1]
    cur    = closes[-1]
    if math.isnan(ma20):
        return 0
    if direction == "buy":
        return 4 if cur > ma20 else -3
    return 4 if cur < ma20 else -3


def _score_session(now_utc: datetime, epic: str) -> int:
    """Trading session quality bonus."""
    t = now_utc.hour * 60 + now_utc.minute  # minutes since midnight UTC
    if epic == "BTC":
        if 13 * 60 <= t < 20 * 60: return 6   # US hours
        if  7 * 60 <= t < 12 * 60: return 3   # EU
        if  0 * 60 <= t <  6 * 60: return 2   # Asia
        return 1
    else:  # US indices
        if 13 * 60 <= t < 20 * 60: return 6   # US session
        if  8 * 60 <= t <  9 * 60: return 5   # London open
        if  7 * 60 <= t < 12 * 60: return 4   # EU session
        if  0 * 60 <= t <  6 * 60: return 2   # Asia
        return 1


def _score_additional(h1: list[Candle], atr: float, pattern: PatternResult) -> int:
    """Round number (+5), above-avg volume (+3), choppy/tight range (-10)."""
    pts   = 0
    entry = pattern.entry

    # Round number proximity
    if entry > 0:
        mag   = 10 ** math.floor(math.log10(entry))
        round_lvl = round(entry / mag) * mag
        if abs(entry - round_lvl) < 0.2 * atr:
            pts += 5

    if len(h1) >= 20:
        # Above-average volume (optional bonus — tick vol on CFDs is unreliable)
        vols = [c.volume for c in h1[-20:] if c.volume > 0]
        if vols:
            avg_vol = sum(vols) / len(vols)
            if h1[-1].volume > 1.2 * avg_vol:
                pts += 3

        # Choppy market: range over last 20 bars < 3×ATR
        rng = max(c.high for c in h1[-20:]) - min(c.low for c in h1[-20:])
        if atr > 0 and rng / atr < 3:
            pts -= 10

    return pts


# ── Pattern scan + best pick ──────────────────────────────────────────────────

def _ema_direction(h1: list[Candle]) -> Optional[str]:
    if len(h1) < 52:
        return None
    closes = [c.close for c in h1]
    e20 = _ema(closes, 20)[-1]
    e50 = _ema(closes, 50)[-1]
    if math.isnan(e20) or math.isnan(e50) or e20 == e50:
        return None
    return "buy" if e20 > e50 else "sell"


def detect_best_pattern(
    h1: list[Candle], atr: float, instr: Instrument,
) -> Optional[PatternResult]:
    direction = _ema_direction(h1)
    if direction is None:
        return None

    closed = h1[:-1]  # exclude potentially open current candle
    candidates: list[PatternResult] = []

    for fn in [
        lambda d: _detect_sweep_bos(closed, d, atr, instr.sweep_tol),
        lambda d: _detect_supply_demand(closed, d, atr),
        lambda d: _detect_double_formation(closed, d, atr),
        lambda d: _detect_flag(closed, d, atr),
    ]:
        try:
            p = fn(direction)
            if p is not None:
                candidates.append(p)
        except Exception as exc:
            logger.debug("Pattern detector error: %s", exc)

    if not candidates:
        return None
    return max(candidates, key=lambda p: p.base_pts)


# ── Full scoring ──────────────────────────────────────────────────────────────

def total_score(
    pattern: PatternResult,
    h1: list[Candle],
    daily: list[Candle],
    atr: float,
    now_utc: datetime,
    epic: str,
) -> int:
    return (
        pattern.base_pts
        + pattern.quality_pts
        + _score_technical(h1, pattern.direction)
        + _score_daily_bias(daily, pattern.direction)
        + _score_ma20(h1, pattern.direction)
        + _score_session(now_utc, epic)
        + _score_additional(h1, atr, pattern)
    )


# ── Trade plan ────────────────────────────────────────────────────────────────

def calc_trade_plan(pattern: PatternResult, atr: float, instr: Instrument) -> dict:
    entry    = pattern.entry
    raw_dist = instr.sl_atr_mult * atr
    max_dist = instr.max_sl_atr  * atr

    if pattern.direction == "buy":
        sl      = pattern.sl_ref - raw_dist
        sl_dist = min(entry - sl, max_dist)
        sl      = entry - sl_dist
        tp1     = entry + 1.5 * sl_dist
        tp2     = entry + 2.5 * sl_dist
    else:
        sl      = pattern.sl_ref + raw_dist
        sl_dist = min(sl - entry, max_dist)
        sl      = entry + sl_dist
        tp1     = entry - 1.5 * sl_dist
        tp2     = entry - 2.5 * sl_dist

    rr = 1.5  # by construction
    return {"sl": sl, "tp1": tp1, "tp2": tp2, "rr1": rr, "rr2": 2.5}


# ── Market helpers ────────────────────────────────────────────────────────────

def _is_tradeable_us(now_utc: datetime) -> bool:
    """Near-24h US index schedule with daily maintenance break."""
    from datetime import time as dtime
    from zoneinfo import ZoneInfo
    et = now_utc.astimezone(ZoneInfo("America/New_York"))
    wd = et.weekday()
    t  = et.time()
    if wd == 5:
        return False
    if wd == 6 and t < dtime(18, 0):
        return False
    if wd == 4 and t >= dtime(16, 30):
        return False
    if dtime(16, 30) <= t < dtime(18, 0):
        return False
    return True


def _in_news_blackout(now_utc: datetime, epic: str) -> bool:
    if epic not in US_INDEX_EPICS:
        return False
    t = now_utc.hour * 60 + now_utc.minute
    # Pre-NFP / FOMC windows (12:25-13:05 UTC and 13:25-14:05 UTC)
    return (12 * 60 + 25 <= t < 13 * 60 + 5) or (13 * 60 + 25 <= t < 14 * 60 + 5)


# ── State persistence ─────────────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        with open(COOLDOWN_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    try:
        with open(COOLDOWN_FILE, "w") as f:
            json.dump(state, f)
    except OSError as exc:
        logger.warning("Could not save state: %s", exc)


# ── Telegram ──────────────────────────────────────────────────────────────────

def _strip(s: str) -> str:
    for tag in ("<b>", "</b>", "<i>", "</i>"):
        s = s.replace(tag, "")
    return s


def send_telegram(html: str, plain: str = "") -> None:
    if not BOT_TOKEN or not CHAT_ID:
        logger.info("Telegram not configured — alert:\n%s", plain or html)
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": html, "parse_mode": "HTML"},
            timeout=10,
        )
        if r.status_code != 200:
            logger.warning("Telegram HTTP %d: %s", r.status_code, r.text[:200])
    except Exception as exc:
        logger.error("Telegram send failed: %s", exc)


def _daily_trend_label(daily: list[Candle], direction: str) -> str:
    if len(daily) < 52:
        return "Unknown"
    closes = [c.close for c in daily]
    e50    = _ema(closes, 50)[-1]
    e200   = _ema(closes, 200)[-1] if len(daily) >= 202 else math.nan
    cur    = closes[-1]
    if math.isnan(e50):
        return "Unknown"
    if not math.isnan(e200):
        bull_bias = e50 > e200
    else:
        bull_bias = cur > e50

    if bull_bias:
        return "Uptrend " + ("✅" if direction == "buy" else "⚠️")
    return "Downtrend " + ("✅" if direction == "sell" else "⚠️")


def _build_alert(
    instr: Instrument,
    pattern: PatternResult,
    score: int,
    plan: dict,
    daily_trend: str,
    now_utc: datetime,
    is_aplus: bool,
    correlated_with: list[str] | None = None,
) -> tuple[str, str]:
    sep   = "━━━━━━━━━━━━━━━━━━━━━━"
    t_str = now_utc.strftime("%H:%M UTC")
    d     = pattern.direction
    emoji = "🟢" if is_aplus else "⚡"
    grade = "A+" if is_aplus else "WATCH"
    dl    = "BUY" if d == "buy" else "SELL"
    arrow = "📈" if d == "buy" else "📉"
    entry_note = "limit — 50% retracement" if "Sweep" in pattern.name or "Zone" in pattern.name \
                 else "limit — zone level"

    lines = [
        f"{emoji} <b>{grade} — {instr.name}</b>   {arrow}",
        sep,
        f"Pattern  : {pattern.name}",
        f"Score    : {score}",
        f"Direction: <b>{dl}</b>",
        sep,
        f"Entry    : <b>{pattern.entry:,.2f}</b>  ({entry_note})",
        f"SL       : <b>{plan['sl']:,.2f}</b>",
        f"TP1      : <b>{plan['tp1']:,.2f}</b>  (R:R {plan['rr1']:.1f})",
        f"TP2      : <b>{plan['tp2']:,.2f}</b>  (R:R {plan['rr2']:.1f})",
        f"Daily    : {daily_trend}",
        sep,
        f"🕐 {t_str}",
        "<i>Alert only — always confirm before trading.</i>",
    ]
    if correlated_with:
        others = " / ".join(correlated_with)
        lines.append(
            f"<i>⚠️ Also firing on {others} — one exposure, not independent trades.</i>"
        )

    html  = "\n".join(lines)
    plain = "\n".join(_strip(l) for l in lines)
    return html, plain


# ── Scan ──────────────────────────────────────────────────────────────────────

def scan_once(state: dict) -> dict:
    now_utc = datetime.now(timezone.utc)

    # Daily signal count
    today       = now_utc.strftime("%Y-%m-%d")
    daily_state = state.get("daily", {})
    if daily_state.get("date") != today:
        daily_state = {"date": today, "count": 0}

    # Adaptive threshold
    threshold_a = state.get("threshold_a", THRESHOLD_A_PLUS)

    pending: dict[str, dict] = {}

    for instr in INSTRUMENTS:
        epic = instr.epic

        # Cooldown
        if time.time() - state.get(f"cooldown_{epic}", 0) < ALERT_COOLDOWN_S:
            logger.debug("%s: on cooldown", epic)
            continue

        # Market hours (US indices only)
        if epic in US_INDEX_EPICS and not _is_tradeable_us(now_utc):
            logger.debug("%s: outside market hours", epic)
            continue

        # News blackout
        if _in_news_blackout(now_utc, epic):
            logger.info("%s: news blackout — skip", epic)
            continue

        # Daily A+ cap
        if daily_state.get("count", 0) >= MAX_DAILY_SIGNALS:
            logger.info("Daily A+ cap (%d) reached", MAX_DAILY_SIGNALS)
            break

        # Fetch candles
        h1    = fetch_candles(epic, "HOUR", 200)
        daily = fetch_candles(epic, "DAY",  200)

        if len(h1) < 60:
            logger.debug("%s: not enough H1 bars (%d)", epic, len(h1))
            continue

        # ATR (closed candles)
        atr_list = _atr(h1[:-1], 14)
        atr = atr_list[-1] if atr_list and not math.isnan(atr_list[-1]) else 0.0
        if atr == 0:
            continue

        # Pattern detection
        pattern = detect_best_pattern(h1, atr, instr)
        if pattern is None:
            logger.debug("%s: no pattern", epic)
            continue

        # Score
        score = total_score(pattern, h1, daily, atr, now_utc, epic)
        logger.info("%s | %s | dir=%s | score=%d", epic, pattern.name, pattern.direction, score)

        if score < THRESHOLD_WATCH:
            continue

        daily_trend = _daily_trend_label(daily, pattern.direction)
        plan        = calc_trade_plan(pattern, atr, instr)

        pending[epic] = {
            "instr":       instr,
            "pattern":     pattern,
            "score":       score,
            "plan":        plan,
            "daily_trend": daily_trend,
            "is_aplus":    score >= threshold_a,
        }

    # US index correlation filter: keep only the highest-scoring one
    us_pending = {e: v for e, v in pending.items() if e in US_INDEX_EPICS}
    if len(us_pending) >= 2:
        directions = [v["pattern"].direction for v in us_pending.values()]
        if directions.count("buy") != directions.count("sell"):
            consensus = "buy" if directions.count("buy") > directions.count("sell") else "sell"
            for epic in list(pending.keys()):
                if epic in US_INDEX_EPICS and pending[epic]["pattern"].direction != consensus:
                    logger.info("%s: suppressed (contradicts consensus)", epic)
                    del pending[epic]

        us_after = {e: v for e, v in pending.items() if e in US_INDEX_EPICS}
        if len(us_after) > 1:
            strongest = max(us_after, key=lambda e: us_after[e]["score"])
            for epic in list(pending.keys()):
                if epic in US_INDEX_EPICS and epic != strongest:
                    logger.info("%s: suppressed (score=%d < %s score=%d)",
                                epic, pending[epic]["score"], strongest, us_after[strongest]["score"])
                    del pending[epic]

    # Send alerts
    for epic, data in pending.items():
        instr      = data["instr"]
        pattern    = data["pattern"]
        score      = data["score"]
        plan       = data["plan"]
        is_aplus   = data["is_aplus"]

        corr = None
        if epic in US_INDEX_EPICS:
            others = [e for e in pending if e in US_INDEX_EPICS and e != epic]
            if others:
                corr = sorted(others)

        html, plain = _build_alert(
            instr, pattern, score, plan, data["daily_trend"],
            now_utc, is_aplus, correlated_with=corr,
        )
        send_telegram(html, plain)
        logger.info("Alert sent: %s %s score=%d a+=%s", epic, pattern.direction, score, is_aplus)

        state[f"cooldown_{epic}"] = time.time()

        if is_aplus:
            daily_state["count"] = daily_state.get("count", 0) + 1
            state["last_signal_date"]  = today
            state["days_without"]      = 0
            state["threshold_a"]       = THRESHOLD_A_PLUS

    # Adaptive threshold: lower after 3+ signal-free days (min 65)
    last_sig = state.get("last_signal_date", "")
    if last_sig and last_sig != today and state.get("days_without_checked") != today:
        state["days_without"]       = state.get("days_without", 0) + 1
        state["days_without_checked"] = today
        if state["days_without"] >= 3:
            state["threshold_a"] = max(65, state.get("threshold_a", THRESHOLD_A_PLUS) - 2)
            logger.info("Adaptive threshold → %d (days_without=%d)",
                        state["threshold_a"], state["days_without"])

    state["daily"] = daily_state
    return state


# ── Entry point ───────────────────────────────────────────────────────────────

_running = True


def _handle_shutdown(sig, frame):
    global _running
    logger.info("Shutdown signal — stopping")
    _running = False


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT,  _handle_shutdown)

    if not (CAP_API_KEY and CAP_ID and CAP_PASSWORD):
        logger.error(
            "Missing Capital.com credentials. "
            "Set CAPITAL_API_KEY, CAPITAL_IDENTIFIER, CAPITAL_PASSWORD."
        )
        send_telegram(
            "🔴 <b>Alert bot stopped</b> — missing Capital.com credentials.",
            "Alert bot stopped — missing Capital.com credentials.",
        )
        sys.exit(1)

    _login()
    threading.Thread(target=_keepalive, daemon=True, name="capital-keepalive").start()

    state = _load_state()

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    send_telegram(
        f"🟡 <b>Alert bot started</b> — <i>{now_str}</i>\n"
        "Watching S&amp;P 500, Nasdaq 100, Dow Jones, Bitcoin.\n"
        "Strategy: 5-Pattern SMC Scoring  |  Scanning every 5 min.",
        f"Alert bot started {now_str}. Watching US500, US100, US30, BTC.",
    )
    logger.info("Bot started — US500, US100, US30, BTC")

    max_runtime = int(os.getenv("MAX_RUNTIME_S", "0"))
    start_time  = time.time()

    while _running:
        try:
            state = scan_once(state)
            _save_state(state)
        except Exception as exc:
            logger.error("Scan error: %s", exc, exc_info=True)

        if max_runtime and (time.time() - start_time) >= max_runtime:
            logger.info("Max runtime reached — exiting.")
            break

        if _running:
            logger.debug("Sleeping %ds until next scan", SCAN_INTERVAL_S)
            time.sleep(SCAN_INTERVAL_S)

    logger.info("Bot stopped.")


if __name__ == "__main__":
    main()
