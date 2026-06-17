"""
Plan A — Smart Trading Bot Strategy  |  US100 · US500 · US30
Gate 1  H4 trend   : EMA20/EMA50 slope (±0.05% threshold)
Gate 2  H4 vol     : ATR14/Close ≤ 1.8%
Gate 3  H1 sweep   : Liquidity sweep of last Swing H/L (20-bar window, min 5 back)
Gate 4  H1 BOS     : Close beyond opposite swing after sweep
Gate 5  Session    : London/overlap/NY-early — FYI only, never blocks
Gate 6  H1 RSI     : RSI14 > 50 (BUY) or < 50 (SELL)
R:R check          : TP1 (nearest swing) or TP2 (2.5×ATR) must achieve ≥ 1.5
Alert-only — never opens trades.
"""

import logging
import math
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from execution.models import Signal
from strategy.base import MultiTimeframeCandles, StrategyBase, TF_H1, TF_H4

logger = logging.getLogger(__name__)

# ── Parameters ──────────────────────────────────────────────────────────────
_EMA_FAST       = 20
_EMA_SLOW       = 50
_SLOPE_LB       = 3            # EMA50[0] − EMA50[−3]
_SLOPE_MIN_PCT  = 0.05
_ATR_PERIOD     = 14
_ATR_PCT_MAX    = 1.8          # percent
_SWING_LB       = 20           # H1 bars to define swing reference
_SWING_MIN_DIST = 5            # swing must be ≥ 5 bars from current
_SWEEP_LB       = 5            # search sweep in last N closed H1 candles
_RSI_PERIOD     = 14
_SL_ATR_MULT    = 0.5
_TP2_ATR_MULT   = 2.5
_MIN_RR         = 1.5
_LOTS           = 1.0

_LONDON_HRS    = (8, 12)
_OVERLAP_HRS   = (13, 17)
_NY_EARLY_MINS = (13 * 60 + 30, 15 * 60 + 30)


# ── Pure indicator helpers ───────────────────────────────────────────────────

def _ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return [math.nan] * len(values)
    k = 2.0 / (period + 1)
    result = [math.nan] * (period - 1)
    result.append(sum(values[:period]) / period)
    for v in values[period:]:
        result.append(result[-1] + k * (v - result[-1]))
    return result


def _atr(candles, period: int) -> list[float]:
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


def _rsi(closes: list[float], period: int) -> list[float]:
    result: list[float] = [math.nan] * period
    if len(closes) < period + 1:
        return result + [math.nan] * max(0, len(closes) - period)
    ag = al = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        ag += max(d, 0.0)
        al += max(-d, 0.0)
    ag /= period
    al /= period
    result.append(100.0 if al == 0 else 100 - 100 / (1 + ag / al))
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0.0)) / period
        al = (al * (period - 1) + max(-d, 0.0)) / period
        result.append(100.0 if al == 0 else 100 - 100 / (1 + ag / al))
    return result


def _in_session(now_utc: datetime) -> tuple[bool, str]:
    h = now_utc.hour
    t = h * 60 + now_utc.minute
    if _LONDON_HRS[0] <= h < _LONDON_HRS[1]:
        return True, "London"
    if _OVERLAP_HRS[0] <= h < _OVERLAP_HRS[1]:
        return True, "London/NY overlap"
    if _NY_EARLY_MINS[0] <= t <= _NY_EARLY_MINS[1]:
        return True, "NY early"
    return False, "off-session"


# ── Strategy ─────────────────────────────────────────────────────────────────

