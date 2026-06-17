"""
Plan B — Scalping Strategy  |  US100 · US500 · US30
Gate 1  H1 trend    : EMA20/50 slope ±0.05% (mandatory — determines direction)
Gate 2  M15 vol     : ATR14/Close  >0.7%=0pt  0.4–0.7%=1pt  <0.4%=0.5pt
Gate 3  M15 sweep   : Liquidity sweep within 0.2×ATR of Swing H/L (20-bar, min 3 back)
Gate 4  M15 BOS     : Close beyond opposite swing within 3 candles of sweep (mandatory)
Gate 5  Session     : London/overlap/NY-early — FYI, always counts as 1 pt
Gate 6  M15 RSI+Stoch: RSI14>45 & Stoch<80 (BUY)  |  RSI14<55 & Stoch>20 (SELL)
Score threshold     : ≥ 5 / 6 to fire alert
R:R check           : TP1 (1.0×ATR) or TP2 (2.0×ATR) must achieve ≥ 1.5

Alert-only — never opens trades.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Sequence

import pandas as pd

from strategy.scalping_config import ScalpingConfig
from strategy.scalping_indicators import atr, ema, ema_slope_pct, rsi, stochastic

_LONDON_HRS  = (8, 12)
_OVERLAP_HRS = (13, 17)
_NY_START    = (13 * 60 + 30, 15 * 60 + 30)   # 13:30–15:30 UTC in minutes


@dataclass
class ScalpingResult:
    signal:     Optional[str] = None   # "BUY" or "SELL"
    reason:     str = ""
    gates_passed: list = field(default_factory=list)

    entry:      Optional[float] = None
    stop_loss:  Optional[float] = None
    tp1:        Optional[float] = None
    tp2:        Optional[float] = None
    rr:         Optional[float] = None
    score:      float = 0.0

    h1_bias:    Optional[str]  = None  # "up" | "down"
    atr_m15:    Optional[float] = None
    in_session: bool = True            # FYI — False = outside London/NY hours


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
        now_utc    = now_utc or datetime.now(timezone.utc).replace(tzinfo=None)
        news_times = high_impact_news_times or []
        result     = ScalpingResult()

        if not self.cfg.enabled:
            result.reason = "Plan B disabled"
            return result

        if len(h1_df) < self.MIN_H1_BARS or len(m15_df) < self.MIN_M15_BARS:
            result.reason = "not enough bars"
            return result

        score = 0.0

        # ── Gate 1: H1 trend (mandatory — determines direction) ──────────────
        direction = self._gate1_h1_trend(h1_df)
        if direction is None:
            result.reason = "gate1 FAIL: H1 ranging"
            return result
        score += 1.0
        result.h1_bias = direction.lower()
        result.gates_passed.append("gate1_trend")

        # ── Gate 2: M15 volatility (0 / 0.5 / 1 pt) ─────────────────────────
        atr_m15 = atr(m15_df, self.cfg.atr_period).iloc[-1]
        close_m15 = m15_df["close"].iloc[-1]
        atr_pct = (atr_m15 / close_m15 * 100) if close_m15 else 0.0
        result.atr_m15 = float(atr_m15)

        if atr_pct > self.cfg.atr_pct_max:        # > 0.7% → 0 pts
            g2_pts = 0.0
        elif atr_pct >= self.cfg.atr_pct_min:     # 0.4–0.7% → 1 pt
            g2_pts = 1.0
        else:                                       # < 0.4% → 0.5 pt
            g2_pts = 0.5
        score += g2_pts
        result.gates_passed.append(f"gate2_vol ({g2_pts}pts, ATR%={atr_pct:.2f})")

        # ── Gate 5: Session (FYI — always 1 pt) ──────────────────────────────
        in_sess, sess_note = self._gate5_session(now_utc, news_times)
        result.in_session = in_sess
        score += 1.0   # FYI: always counted, never blocks
        result.gates_passed.append(
            f"gate5_session ({sess_note})" if in_sess
            else f"gate5_FYI (off-session: {sess_note})"
        )

        # ── Gate 6: M15 RSI + Stochastic (0 or 1 pt) ────────────────────────
        g6_pts = 1.0 if self._gate6_momentum(m15_df, direction) else 0.0
        score += g6_pts
        result.gates_passed.append(f"gate6_momentum ({g6_pts}pt)")

        # ── Gates 3+4: M15 sweep + BOS (mandatory for entry) ─────────────────
        sweep_result = self._gate3_sweep_and_gate4_bos(m15_df, direction, float(atr_m15))
        if sweep_result is None:
            result.reason = f"gate3/4 FAIL: no sweep+BOS  (score so far={score:.1f})"
            result.score = score
            return result
        sweep_extreme, entry, bos_pts = sweep_result
        score += 2.0   # gate3=1 + gate4=1
        result.gates_passed.append("gate3_sweep + gate4_bos")

        result.score = score
        if score < self.cfg.min_points:
            result.reason = f"score {score:.1f}/6 < {self.cfg.min_points}"
            return result

        # ── Trade plan ────────────────────────────────────────────────────────
        sl_dist  = self.cfg.sl_atr_mult  * float(atr_m15)
        tp1_dist = self.cfg.tp1_atr_mult * float(atr_m15)
        tp2_dist = self.cfg.tp2_atr_mult * float(atr_m15)

        if direction == "BUY":
            sl  = sweep_extreme - sl_dist
            tp1 = entry + tp1_dist
            tp2 = entry + tp2_dist
        else:
            sl  = sweep_extreme + sl_dist
            tp1 = entry - tp1_dist
            tp2 = entry - tp2_dist

        risk = abs(entry - sl)
        if risk == 0:
            result.reason = "zero SL distance"
            return result

        rr = abs(tp1 - entry) / risk
        if rr < self.cfg.min_rr:
            result.reason = f"R:R {rr:.2f} < {self.cfg.min_rr}"
            return result

        result.signal    = direction
        result.entry     = entry
        result.stop_loss = sl
        result.tp1       = tp1
        result.tp2       = tp2
        result.rr        = rr
        result.reason    = f"all gates — score {score:.1f}/6"
        return result

    # ── Gate implementations ─────────────────────────────────────────────────

    def _gate1_h1_trend(self, h1_df: pd.DataFrame) -> Optional[str]:
        ema_fast  = ema(h1_df["close"], self.cfg.ema_fast)
        ema_slow  = ema(h1_df["close"], self.cfg.ema_slow)
        slope_pct = ema_slope_pct(ema_slow, lookback=3)
        if abs(slope_pct) <= self.cfg.ema_slope_min_pct:
            return None   # ranging
        if slope_pct > 0 and ema_fast.iloc[-1] > ema_slow.iloc[-1]:
            return "BUY"
        if slope_pct < 0 and ema_fast.iloc[-1] < ema_slow.iloc[-1]:
            return "SELL"
        return None

    def _gate3_sweep_and_gate4_bos(
        self, m15_df: pd.DataFrame, direction: str, atr_m15: float
    ) -> Optional[tuple[float, float, float]]:
        """
        Returns (sweep_extreme, entry_price, bos_pts=2.0) if both sweep and BOS
        are found, otherwise None.
        Sweep tolerance: Low/High within 0.2×ATR of Swing Low/High counts as sweep.
        BOS: close beyond opposite swing within the next 3 M15 candles after sweep.
        """
        lb, min_dist = self.cfg.swing_lookback, self.cfg.swing_confirm_bars
        df = m15_df.iloc[:-1]  # exclude potentially-open candle
        if len(df) < lb + 3:
            return None

        sw_window = df.iloc[-lb:-min_dist]
        swing_low  = float(sw_window["low"].min())
        swing_high = float(sw_window["high"].max())
        tol        = 0.2 * atr_m15

        # Search last lb candles for sweep
        search = df.iloc[-lb:]
        sweep_idx   : int | None = None
        sweep_extreme: float     = 0.0

        for i, (_, row) in enumerate(search.iterrows()):
            if direction == "BUY":
                if row["low"] < swing_low + tol and row["close"] > swing_low:
                    sweep_idx     = len(df) - lb + i
                    sweep_extreme = float(row["low"])
            else:
                if row["high"] > swing_high - tol and row["close"] < swing_high:
                    sweep_idx     = len(df) - lb + i
                    sweep_extreme = float(row["high"])

        if sweep_idx is None:
            return None

        # Gate 4: any of the next 3 M15 candles closes beyond opposite swing
        post_sweep = df.iloc[sweep_idx + 1 : sweep_idx + 4]
        entry: float | None = None
        for _, row in post_sweep.iterrows():
            if direction == "BUY" and row["close"] > swing_high:
                entry = float(row["close"])
                break
            if direction == "SELL" and row["close"] < swing_low:
                entry = float(row["close"])
                break

        if entry is None:
            return None

        return sweep_extreme, entry, 2.0

    def _gate5_session(
        self, now_utc: datetime, news_times: Sequence[datetime]
    ) -> tuple[bool, str]:
        from datetime import timedelta
        blackout = timedelta(minutes=self.cfg.news_blackout_minutes)
        for event in news_times:
            if abs((now_utc - event).total_seconds()) <= blackout.total_seconds():
                return False, f"within {self.cfg.news_blackout_minutes}min of news"
        h = now_utc.hour
        t = h * 60 + now_utc.minute
        if _LONDON_HRS[0] <= h < _LONDON_HRS[1]:
            return True, "London"
        if _OVERLAP_HRS[0] <= h < _OVERLAP_HRS[1]:
            return True, "London/NY overlap"
        if _NY_START[0] <= t <= _NY_START[1]:
            return True, "NY early"
        return False, "outside trading sessions"

    def _gate6_momentum(self, m15_df: pd.DataFrame, direction: str) -> bool:
        rsi_val = rsi(m15_df["close"], self.cfg.rsi_period).iloc[-1]
        k, _    = stochastic(m15_df, self.cfg.stoch_period,
                             self.cfg.stoch_smooth_k, self.cfg.stoch_smooth_d)
        stoch_val = k.iloc[-1]
        if pd.isna(rsi_val) or pd.isna(stoch_val):
            return False
        if direction == "BUY":
            return rsi_val > self.cfg.rsi_buy_threshold and stoch_val < self.cfg.stoch_overbought
        return rsi_val < self.cfg.rsi_sell_threshold and stoch_val > self.cfg.stoch_oversold
