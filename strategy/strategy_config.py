"""
strategy_config.py — every tunable number from the strategy document lives here.

Edit values here, not in the engine. All session windows are in UTC.
Values marked CALIBRATE are starting points and must be validated on real data
before they are trusted (especially everything for BTCUSD).
"""
from __future__ import annotations

# ── Decision thresholds ───────────────────────────────────────────────────────
# v3.2: raised by ~half the new order-flow bonus range (+8 max) so the anchored
# VWAP / volume profile factors differentiate setups instead of inflating everything.
WATCH_MIN      = 72      # 72–81  -> WATCH
A_PLUS_BASE    = 82      # >=82   -> A+   (this one adapts)
A_PLUS_FLOOR   = 68      # adaptive threshold never drops below this
A_PLUS_CEIL    = 90      # adaptive threshold never rises above this

# ── Adaptive threshold ────────────────────────────────────────────────────────
ADAPT_NO_SIGNAL_DAYS = 3   # after N consecutive no-signal days, lower threshold
ADAPT_STEP_DOWN      = 2   # 75 -> 73 -> 71 ...
ADAPT_STEP_UP        = 1   # raise by 1 on high-signal days (v3)

# ── Daily caps ────────────────────────────────────────────────────────────────
MAX_A_PLUS_PER_DAY = 5
MAX_WATCH_PER_DAY  = 10     # v3.1: loosened caps — strategy quality filters are the real gate

# ── Pattern base scores + max bonus (Factor 1) ───────────────────────────────
# type: "breakout"  -> entry = 50% retrace limit
#       "rejection" -> entry = structural level (limit order at key level)
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
# Continuation patterns (flag, sd_rejection, news_retest) are hard-blocked
# counter-trend (explicit `return None` in the engine — BIAS_COUNTER is unused
# for them beyond documentation). Reversal patterns (sweep_bos, reversal) use
# BIAS_COUNTER_REVERSAL: only the strongest counter-trend reversals clear
# WATCH_MIN, signalling caution.
BIAS_ALIGNED          = 15
BIAS_NEUTRAL          = 5
BIAS_COUNTER          = -12   # continuation patterns — hard block
BIAS_COUNTER_REVERSAL = -8    # reversal patterns — allowed with penalty
EMA_FAST_BIAS = 50
EMA_SLOW_BIAS = 200

# Patterns allowed to fire counter-trend (with BIAS_COUNTER_REVERSAL penalty).
# Continuation patterns not in this set are hard-blocked when counter-trend.
COUNTER_TREND_PATTERNS = frozenset({"sweep_bos", "reversal"})

# ── Additional factors ────────────────────────────────────────────────────────
ROUND_NUMBER_BONUS   = 5
VOLUME_CONFIRM_BONUS = 3     # optional — CFD tick-volume is unreliable
CHOPPY_PENALTY       = -10   # applied when ADX < ADX_CHOPPY_THRESHOLD

# ── Market condition guards ───────────────────────────────────────────────────
# Choppy: ADX below this threshold → penalise score (v3: ADX < 18)
ADX_CHOPPY_THRESHOLD = 20    # v3.1: raised from 18 — stricter choppy-market filter
ADX_PERIOD           = 14
# Volatile regime: ATR/price > per-instrument volatile_atr_pct → skip setup entirely (v3: 1.8%)
# BTC uses a higher limit because crypto is structurally more volatile.

# ── Indicator parameters ──────────────────────────────────────────────────────
RSI_PERIOD  = 14
ATR_PERIOD  = 14
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9   # kept — macd() still available in indicators.py
VWAP_PERIOD = 24   # 24 H1 bars ≈ 1 full trading day; rolling approximation replaces MACD in Factor 2

# ── Momentum bonus (v3.1 — disabled pending calibration) ─────────────────────
MOMENTUM_BONUS         = 3
MOMENTUM_BONUS_ENABLED = False

# ── Order-flow proxies (v3.2) ─────────────────────────────────────────────────
# Anchored VWAP: anchored at the most recent opposing swing extreme (swing low
# for buys, swing high for sells). Price on the correct side means the average
# participant since that swing is in profit in the trade's direction.
AVWAP_BONUS = 4

