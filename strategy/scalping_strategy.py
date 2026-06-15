"""
Plan B — Scalping / Intraday Strategy
======================================

Entry timeframe : M15
Bias timeframe  : H1
Target          : 100-200 pts, held ~1-2 hours

6 gates (all must pass):
  Gate 1 — H1 EMA20/50 trend bias (up / down / ranging)
  Gate 2 — M15 ATR% volatility ceiling
  Gate 3 — M15 liquidity sweep aligned with H1 bias
  Gate 4 — M15 BOS (close breaks last confirmed swing level)
  Gate 5 — London / NY session + news blackout
  Gate 6 — M15 RSI > 50 and Stochastic not overbought (BUY) or RSI < 50 and Stoch not oversold (SELL)
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
    signal:     Optional[str] = None    # "BUY", "SELL", or None
    reason:     str = ""
    gates_passed: list = field(default_factory=list)

    entry:      Optional[float] = None
    stop_loss:  Optional[float] = None
    tp1:        Optional[float] = None
    tp2:        Optional[float] = None
    rr:         Optional[float] = None

    h1_bias:    Optional[str]   = None  # "up", "down", "ranging"
    atr_m15:    Optional[float] = None
    in_session: bool = True             # FYI only — False = outside London/NY hours


class ScalpingStrategy:
    MIN_H1_BARS  = 60
    MIN_M15_BARS = 60

    def __init__(self, config: ScalpingConfig):
        self.cfg = config

    def run(
        self,
        h1_df: pd.DataFrame,
        m15_df: pd.DataFrame,
        now_utc: Optional[datetime] = None,
        high_impact_news_times: Optional[Sequence[datetime]] = None,
    ) -> ScalpingResult:
        now_utc    = now_utc or datetime.utcnow()
        news_times = high_impact_news_times or []
        result     = ScalpingResult()

        if not self.cfg.enabled:
            result.reason = "Plan B disabled"
            return result

        if len(h1_df) < self.MIN_H1_BARS or len(m15_df) < self.MIN_M15_BARS:
            result.reason = "not enough bars for H1/M15 analysis"
            return result

        # Gate 1: H1 trend bias
        bias = self._gate1_h1_bias(h1_df)
        result.h1_bias = bias
        result.gates_passed.append("gate1_trend_filter")

        # Gate 2: M15 volatility ceiling
        atr_m15   = atr(m15_df, self.cfg.atr_period).iloc[-1]
        close_m15 = m15_df["close"].iloc[-1]
        atr_pct   = (atr_m15 / close_m15) * 100 if close_m15 else 0.0
        result.atr_m15 = float(atr_m15)
        if atr_pct > self.cfg.atr_pct_max:
            result.reason = (f"gate2 FAIL: ATR%={atr_pct:.3f} > "
                             f"{self.cfg.atr_pct_max} (too volatile)")
            return result
        result.gates_passed.append("gate2_volatility_filter")

        # Gate 3: M15 liquidity sweep (must align with H1 bias)
        sweep_dir, sweep_level = self._gate3_liquidity_sweep(m15_df)
        if sweep_dir is None:
            result.reason = "gate3 FAIL: no liquidity sweep on M15"
            return result
        if bias == "up" and sweep_dir == "SELL":
            result.reason = "gate3 FAIL: bearish sweep against H1 uptrend"
            return result
        if bias == "down" and sweep_dir == "BUY":
            result.reason = "gate3 FAIL: bullish sweep against H1 downtrend"
            return result
        result.gates_passed.append("gate3_liquidity_sweep")

        # Gate 4: M15 BOS confirmation
        if not self._gate4_bos(m15_df, sweep_dir):
            result.reason = f"gate4 FAIL: no M15 BOS for {sweep_dir}"
            return result
        result.gates_passed.append("gate4_bos_confirmation")

        # Gate 5: session + news filter (FYI only — not a hard block)
        session_ok, session_note = self._gate5_session(now_utc, news_times)
        result.in_session = session_ok
        result.gates_passed.append(
            f"gate5_session ({session_note})" if session_ok
            else f"gate5_FYI (off-session: {session_note})"
        )

        # Gate 6: RSI + Stochastic
        if not self._gate6_momentum(m15_df, sweep_dir):
            result.reason = f"gate6 FAIL: RSI/Stoch do not confirm {sweep_dir}"
            return result
        result.gates_passed.append("gate6_momentum_filter")

        # Build trade plan
        entry, sl, tp1, tp2, rr = self._build_trade_plan(
            m15_df, sweep_dir, sweep_level, atr_m15
        )

        if rr < self.cfg.min_rr:
            result.reason = f"R:R FAIL: {rr:.2f} < {self.cfg.min_rr}"
            result.entry, result.stop_loss, result.tp1, result.tp2, result.rr = (
                entry, sl, tp1, tp2, rr)
            return result

        result.signal     = sweep_dir
        result.entry      = entry
        result.stop_loss  = sl
        result.tp1        = tp1
        result.tp2        = tp2
        result.rr         = rr
        result.reason     = "all gates passed"
        return result

    # ── Gate implementations ─────────────────────────────────────────────────

    def _gate1_h1_bias(self, h1_df: pd.DataFrame) -> str:
        ema_fast = ema(h1_df["close"], self.cfg.ema_fast)
        ema_slow = ema(h1_df["close"], self.cfg.ema_slow)
        slope    = ema_slope_pct(ema_fast, lookback=3)
        if abs(slope) <= self.cfg.ema_slope_min_pct:
            return "ranging"
        if slope > 0 and ema_fast.iloc[-1] > ema_slow.iloc[-1]:
            return "up"
        if slope < 0 and ema_fast.iloc[-1] < ema_slow.iloc[-1]:
            return "down"
        return "ranging"

    def _gate3_liquidity_sweep(self, m15_df: pd.DataFrame):
        last    = m15_df.iloc[-1]
        history = m15_df.iloc[:-1]
        lb, cb  = self.cfg.swing_lookback, self.cfg.swing_confirm_bars

        s_low  = swing_low( history, lb, cb)
        s_high = swing_high(history, lb, cb)

        if s_low  is not None and last["low"]  < s_low  and last["close"] > s_low:
            return "BUY", s_low
        if s_high is not None and last["high"] > s_high and last["close"] < s_high:
            return "SELL", s_high
        return None, None

    def _gate4_bos(self, m15_df: pd.DataFrame, direction: str) -> bool:
        history    = m15_df.iloc[:-1]
        last_close = m15_df["close"].iloc[-1]
        lb, cb     = self.cfg.swing_lookback, self.cfg.swing_confirm_bars
        if direction == "BUY":
            ref = swing_high(history, lb, cb)
            return ref is not None and last_close > ref
        ref = swing_low(history, lb, cb)
        return ref is not None and last_close < ref

    def _gate5_session(
        self, now_utc: datetime, news_times: Sequence[datetime]
    ) -> tuple[bool, str]:
        blackout = timedelta(minutes=self.cfg.news_blackout_minutes)
        for event in news_times:
            if abs((now_utc - event).total_seconds()) <= blackout.total_seconds():
                return False, f"within {self.cfg.news_blackout_minutes}min of news"
        hour = now_utc.hour
        ov_s, ov_e   = self.cfg.london_ny_overlap
        ldn_s, ldn_e = self.cfg.london_session
        ny_s, ny_e   = self.cfg.ny_session
        if ov_s  <= hour < ov_e:  return True, "London/NY overlap"
        if ldn_s <= hour < ldn_e: return True, "London session"
        if ny_s  <= hour < ny_e:  return True, "New York session"
        return False, "outside trading sessions"

    def _gate6_momentum(self, m15_df: pd.DataFrame, direction: str) -> bool:
        rsi_val    = rsi(m15_df["close"], self.cfg.rsi_period).iloc[-1]
        k, _d      = stochastic(m15_df, self.cfg.stoch_period,
                                self.cfg.stoch_smooth_k, self.cfg.stoch_smooth_d)
        stoch_val  = k.iloc[-1]
        if pd.isna(rsi_val) or pd.isna(stoch_val):
            return False
        if direction == "BUY":
            return rsi_val > 50 and stoch_val < self.cfg.stoch_overbought
        return rsi_val < 50 and stoch_val > self.cfg.stoch_oversold

    def _build_trade_plan(
        self, m15_df: pd.DataFrame, direction: str,
        sweep_level: float, atr_m15: float,
    ) -> tuple[float, float, float, float, float]:
        entry    = float(m15_df["close"].iloc[-1])
        sl_dist  = self.cfg.sl_atr_mult  * atr_m15
        tp1_dist = self.cfg.tp1_atr_mult * atr_m15
        tp2_dist = self.cfg.tp2_atr_mult * atr_m15

        if direction == "BUY":
            sl   = sweep_level - sl_dist
            tp1  = entry + tp1_dist
            tp2  = entry + tp2_dist
            risk = entry - sl
        else:
            sl   = sweep_level + sl_dist
            tp1  = entry - tp1_dist
            tp2  = entry - tp2_dist
            risk = sl - entry

        reward = abs(tp1 - entry)
        rr     = (reward / risk) if risk > 0 else 0.0
        return entry, sl, tp1, tp2, rr
