"""
Unified Strategy V4 (Final Simplified)  |  US100 · US500 · US30
Gate 1  EMA Cross   : EMA20 > EMA50 → BUY, EMA20 < EMA50 → SELL
                      Must agree on BOTH H1 and M15.
                      Skip only if EMAs are exactly equal or data insufficient.
Gate 2  Sweep+BOS   : Liquidity sweep of swing H/L (±sweep_tolerance×ATR),
                      BOS close within 3 candles.
                      Fires if EITHER H1 or M15 (or both) confirm.
Trade plan (calibrated per instrument):
  SL  = sweep extreme ± sl_atr_mult × ATR (ATR from confirming TF; M15 preferred)
  TP1 = Entry ± tp1_atr_mult_m15 × ATR14_M15   (short target)
  TP2 = Entry ± tp2_atr_mult_h1  × ATR14_H1    (long target)
  R:R ≥ min_rr required (checked against TP1, then TP2)
Alert-only — never opens trades. 24h operation.

Instrument calibration rationale:
  US100 — most volatile (beta ~1.2–1.3 vs S&P): wider SL and sweep tolerance,
           higher TP2 multiplier since it trends further.
  US500 — moderate volatility: baseline / reference calibration.
  US30  — large nominal pip value; ATR accounts for this but needs a slightly
           wider SL buffer due to price scale (wide raw-point moves).
"""

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from execution.models import Signal
from strategy.base import MultiTimeframeCandles, StrategyBase, TF_H1, TF_M15

logger = logging.getLogger(__name__)

# ── Per-instrument configuration ─────────────────────────────────────────────

@dataclass(frozen=True)
class InstrumentConfig:
    sweep_tolerance:  float = 0.20   # sweep proximity as fraction of ATR
    swing_lb:         int   = 20     # bars for swing reference window
    swing_min_dist:   int   = 3      # swing must be ≥ this many bars back
    bos_window:       int   = 3      # BOS must close within this many candles
    sl_atr_mult:      float = 0.50   # SL distance in ATR units
    tp1_atr_mult_m15: float = 1.00   # TP1 as multiple of M15 ATR
    tp2_atr_mult_h1:  float = 2.50   # TP2 as multiple of H1 ATR
    min_rr:           float = 1.50   # minimum R:R ratio required
    atr_period:       int   = 14
    lots:             float = 1.0


_INSTRUMENT_CONFIGS: dict[str, InstrumentConfig] = {
    # Nasdaq 100 — highest volatility (beta ~1.2–1.3 vs S&P)
    # Wider SL/sweep tolerance to absorb larger intra-bar swings;
    # higher TP2 target because trends extend further before reversing.
    "US100": InstrumentConfig(
        sweep_tolerance  = 0.25,
        sl_atr_mult      = 0.60,
        tp1_atr_mult_m15 = 1.00,
        tp2_atr_mult_h1  = 3.00,
        min_rr           = 1.50,
    ),
    # S&P 500 — moderate volatility; baseline calibration.
    "US500": InstrumentConfig(
        sweep_tolerance  = 0.20,
        sl_atr_mult      = 0.50,
        tp1_atr_mult_m15 = 1.00,
        tp2_atr_mult_h1  = 2.50,
        min_rr           = 1.50,
    ),
    # Dow Jones — disciplined on a %-basis but large raw pip values;
    # slightly wider SL buffer to cope with high nominal point moves.
    "US30": InstrumentConfig(
        sweep_tolerance  = 0.20,
        sl_atr_mult      = 0.55,
        tp1_atr_mult_m15 = 1.00,
        tp2_atr_mult_h1  = 2.50,
        min_rr           = 1.50,
    ),
}

_DEFAULT_CONFIG = InstrumentConfig()   # fallback for unknown epics


# ── Pure indicator helpers ────────────────────────────────────────────────────

def _ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return [math.nan] * len(values)
    k = 2.0 / (period + 1)
    result = [math.nan] * (period - 1)
    result.append(sum(values[:period]) / period)
    for v in values[period:]:
        result.append(result[-1] + k * (v - result[-1]))
    return result