# Volume profile: POC (point of control) + 70% value area over ~1 trading week
# of H1 bars. NOTE — Capital.com volume is TICK COUNT, not true traded volume,
# so this is an approximation of where activity concentrated. Kept as a modest
# bonus, never a hard filter.
VP_POC_BONUS  = 4     # price on the correct side of POC
VP_LOOKBACK   = 120   # H1 bars ≈ 1 trading week
VP_BINS       = 24
VP_VALUE_AREA = 0.70  # fraction of volume inside the value area
SWING_LEFT, SWING_RIGHT = 3, 3
SIGNAL_LOOKBACK = 80   # 80 H1 bars ≈ 3-4 trading days of context

# ── Setup expiry (H1 trades: 8 hours) ────────────────────────────────────────
SETUP_EXPIRY_MIN = 480

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

# ── Entry mode ────────────────────────────────────────────────────────────────
# "bos_close"     -> enter immediately at the confirmation candle's close
#                    (market order, no waiting). Applies to ALL five patterns on
#                    that instrument, not just sweep_bos — every detector emits
#                    a confirm_price. Fixes setups expiring unfilled, at the cost
#                    of a worse average entry price.
# "retrace_limit" -> original behaviour: breakout patterns wait for a 50%
#                    retrace, rejection patterns place a limit at the structural
#                    level. Better fills, but a quarter of setups never fill.
#
# NOTE: with "bos_close" the entry sits further from the structural stop anchor,
# so `dist` grows and the atr_max clip fires much more often. When it does, the
# stop is placed by this config rather than by market structure — watch the
# `sl_clip` field in the journal ("max" means the structural stop was cut short).
ENTRY_MODES = ("bos_close", "retrace_limit")

# ── Per-instrument configuration ──────────────────────────────────────────────
# volatile_atr_pct: ATR/price above this → setup skipped entirely (v3 = 1.8% for indices)
# BTC threshold is higher because crypto ATR naturally exceeds 1.8% of price.
# ATR min/max are in ATR multiples (H1 timeframe).
# H1 ATR reference: US500 ≈ 25-45 pts, US100 ≈ 80-150 pts, US30 ≈ 180-350 pts.
# atr_min=2.0 → US500 min SL ≈ 50-90 pts, US100 ≈ 160-300 pts — in line with user's
# $50-75 target for US500 and proportionally larger for faster-moving indices.
#
# entry_mode: US indices use "bos_close" (they move too fast for a retrace to be
# reachable in practice). BTCUSD stays on "retrace_limit" as a control group, so
# the two modes can be compared on live data rather than assumed.
INSTRUMENTS = {
    "US500":  {"name": "S&P 500",    "asset": "index",  "session": "index_sp_dow",
               "atr_max": 4.0, "atr_min": 2.0, "round_step": 50,    "round_prox": 0.0012,
               "volatile_atr_pct": 0.018, "entry_mode": "bos_close",
               "correlated_group": "us_indices", "always_open": False},
    "US30":   {"name": "Dow Jones",  "asset": "index",  "session": "index_sp_dow",
               "atr_max": 4.0, "atr_min": 2.0, "round_step": 250,   "round_prox": 0.0012,
               "volatile_atr_pct": 0.018, "entry_mode": "bos_close",
               "correlated_group": "us_indices", "always_open": False},
    "US100":  {"name": "Nasdaq 100", "asset": "index",  "session": "index_nasdaq",
               "atr_max": 4.0, "atr_min": 2.0, "round_step": 100,   "round_prox": 0.0012,
               "volatile_atr_pct": 0.018, "entry_mode": "bos_close",
               "correlated_group": "us_indices", "always_open": False},
    "BTCUSD": {"name": "Bitcoin",    "asset": "crypto", "session": "btc",
               "atr_max": 3.5, "atr_min": 2.0, "round_step": 1000,  "round_prox": 0.0015,
               "volatile_atr_pct": 0.05,          # CALIBRATE — crypto is structurally more volatile
               "entry_mode": "retrace_limit",     # control group for the A/B
               "correlated_group": None, "always_open": True},
}

MIN_RR = 2.0

# ── Stop-loss placement ───────────────────────────────────────────────────────
# Buffer below (buy) / above (sell) the structural anchor (swing low/high + key level).
# 1.0 ATR gives adequate breathing room beyond the structural level. On M15 S&P 500
# (ATR ≈ 10 pts) this is ~10 pts of clearance — a single candle range — so normal
# retests and wicks don't reach the stop before the trade develops.
SL_BUFFER_ATR = 1.0
