"""
strategy_config.py — every tunable number from the strategy document lives here.

Edit values here, not in the engine. All session windows are in UTC.
Values marked CALIBRATE are starting points and must be validated on real data
before they are trusted (especially everything for BTCUSD).
"""
from __future__ import annotations

# ── Decision thresholds (Section IV) ─────────────────────────────────────────
WATCH_MIN      = 62      # 62–74  -> WATCH
A_PLUS_BASE    = 75      # >=75   -> A+   (this one adapts)
A_PLUS_FLOOR   = 65      # adaptive threshold never drops below this
A_PLUS_CEIL    = 85      # adaptive threshold never rises above this

# ── Adaptive threshold (Section V) ────────────────────────────────────────
ADAPT_NO_SIGNAL_DAYS = 3   # after N consecutive no-signal days, lower threshold
ADAPT_STEP_DOWN      = 2   # 75 -> 73 -> 71 ...
ADAPT_STEP_UP        = 5   # raise toward ceiling on high-signal days

# ── Daily caps (Section V) ────────────────────────────────────────────
MAX_A_PLUS_PER_DAY = 4
MAX_WATCH_PER_DAY  = None   # None = no cap

# ── Pattern base scores + max bonus (Factor 1) ───────────────────────────────
# type: "breakout"  -> entry = 50% retrace limit
#       "rejection" -> entry = confirmation-candle close
PATTERNS = {
    "sweep_bos":    {"base": 38, "max_bonus": 10, "type": "breakout",  "label": "Liquidity Sweep + BOS"},
    "sd_rejection": {"base": 37, "max_bonus": 8,  "type": "rejection", "label": "Supply/Demand Rejection"},
    "reversal":     {"base": 37, "max_bonus": 10, "type": "rejection", "label": "Double Top/Bottom / H&S"},
    "flag":         {"base": 36, "max_bonus": 8,  "type": "breakout",  "label": "Bull/Bear Flag"},
    "news_retest":  {"base": 34, "max_bonus": 8,  "type": "rejection", "label": "Post-News Retest"},
}

# ── Factor 2 — technical confirmation (RSI + MACD + EMA) ─────────────────────
CONF_2_OR_3 = 10
CONF_1      = 4
CONF_0      = 0

# ── Factor 3 — daily bias (EMA50/200) ───────────────────────────────────────
BIAS_ALIGNED = 15
BIAS_NEUTRAL = 5
BIAS_COUNTER = -8

# ── Factor 4 — MA20 on the entry timeframe ─────────────────────────────────
MA20_ALIGNED = 4
MA20_NEUTRAL = 0
MA20_COUNTER = -3
MA20_NEUTRAL_BAND = 0.0008   # |price-ma20|/price below this => neutral

# ── Additional factors ──────────────────────────────────────────────────
ROUND_NUMBER_BONUS   = 5
VOLUME_CONFIRM_BONUS = 3     # optional — CFD tick-volume is unreliable
HIGH_ATR_PENALTY     = -10
CHOPPY_PENALTY       = -10
HIGH_ATR_PCTILE      = 0.90  # ATR above this percentile of recent = "too volatile"

# ── Indicator parameters ───────────────────────────────────────────────
EMA_FAST_BIAS = 50
EMA_SLOW_BIAS = 200
MA20_PERIOD   = 20
RSI_PERIOD    = 14
ATR_PERIOD    = 14
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
SWING_LEFT, SWING_RIGHT = 3, 3
SIGNAL_LOOKBACK = 40

# ── Session windows (Factor 5) — UTC hours ────────────────────────────────
SESSION_TABLES = {
    "index_sp_dow": [
        ((12, 30), (16, 0), 10),   # New York
        ((7, 0),   (12, 30), 4),   # London
        ((0, 0),   (7, 0),   2),   # Asia
    ],
    "index_nasdaq": [
        ((12, 30), (16, 0), 10),
        ((7, 0),   (12, 30), 3),
        ((0, 0),   (7, 0),   2),
    ],
}
# BTC handled separately in market_sessions.py
BTC_US_OVERLAP_BONUS = 6
BTC_EUROPE_BONUS     = 3
BTC_ASIA_BONUS       = 2
BTC_WEEKEND_PENALTY  = -4

# ── Per-instrument configuration ──────────────────────────────────────────
INSTRUMENTS = {
    "US500":  {"name": "S&P 500",    "asset": "index",  "session": "index_sp_dow",
               "atr_max": 1.8, "atr_min": 0.50, "round_step": 50,    "round_prox": 0.0012,
               "correlated_group": "us_indices", "always_open": False},
    "US30":   {"name": "Dow Jones",  "asset": "index",  "session": "index_sp_dow",
               "atr_max": 1.8, "atr_min": 0.50, "round_step": 250,   "round_prox": 0.0012,
               "correlated_group": "us_indices", "always_open": False},
    "US100":  {"name": "Nasdaq 100", "asset": "index",  "session": "index_nasdaq",
               "atr_max": 2.0, "atr_min": 0.55, "round_step": 100,   "round_prox": 0.0012,
               "correlated_group": "us_indices", "always_open": False},
    "BTCUSD": {"name": "Bitcoin",    "asset": "crypto", "session": "btc",
               "atr_max": 2.2, "atr_min": 0.60, "round_step": 1000,  "round_prox": 0.0015,
               "correlated_group": None, "always_open": True},
}

MIN_RR = 2.0
SETUP_EXPIRY_MIN = 90