def _atr(candles: list, period: int) -> list[float]:
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


def _ema_direction(candles: list) -> Optional[str]:
    """Return 'buy', 'sell', or None if data insufficient or EMAs exactly equal."""
    if len(candles) < 52:   # EMA50 + 2 warm-up bars
        return None
    closes = [c.close for c in candles]
    ema20  = _ema(closes, 20)
    ema50  = _ema(closes, 50)
    e20, e50 = ema20[-1], ema50[-1]
    if math.isnan(e20) or math.isnan(e50) or e50 == 0:
        return None
    if e20 == e50:
        return None   # exact cross — direction undefined
    return "buy" if e20 > e50 else "sell"


def _sweep_and_bos(
    candles: list,
    direction: str,
    atr_val: float,
    cfg: InstrumentConfig,
) -> Optional[tuple[float, float]]:
    """
    Returns (sweep_extreme, bos_entry) if sweep + BOS found within cfg.bos_window
    candles after sweep, else None.
    `candles` must be closed candles only (caller excludes open candle).
    """
    min_len = cfg.swing_lb + cfg.bos_window + 1
    if len(candles) < min_len:
        return None
    tol = cfg.sweep_tolerance * atr_val

    sw_window  = candles[-cfg.swing_lb : -cfg.swing_min_dist]
    swing_low  = min(c.low  for c in sw_window)
    swing_high = max(c.high for c in sw_window)

    search     = candles[-cfg.swing_lb:]
    sweep_idx: Optional[int] = None
    sweep_extreme: float     = 0.0

    for i, c in enumerate(search):
        abs_i = len(candles) - cfg.swing_lb + i
        if direction == "buy" and c.low < swing_low + tol and c.close > swing_low:
            sweep_idx     = abs_i
            sweep_extreme = c.low
        elif direction == "sell" and c.high > swing_high - tol and c.close < swing_high:
            sweep_idx     = abs_i
            sweep_extreme = c.high

    if sweep_idx is None:
        return None

    post_sweep = candles[sweep_idx + 1 : sweep_idx + 1 + cfg.bos_window]
    for c in post_sweep:
        if direction == "buy"  and c.close > swing_high:
            return sweep_extreme, c.close
        if direction == "sell" and c.close < swing_low:
            return sweep_extreme, c.close

    return None


# ── Strategy ──────────────────────────────────────────────────────────────────

