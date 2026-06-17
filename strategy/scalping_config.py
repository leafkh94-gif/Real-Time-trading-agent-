"""
Configuration for Plan B — Scalping Strategy (H1 bias / M15 entries).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ScalpingConfig:
    enabled: bool = True

    # Timing
    scan_interval_s:  int = 300    # 5 min
    alert_cooldown_s: int = 1800   # 30 min
    time_stop_s:      int = 7200   # 2h time stop if TP1 not hit

    # Gate 1: H1 trend (EMA slope)
    ema_fast: int = 20
    ema_slow: int = 50
    ema_slope_min_pct: float = 0.05

    # Gate 2: M15 volatility bands (ATR / Close %)
    atr_period:  int   = 14
    atr_pct_max: float = 0.7   # > 0.7% → 0 pts
    atr_pct_min: float = 0.4   # 0.4–0.7% → 1 pt  |  < 0.4% → 0.5 pt

    # Gate 3: M15 sweep
    swing_lookback:    int = 20   # bars to look back for swing H/L
    swing_confirm_bars: int = 3   # min distance of swing from current candle
    sweep_atr_tolerance: float = 0.2  # proximity fraction of ATR

    # Gate 5: news blackout
    news_blackout_minutes: int = 30

    # Gate 6: RSI + Stochastic thresholds
    rsi_period:         int = 14
    rsi_buy_threshold:  float = 45.0   # RSI > 45 for BUY (±5 flexibility)
    rsi_sell_threshold: float = 55.0   # RSI < 55 for SELL
    stoch_period:   int = 14
    stoch_smooth_k: int = 3
    stoch_smooth_d: int = 3
    stoch_overbought: int = 80
    stoch_oversold:   int = 20

    # Scoring
    min_points: float = 5.0   # need ≥ 5/6 to fire

    # Trade plan (ATR multiples on M15)
    sl_atr_mult:  float = 0.5
    tp1_atr_mult: float = 1.0
    tp2_atr_mult: float = 2.0
    min_rr:       float = 1.5


PLAN_B_CONFIG = ScalpingConfig()
