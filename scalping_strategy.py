"""
Plan B - Scalping / Intraday Strategy
======================================

Entry timeframe : M15
Bias timeframe  : H1
Target          : 100-200 pts, held ~1-2 hours

Drop this into strategy/scalping_strategy.py. It is designed to run
alongside your existing Plan A (strategy/gold_strategy.py) without
touching it - the Orchestrator can call both and label alerts
"[Plan A]" / "[Plan B]".

Usage
-----
    from strategy.scalping_strategy import ScalpingStrategy
    from strategy.scalping_config import PLAN_B_CONFIG

    strat = ScalpingStrategy(PLAN_B_CONFIG)
    result = strat.run(h1_df, m15_df, now_utc=datetime.utcnow(),
                        high_impact_news_times=news_times)

    if result.signal is not None:
        # result.signal is "BUY" or "SELL"
        # result.entry / result.stop_loss / result.tp1 / result.tp2 / result.rr
        ...
    else:
        # result.reason explains why no signal was generated
        ...
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Sequence

import pandas as pd

from strategy.scalping_config import ScalpingConfig
from strategy.scalping_indicators import (
    atr,
    ema,
    ema_slope_pct,
    rsi,
    stochastic,
    swing_high,
    swing_low,
)


@dataclass
class ScalpingResult:
    signal: Optional[str] = None          # "BUY", "SELL", or None
    reason: str = ""                      # why no signal (or which gates passed)
    gates_passed: list = field(default_factory=list)

    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    rr: Optional[float] = None

    h1_bias: Optional[str] = None         # "up", "down", "ranging"
    atr_m15: Optional[float] = None


class ScalpingStrategy:
    """Plan B: M15 liquidity-sweep + BOS scalping strategy with an H1 bias filter."""

    MIN_H1_BARS = 60   # need enough bars for EMA50 + slope
    MIN_M15_BARS = 60  # need enough bars for ATR14, RSI14, Stoch14, swings

    def __init__(self, config: ScalpingConfig):
        self.cfg = config

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def run(
        self,
        h1_df: pd.DataFrame,
        m15_df: pd.DataFrame,
        now_utc: Optional[datetime] = None,
        high_impact_news_times: Optional[Sequence[datetime]] = None,
    ) -> ScalpingResult:
        """
        Run all 6 gates in order. Returns a ScalpingResult.
        `result.signal` is None unless ALL gates pass.
        """
        now_utc = now_utc or datetime.utcnow()
        news_times = high_impact_news_times or []
        result = ScalpingResult()

        if not self.cfg.enabled:
            result.reason = "Plan B disabled"
            return result

        if len(h1_df) < self.MIN_H1_BARS or len(m15_df) < self.MIN_M15_BARS:
            result.reason = "not enough data for H1/M15 analysis"
            return result

        # ---- Gate 1: H1 trend filter ----
        bias = self._gate1_h1_bias(h1_df)
        result.h1_bias = bias
        result.gates_passed.append("gate1_trend_filter")

        # ---- Gate 2: M15 volatility filter ----
        atr_m15 = atr(m15_df, self.cfg.atr_period).iloc[-1]
        close_m15 = m15_df["close"].iloc[-1]
        atr_pct = (atr_m15 / close_m15) * 100 if close_m15 else 0.0
        result.atr_m15 = float(atr_m15)
        if atr_pct > self.cfg.atr_pct_max:
            result.reason = f"gate2 FAIL: ATR%={atr_pct:.3f} > {self.cfg.atr_pct_max} (volatile)"
            return result
        result.gates_passed.append("gate2_volatility_filter")

        # ---- Gate 3: Liquidity sweep (M15) ----
        sweep_dir, sweep_level = self._gate3_liquidity_sweep(m15_df)
        if sweep_dir is None:
            result.reason = "gate3 FAIL: no liquidity sweep detected on M15"
            return result

        # Bias alignment: if H1 has a clear trend, reject counter-trend sweeps
        # (this is the "known bug" fix from Plan A, applied here too)
        if bias == "up" and sweep_dir == "SELL":
            result.reason = "gate3 FAIL: bearish sweep against H1 uptrend (rejected)"
            return result
        if bias == "down" and sweep_dir == "BUY":
            result.reason = "gate3 FAIL: bullish sweep against H1 downtrend (rejected)"
            return result
        result.gates_passed.append("gate3_liquidity_sweep")

        # ---- Gate 4: Break of structure confirmation (M15) ----
        if not self._gate4_bos_confirmation(m15_df, sweep_dir):
            result.reason = f"gate4 FAIL: no BOS confirmation for {sweep_dir} on M15"
            return result
        result.gates_passed.append("gate4_bos_confirmation")

        # ---- Gate 5: Session timing ----
        session_ok, session_note = self._gate5_session_filter(now_utc, news_times)
        if not session_ok:
            result.reason = f"gate5 FAIL: {session_note}"
            return result
        result.gates_passed.append(f"gate5_session_filter ({session_note})")

        # ---- Gate 6: RSI + Stochastic (M15) ----
        if not self._gate6_momentum_filter(m15_df, sweep_dir):
            result.reason = f"gate6 FAIL: RSI/Stochastic do not confirm {sweep_dir}"
            return result
        result.gates_passed.append("gate6_momentum_filter")

        # ---- All gates passed: build the trade plan ----
        entry, sl, tp1, tp2, rr = self._build_trade_plan(
            m15_df, sweep_dir, sweep_level, atr_m15
        )

        if rr < self.cfg.min_rr:
            result.reason = f"R:R FAIL: {rr:.2f} < {self.cfg.min_rr} (low quality, do not trade)"
            result.entry, result.stop_loss, result.tp1, result.tp2, result.rr = (
                entry, sl, tp1, tp2, rr,
            )
            return result

        result.signal = sweep_dir
        result.entry, result.stop_loss, result.tp1, result.tp2, result.rr = (
            entry, sl, tp1, tp2, rr,
        )
        result.reason = "all gates passed"
        return result

    # ------------------------------------------------------------------
    # Gate 1 - H1 trend filter
    # ------------------------------------------------------------------
    def _gate1_h1_bias(self, h1_df: pd.DataFrame) -> str:
        ema_fast = ema(h1_df["close"], self.cfg.ema_fast)
        ema_slow = ema(h1_df["close"], self.cfg.ema_slow)
        slope = ema_slope_pct(ema_fast, lookback=3)

        if abs(slope) <= self.cfg.ema_slope_min_pct:
            return "ranging"

        if slope > 0 and ema_fast.iloc[-1] > ema_slow.iloc[-1]:
            return "up"
        if slope < 0 and ema_fast.iloc[-1] < ema_slow.iloc[-1]:
            return "down"
        return "ranging"

    # ------------------------------------------------------------------
    # Gate 2 handled inline in run() (single ATR% check)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Gate 3 - Liquidity sweep on M15
    # ------------------------------------------------------------------
    def _gate3_liquidity_sweep(self, m15_df: pd.DataFrame):
        """
        Returns (direction, level) where direction is "BUY"/"SELL"/None
        and level is the swept swing level (used later for SL placement).
        """
        last = m15_df.iloc[-1]
        lookback = self.cfg.swing_lookback
        confirm = self.cfg.swing_confirm_bars

        # Use bars BEFORE the current one to find the swing reference
        history = m15_df.iloc[:-1]

        s_low = swing_low(history, lookback, confirm)
        s_high = swing_high(history, lookback, confirm)

        # Bullish sweep: pierce below swing low, close back above it
        if s_low is not None and last["low"] < s_low and last["close"] > s_low:
            return "BUY", s_low

        # Bearish sweep: pierce above swing high, close back below it
        if s_high is not None and last["high"] > s_high and last["close"] < s_high:
            return "SELL", s_high

        return None, None

    # ------------------------------------------------------------------
    # Gate 4 - Break of structure confirmation on M15
    # ------------------------------------------------------------------
    def _gate4_bos_confirmation(self, m15_df: pd.DataFrame, direction: str) -> bool:
        lookback = self.cfg.swing_lookback
        confirm = self.cfg.swing_confirm_bars

        # Reference structure excludes the current (sweep/BOS) candle
        history = m15_df.iloc[:-1]
        last_close = m15_df["close"].iloc[-1]

        if direction == "BUY":
            ref_high = swing_high(history, lookback, confirm)
            return ref_high is not None and last_close > ref_high

        if direction == "SELL":
            ref_low = swing_low(history, lookback, confirm)
            return ref_low is not None and last_close < ref_low

        return False

    # ------------------------------------------------------------------
    # Gate 5 - Session filter
    # ------------------------------------------------------------------
    def _gate5_session_filter(
        self, now_utc: datetime, news_times: Sequence[datetime]
    ) -> tuple[bool, str]:
        # News blackout: reject if within +/- N minutes of a high-impact USD event
        blackout = timedelta(minutes=self.cfg.news_blackout_minutes)
        for event_time in news_times:
            if abs((now_utc - event_time).total_seconds()) <= blackout.total_seconds():
                return False, f"within {self.cfg.news_blackout_minutes}min of high-impact news"

        hour = now_utc.hour
        ov_start, ov_end = self.cfg.london_ny_overlap
        ldn_start, ldn_end = self.cfg.london_session
        ny_start, ny_end = self.cfg.ny_session

        if ov_start <= hour < ov_end:
            return True, "London/NY overlap"
        if ldn_start <= hour < ldn_end:
            return True, "London session"
        if ny_start <= hour < ny_end:
            return True, "New York session"

        return False, "outside trading sessions (low liquidity)"

    # ------------------------------------------------------------------
    # Gate 6 - RSI + Stochastic (M15)
    # ------------------------------------------------------------------
    def _gate6_momentum_filter(self, m15_df: pd.DataFrame, direction: str) -> bool:
        rsi_val = rsi(m15_df["close"], self.cfg.rsi_period).iloc[-1]
        k, _d = stochastic(
            m15_df,
            self.cfg.stoch_period,
            self.cfg.stoch_smooth_k,
            self.cfg.stoch_smooth_d,
        )
        stoch_val = k.iloc[-1]

        if pd.isna(rsi_val) or pd.isna(stoch_val):
            return False

        if direction == "BUY":
            return rsi_val > 50 and stoch_val < self.cfg.stoch_overbought

        if direction == "SELL":
            return rsi_val < 50 and stoch_val > self.cfg.stoch_oversold

        return False

    # ------------------------------------------------------------------
    # Trade plan (Entry / SL / TP1 / TP2 / R:R)
    # ------------------------------------------------------------------
    def _build_trade_plan(
        self,
        m15_df: pd.DataFrame,
        direction: str,
        sweep_level: float,
        atr_m15: float,
    ) -> tuple[float, float, float, float, float]:
        entry = float(m15_df["close"].iloc[-1])  # BOS candle close
        sl_dist_atr = self.cfg.sl_atr_mult * atr_m15
        tp1_dist = self.cfg.tp1_atr_mult * atr_m15
        tp2_dist = self.cfg.tp2_atr_mult * atr_m15

        if direction == "BUY":
            stop_loss = sweep_level - sl_dist_atr
            tp1 = entry + tp1_dist
            tp2 = entry + tp2_dist
            risk = entry - stop_loss
            reward = tp1 - entry
        else:  # SELL
            stop_loss = sweep_level + sl_dist_atr
            tp1 = entry - tp1_dist
            tp2 = entry - tp2_dist
            risk = stop_loss - entry
            reward = entry - tp1

        rr = (reward / risk) if risk > 0 else 0.0
        return entry, stop_loss, tp1, tp2, rr
