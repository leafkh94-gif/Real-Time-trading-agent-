"""
Configuration for Plan B - Scalping/Intraday Strategy (M15 entries / H1 bias).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ScalpingConfig:
    # Master switch — lets you run Plan B alongside Plan A without touching it
    enabled: bool = True

    # --- Timing ---
    scan_interval_s: int = 300          # scan every 5 min
    alert_cooldown_s: int = 1800        # 30 min lock per instrument
    time_stop_s: int = 7200             # 2h time stop if TP1 not hit

    # --- Gate 1: H1 trend filter ---
    ema_fast: int = 20
    ema_slow: int = 50
    ema_slope_min_pct: float = 0.05

    # --- Gate 2: M15 volatility filter ---
    atr_period: int = 14
    atr_pct_max: float = 0.5            # skip if ATR/Close% > this

    # --- Gate 3: Liquidity sweep (M15) ---
    swing_lookback: int = 15            # bars to look back for swing high/low
    swing_confirm_bars: int = 3         # bars each side to confirm a pivot

    # --- Gate 5: Session filter (UTC hours) ---
    london_ny_overlap: tuple = (13, 17)
    london_session: tuple = (8, 17)
    ny_session: tuple = (13, 22)
    news_blackout_minutes: int = 30     # avoid +/- 30 min around high-impact news

    # --- Gate 6: RSI / Stochastic (M15) ---
    rsi_period: int = 14
    stoch_period: int = 14
    stoch_smooth_k: int = 3
    stoch_smooth_d: int = 3
    stoch_overbought: int = 80
    stoch_oversold: int = 20

    # --- Entry / SL / TP (M15 ATR multiples) ---
    sl_atr_mult: float = 0.3
    tp1_atr_mult: float = 1.5
    tp2_atr_mult: float = 2.5
    min_rr: float = 1.5                 # R:R floor vs TP1


PLAN_B_CONFIG = ScalpingConfig()