class SmartTradingBotStrategy(StrategyBase):
    """6-gate alert engine for US100/US500/US30 on H4+H1."""

    def __init__(self, epic: str = "US500", lots: float = _LOTS):
        self.epic = epic
        self.lots = lots

    @property
    def name(self) -> str:
        return f"PlanA_{self.epic}"

    def evaluate(self, candles: MultiTimeframeCandles) -> Optional[Signal]:
        h4 = candles.get(TF_H4, [])
        h1 = candles.get(TF_H1, [])
        now_utc = datetime.now(tz=ZoneInfo("UTC"))

        # Gate 5 — session (FYI, non-blocking)
        in_sess, sess_note = _in_session(now_utc)
        sess_comment = "" if in_sess else "⚠️ Off-session"
        if not in_sess:
            logger.info("[%s] gate5 FYI: %s — continuing", self.epic, sess_note)

        # Gate 1 — H4 trend: EMA20/50 + slope of EMA50
        if len(h4) < _EMA_SLOW + _SLOPE_LB + 2:
            logger.debug("[%s] gate1 SKIP: only %d H4 bars", self.epic, len(h4))
            return None
        closes_h4 = [c.close for c in h4]
        ema20 = _ema(closes_h4, _EMA_FAST)
        ema50 = _ema(closes_h4, _EMA_SLOW)
        e20, e50, e50_old = ema20[-1], ema50[-1], ema50[-1 - _SLOPE_LB]
        if math.isnan(e50) or math.isnan(e50_old) or e50 == 0:
            return None
        slope_pct = (e50 - e50_old) / e50 * 100
        if slope_pct > _SLOPE_MIN_PCT and e20 > e50:
            direction = "buy"
        elif slope_pct < -_SLOPE_MIN_PCT and e20 < e50:
            direction = "sell"
        else:
            logger.info("[%s] gate1 SKIP: ranging (slope=%.3f%%)", self.epic, slope_pct)
            return None
        logger.info("[%s] gate1 PASS: %s slope=%.3f%%", self.epic, direction, slope_pct)

        # Gate 2 — H4 volatility: ATR14 / Close ≤ 1.8%
        atr_h4 = _atr(h4, _ATR_PERIOD)
        atr14 = atr_h4[-1]
        close_h4 = h4[-1].close
        if math.isnan(atr14) or close_h4 == 0:
            return None
        atr_pct = atr14 / close_h4 * 100
        if atr_pct > _ATR_PCT_MAX:
            logger.info("[%s] gate2 SKIP: ATR%%=%.2f > %.1f", self.epic, atr_pct, _ATR_PCT_MAX)
            return None
        logger.info("[%s] gate2 PASS: ATR%%=%.2f atr=%.4f", self.epic, atr_pct, atr14)

        # Gates 3+4 — H1 liquidity sweep + BOS
        if len(h1) < _SWING_LB + 3:
            logger.debug("[%s] gate3 SKIP: only %d H1 bars", self.epic, len(h1))
            return None
        h1c = h1[:-1]  # exclude potentially-open current candle

        # Swing reference: last 20 bars, at least 5 back from current
        sw = h1c[-_SWING_LB:-_SWING_MIN_DIST]
        if len(sw) < 3:
            return None
        swing_low  = min(c.low  for c in sw)
        swing_high = max(c.high for c in sw)

        # Gate 3: sweep in last _SWEEP_LB closed candles
        sweep_idx: int | None = None
        sweep_extreme: float = 0.0
        for i in range(max(0, len(h1c) - _SWEEP_LB), len(h1c)):
            c = h1c[i]
            if direction == "buy" and c.low < swing_low and c.close > swing_low:
                sweep_idx, sweep_extreme = i, c.low
            elif direction == "sell" and c.high > swing_high and c.close < swing_high:
                sweep_idx, sweep_extreme = i, c.high
        if sweep_idx is None:
            logger.info("[%s] gate3 SKIP: no %s sweep", self.epic, direction.upper())
            return None
        logger.info("[%s] gate3 PASS: sweep h1[%d] extreme=%.2f", self.epic, sweep_idx, sweep_extreme)

        # Gate 4: BOS — any candle after sweep closes beyond opposite swing
        entry: float | None = None
        for i in range(sweep_idx + 1, len(h1c)):
            c = h1c[i]
            if direction == "buy" and c.close > swing_high:
                entry = c.close
                break
            if direction == "sell" and c.close < swing_low:
                entry = c.close
                break
        if entry is None:
            logger.info("[%s] gate4 SKIP: no BOS after sweep", self.epic)
            return None
        logger.info("[%s] gate4 PASS: BOS entry=%.2f", self.epic, entry)

        # Gate 6 — H1 RSI14
        rsi_vals = _rsi([c.close for c in h1c], _RSI_PERIOD)
        rsi_last = rsi_vals[-1] if rsi_vals else math.nan
        if math.isnan(rsi_last):
            return None
        if direction == "buy" and rsi_last <= 50:
            logger.info("[%s] gate6 SKIP: RSI=%.1f ≤ 50 for BUY", self.epic, rsi_last)
            return None
        if direction == "sell" and rsi_last >= 50:
            logger.info("[%s] gate6 SKIP: RSI=%.1f ≥ 50 for SELL", self.epic, rsi_last)
            return None
        logger.info("[%s] gate6 PASS: RSI=%.1f", self.epic, rsi_last)

        # Trade plan: SL, TP1 (nearest swing pivot), TP2 (ATR-based)
        if direction == "buy":
            sl  = sweep_extreme - _SL_ATR_MULT * atr14
            tp2 = entry + _TP2_ATR_MULT * atr14
            tp1 = self._nearest_pivot(h1c, entry, "high")
        else:
            sl  = sweep_extreme + _SL_ATR_MULT * atr14
            tp2 = entry - _TP2_ATR_MULT * atr14
            tp1 = self._nearest_pivot(h1c, entry, "low")

        sl_dist = abs(entry - sl)
        if sl_dist == 0:
            return None

        # R:R check — prefer TP1 (swing), fall back to TP2 (ATR)
        tp_use: float | None = None
        if tp1 is not None and abs(tp1 - entry) / sl_dist >= _MIN_RR:
            tp_use = tp1
        if tp_use is None and abs(tp2 - entry) / sl_dist >= _MIN_RR:
            tp_use = tp2
        if tp_use is None:
            logger.info("[%s] RR SKIP: TP1=%s TP2=%.2f neither meets %.1f",
                        self.epic, f"{tp1:.2f}" if tp1 else "—", tp2, _MIN_RR)
            return None

        rr = abs(tp_use - entry) / sl_dist
        logger.info("[%s] ✓ SIGNAL %s entry=%.2f sl=%.2f tp=%.2f tp2=%.2f rr=%.2f",
                    self.epic, direction.upper(), entry, sl, tp_use, tp2, rr)
        return Signal(
            direction=direction,
            lots=self.lots,
            confirmed=True,
            entry=entry,
            stop_loss=sl,
            take_profit=tp_use,
            take_profit2=tp2,
            comment=sess_comment,
            timestamp=now_utc.isoformat(),
        )

    def _nearest_pivot(self, h1: list, entry: float, side: str) -> Optional[float]:
        """Nearest confirmed pivot high (side='high') or low (side='low') beyond entry."""
        candidates = []
        lb = 2  # confirm bars each side
        for i in range(lb, len(h1) - lb):
            c = h1[i]
            if side == "high":
                if (c.high > entry and
                        all(c.high > h1[i - j].high for j in range(1, lb + 1)) and
                        all(c.high > h1[i + j].high for j in range(1, lb + 1))):
                    candidates.append(c.high)
            else:
                if (c.low < entry and
                        all(c.low < h1[i - j].low for j in range(1, lb + 1)) and
                        all(c.low < h1[i + j].low for j in range(1, lb + 1))):
                    candidates.append(c.low)
        if not candidates:
            return None
        return min(candidates) if side == "high" else max(candidates)
