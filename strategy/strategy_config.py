"""
strategy_config.py — every tunable number from the strategy document lives here.

Edit values here, not in the engine. All session windows are in UTC.
Values marked CALIBRATE are starting points and must be validated on real data
before they are trusted (especially everything for BTCUSD).
"""
from __future__ import annotations

# ── Decision thresholds ───────────────────────────────────────────────────────
WATCH_MIN      = 62      # 62–74  -> WATCH
A_PLUS_BASE    = 75      # >=75   -> A+   (this one adapts)
A_PLUS_FLOOR   = 65      # adaptive threshold never drops below this
A_PLUS_CEIL    = 85      # adaptive threshold never rises above this

# ── Adaptive threshold ────────────────────────────────────────────────────────
ADAPT_NO_SIGNAL_DAYS = 3   # after N consecutive no-signal days, lower threshold
ADAPT_STEP_DOWN      = 2   # 75 -> 73 -> 71 ...
ADAPT_STEP_UP        = 1   # raise by 1 on high-signal days (v3)

# ── Daily caps ────────────────────────────────────────────────────────────────
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

# ── Factor 2 — technical confirmation (RSI + MACD + EMA20) ───────────────────
# v3: needs ≥2 of 3 aligned for full bonus
CONF_2_OR_3 = 10
CONF_1      = 4
CONF_0      = 0
EMA_CONFIRM = 20    # EMA period for Factor 2 confirmation (v3: EMA20)

# ── Factor 3 — daily bias (EMA50/200) ────────────────────────────────────────
BIAS_ALIGNED  = 15
BIAS_NEUTRAL  = 5
BIAS_COUNTER  = -8
EMA_FAST_BIAS = 50
EMA_SLOW_BIAS = 200

# ── Additional factors ────────────────────────────────────────────────────────
ROUND_NUMBER_BONUS   = 5
VOLUME_CONFIRM_BONUS = 3     # optional — CFD tick-volume is unreliable
CHOPPY_PENALTY       = -10   # applied when ADX < ADX_CHOPPY_THRESHOLD

# ── Market condition guards ───────────────────────────────────────────────────
# Choppy: ADX below this threshold → penalise score (v3: ADX < 18)
ADX_CHOPPY_THRESHOLD = 18
ADX_PERIOD           = 14
# Volatile regime: ATR/price > per-instrument volatile_atr_pct → skip setup entirely (v3: 1.8%)
# BTC uses a higher limit because crypto is structurally more volatile.

# ── Indicator parameters ──────────────────────────────────────────────────────
RSI_PERIOD  = 14
ATR_PERIOD  = 14
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
SWING_LEFT, SWING_RIGHT = 3, 3
SIGNAL_LOOKBACK = 40

# ── Setup expiry (v3: ~3 hours) ───────────────────────────────────────────────
SETUP_EXPIRY_MIN = 180

# ── Session windows (Factor 4) — UTC hours ────────────────────────────────────
SESSION_TABLES = {
    "index_sp_dow": [
        ((12, 30), (16, 0), 10),   # New York  (+10 per v3)
        ((7, 0),   (12, 30), 4),   # London    (+4  per v3)
        ((0, 0),   (7, 0),   2),   # Asia
    ],
    "index_nasdaq": [
        ((12, 30), (16, 0), 10),   # New York  (+10 per v3)
        ((7, 0),   (12, 30), 3),   # London    (+3  per v3)
        ((0, 0),   (7, 0),   2),   # Asia
    ],
}
# BTC session handled separately in market_sessions.py
BTC_US_OVERLAP_BONUS = 6
BTC_EUROPE_BONUS     = 3
BTC_ASIA_BONUS       = 2
BTC_WEEKEND_PENALTY  = -4

# ── Per-instrument configuration ──────────────────────────────────────────────
# volatile_atr_pct: ATR/price above this → setup skipped entirely (v3 = 1.8% for indices)
# BTC threshold is higher because crypto ATR naturally exceeds 1.8% of price.
INSTRUMENTS = {
    "US500":  {"name": "S&P 500",    "asset": "index",  "session": "index_sp_dow",
               "atr_max": 1.8, "atr_min": 0.50, "round_step": 50,    "round_prox": 0.0012,
               "volatile_atr_pct": 0.018,
               "correlated_group": "us_indices", "always_open": False},
    "US30":   {"name": "Dow Jones",  "asset": "index",  "session": "index_sp_dow",
               "atr_max": 1.8, "atr_min": 0.50, "round_step": 250,   "round_prox": 0.0012,
               "volatile_atr_pct": 0.018,
               "correlated_group": "us_indices", "always_open": False},
    "US100":  {"name": "Nasdaq 100", "asset": "index",  "session": "index_nasdaq",
               "atr_max": 2.0, "atr_min": 0.55, "round_step": 100,   "round_prox": 0.0012,
               "volatile_atr_pct": 0.018,
               "correlated_group": "us_indices", "always_open": False},
    "BTCUSD": {"name": "Bitcoin",    "asset": "crypto", "session": "btc",
               "atr_max": 2.2, "atr_min": 0.60, "round_step": 1000,  "round_prox": 0.0015,
               "volatile_atr_pct": 0.05,          # CALIBRATE — crypto is structurally more volatile
               "correlated_group": None, "always_open": True},
}

MIN_RR = 2.0