class SmartTradingBotStrategy(StrategyBase):
    """Unified 2-gate alert engine for US100/US500/US30 on H1+M15."""

    def __init__(self, epic: str = "US500"):
        self.epic = epic
        self._cfg = _INSTRUMENT_CONFIGS.get(epic, _DEFAULT_CONFIG)

    @property
    def name(self) -> str:
        return f"Unified_{self.epic}"

    def evaluate(self, candles: MultiTimeframeCandles) -> Optional[Signal]:
        h1  = candles.get(TF_H1,  [])
        m15 = candles.get(TF_M15, [])
        cfg = self._cfg
        now_utc = datetime.now(tz=ZoneInfo("UTC"))

        # ── Gate 1: EMA direction — must agree on both H1 and M15 ───────────
        dir_h1  = _ema_direction(h1)
        dir_m15 = _ema_direction(m15)

        if dir_h1 is None:
            logger.info("[%s] gate1 SKIP: H1 EMA neutral or insufficient data", self.epic)
            return None
        if dir_m15 is None:
            logger.info("[%s] gate1 SKIP: M15 EMA neutral or insufficient data", self.epic)
            return None
        if dir_h1 != dir_m15:
            logger.info("[%s] gate1 SKIP: H1=%s vs M15=%s disagree",
                        self.epic, dir_h1, dir_m15)
            return None

        direction = dir_h1
        logger.info("[%s] gate1 PASS: %s (H1+M15 agree)", self.epic, direction.upper())

        # ── ATR calculations ─────────────────────────────────────────────────
        h1c  = h1[:-1]    # exclude potentially-open current candle
        m15c = m15[:-1]

        atr_h1_list  = _atr(h1c,  cfg.atr_period)
        atr_m15_list = _atr(m15c, cfg.atr_period)
        atr_h1  = atr_h1_list[-1]  if atr_h1_list  and not math.isnan(atr_h1_list[-1])  else 0.0
        atr_m15 = atr_m15_list[-1] if atr_m15_list and not math.isnan(atr_m15_list[-1]) else 0.0

        # ── Gate 2: Sweep + BOS on H1 and/or M15 ────────────────────────────
        h1_result  = _sweep_and_bos(h1c,  direction, atr_h1,  cfg) if atr_h1  > 0 else None
        m15_result = _sweep_and_bos(m15c, direction, atr_m15, cfg) if atr_m15 > 0 else None

        h1_ok  = h1_result  is not None
        m15_ok = m15_result is not None

        if not h1_ok and not m15_ok:
            logger.info("[%s] gate2 SKIP: no sweep+BOS on H1 or M15", self.epic)
            return None

        tf_tags = "+".join(t for t, v in [("H1", h1_ok), ("M15", m15_ok)] if v)
        logger.info("[%s] gate2 PASS: sweep+BOS confirmed [%s]", self.epic, tf_tags)

        # ── Entry and SL from best confirming TF (prefer M15 — tighter) ─────
        if m15_ok:
            sweep_extreme, entry = m15_result
            sl_atr = atr_m15
        else:
            sweep_extreme, entry = h1_result
            sl_atr = atr_h1

        # ── Trade plan (per-instrument calibrated) ───────────────────────────
        if direction == "buy":
            sl  = sweep_extreme - cfg.sl_atr_mult      * sl_atr
            tp1 = entry         + cfg.tp1_atr_mult_m15 * atr_m15 if atr_m15 > 0 else None
            tp2 = entry         + cfg.tp2_atr_mult_h1  * atr_h1  if atr_h1  > 0 else None
        else:
            sl  = sweep_extreme + cfg.sl_atr_mult      * sl_atr
            tp1 = entry         - cfg.tp1_atr_mult_m15 * atr_m15 if atr_m15 > 0 else None
            tp2 = entry         - cfg.tp2_atr_mult_h1  * atr_h1  if atr_h1  > 0 else None

        sl_dist = abs(entry - sl)
        if sl_dist == 0:
            return None

        # R:R check — prefer TP1, fall back to TP2
        tp_use: Optional[float] = None
        if tp1 is not None and abs(tp1 - entry) / sl_dist >= cfg.min_rr:
            tp_use = tp1
        if tp_use is None and tp2 is not None and abs(tp2 - entry) / sl_dist >= cfg.min_rr:
            tp_use = tp2
        if tp_use is None:
            logger.info("[%s] RR SKIP: TP1=%s TP2=%s neither meets %.1f",
                        self.epic,
                        f"{tp1:.2f}" if tp1 else "—",
                        f"{tp2:.2f}" if tp2 else "—",
                        cfg.min_rr)
            return None

        rr = abs(tp_use - entry) / sl_dist
        logger.info("[%s] ✓ SIGNAL %s [%s] entry=%.2f sl=%.2f tp1=%s tp2=%s rr=%.2f",
                    self.epic, direction.upper(), tf_tags, entry, sl,
                    f"{tp1:.2f}" if tp1 else "—",
                    f"{tp2:.2f}" if tp2 else "—",
                    rr)

        # Encode H1/M15 confirmation flags for alert formatter
        comment = f"h1={'1' if h1_ok else '0'};m15={'1' if m15_ok else '0'}"

        return Signal(
            direction=direction,
            lots=cfg.lots,
            confirmed=True,
            entry=entry,
            stop_loss=sl,
            take_profit=tp1,
            take_profit2=tp2,
            comment=comment,
            timestamp=now_utc.isoformat(),
        )
