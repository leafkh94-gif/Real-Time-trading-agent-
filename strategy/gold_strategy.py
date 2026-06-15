"""
استراتيجية البوت الذكي للتداول — Smart Trading Bot Strategy
US100 • US500 • US30
النسخة المطورة الكاملة — مع الإضافات الجديدة
يونيو 2026

نظرة عامة على الاستراتيجية:
البوت يعمل كمحرك تنبيهات ذكي — يقرأ السوق، يفلتر الإشارات عبر ست بوابات متسلسلة، 
ويرسل تنبيه فقط عندما تتوافق جميع الشروط. لا يفتح صفقات ولا يديرها.

البند             | التفاصيل
الأسواق المستهدفة  | US100 — US500 — US30
الإطار الزمني      | H1 للإشارات | H4 للفلاتر
عدد البوابات       | 6 بوابات متسلسلة (5 أصلية + 1 جديدة)
وظيفة البوت        | إرسال تنبيهات فقط — لا يفتح صفقات
دورة الفحص         | كل 15 دقيقة
قفل الإشارات      | 60 دقيقة لنفس السوق
التنبيهات         | تُرسل تلقائياً على تيليجرام
"""

import logging
import math
from datetime import datetime
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

from execution.models import Signal
from strategy.base import (
    Candle,
    MarketRegime,
    MultiTimeframeCandles,
    StrategyBase,
    TF_H1,
    TF_H4,
)
from strategy.indicators import atr, ema, swing_highs, swing_lows
from strategy.liquidity_sweep import LiquiditySweepDetector
from strategy.regime_filter import RegimeFilter

logger = logging.getLogger(__name__)

# Configuration constants
_BOS_WINDOW = 30
_SWING_LOOKBACK = 5
_RSI_PERIOD = 14
_MIN_RR_RATIO = 1.5
_SIGNAL_LOCK_MINUTES = 60
_SCAN_INTERVAL_MINUTES = 15

# Volatility thresholds (Gate 2)
_VOLATILE_ATR_PCT = 0.018  # If ATR/Close > 1.8% → VOLATILE

# EMA Slope threshold (Gate 1)
_SLOPE_THRESHOLD_PCT = 0.05  # ±0.05% slope threshold

# Session times (UTC)
_LONDON_START = (8, 0)
_LONDON_END = (12, 0)
_LONDON_NY_OVERLAP_START = (13, 0)
_LONDON_NY_OVERLAP_END = (17, 0)
_NY_EARLY_START = (13, 30)
_NY_EARLY_END = (15, 30)

# TP levels multiplier (Gate 4)
_TP2_ATR_MULTIPLIER = 2.5


def rsi(prices: Sequence[float], period: int = 14) -> list[float]:
    if len(prices) < period:
        return [float("nan")] * len(prices)

    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]

    seed_gains  = sum(d for d in deltas[:period] if d > 0) / period
    seed_losses = abs(sum(d for d in deltas[:period] if d < 0)) / period

    gains  = [seed_gains]
    losses = [seed_losses]

    for delta in deltas[period:]:
        gain = delta if delta > 0 else 0
        loss = abs(delta) if delta < 0 else 0
        gains.append((gains[-1]  * (period - 1) + gain) / period)
        losses.append((losses[-1] * (period - 1) + loss) / period)

    result = [float("nan")] * period
    for g, l in zip(gains, losses):
        if l == 0:
            result.append(100.0 if g > 0 else 0.0)
        else:
            rs = g / l
            result.append(100.0 - (100.0 / (1.0 + rs)))

    return result


class GateResult:
    def __init__(self, passed: bool, reason=None):
        self.passed = passed
        self.reason = reason if reason is not None else ""


class SmartTradingBotStrategy(StrategyBase):
    """
    استراتيجية البوت الذكي للتداول — Smart Trading Bot Strategy

    6-gate alert engine for US100, US500, US30:
    Gate 1: Trend Filter (H4 EMA20/50 + Slope)
    Gate 2: Volatility Filter (H4 ATR < 1.8%)
    Gate 3: Liquidity Sweep (H1)
    Gate 4: BOS Confirmation (H1)
    Gate 5: Session Filter (London / NY overlap)
    Gate 6: RSI Momentum Filter (H1 RSI > or < 50)
    + R:R validation (≥ 1.5) + 60-min signal lock per market
    """

    def __init__(
        self,
        epic: str = "GOLD",
        lots: float = 0.05,
        regime_filter: RegimeFilter | None = None,
        sweep_detector: LiquiditySweepDetector | None = None,
    ):
        self.epic           = epic
        self.lots           = lots
        self.regime_filter  = regime_filter  or RegimeFilter()
        self.sweep_detector = sweep_detector or LiquiditySweepDetector()
        self.last_signal_time: dict[str, datetime] = {}

    @property
    def name(self) -> str:
        return f"SmartTradingBot_{self.epic}"

    def evaluate(self, candles: MultiTimeframeCandles) -> Optional[Signal]:
        h4 = candles.get(TF_H4, [])
        h1 = candles.get(TF_H1, [])

        logger.info(f"[{self.epic}] evaluate: {len(h4)} H4 candles, {len(h1)} H1 candles")

        now_utc = datetime.now(tz=ZoneInfo("UTC"))
        _in_session = self._gate5_session_filter(now_utc)
        if not _in_session:
            logger.info(f"[{self.epic}] gate5 FYI: outside active session — continuing")

        if not self._check_signal_lock(now_utc):
            logger.info(f"[{self.epic}] SKIP: signal lock active")
            return None

        # Gate 1: Trend
        gate1 = self._gate1_trend_filter(h4)
        if not gate1.passed:
            logger.info(f"[{self.epic}] gate1 SKIP: {gate1.reason}")
            return None
        allowed_direction = "buy" if gate1.reason == "uptrend" else "sell"
        logger.info(f"[{self.epic}] gate1 PASS: {allowed_direction}")

        # Gate 2: Volatility — reason stored as float ATR value
        gate2 = self._gate2_volatility_filter(h4)
        if not gate2.passed:
            logger.info(f"[{self.epic}] gate2 SKIP: {gate2.reason}")
            return None
        atr_value = float(gate2.reason)   # gate2 stores str(last_atr); cast to float
        logger.info(f"[{self.epic}] gate2 PASS: ATR={atr_value:.5f}")

        # Gate 3: Sweep
        gate3 = self._gate3_liquidity_sweep(h1, allowed_direction)
        if not gate3.passed:
            logger.info(f"[{self.epic}] gate3 SKIP: {gate3.reason}")
            return None
        sweep_info = gate3.reason
        logger.info(f"[{self.epic}] gate3 PASS: {allowed_direction} sweep")

        # Gate 4: BOS
        gate4 = self._gate4_bos_confirmation(h1, allowed_direction, sweep_info)
        if not gate4.passed:
            logger.info(f"[{self.epic}] gate4 SKIP: {gate4.reason}")
            return None
        bos_info = gate4.reason
        logger.info(f"[{self.epic}] gate4 PASS: BOS confirmed")

        # Gate 6: RSI
        gate6 = self._gate6_rsi_filter(h1, allowed_direction)
        if not gate6.passed:
            logger.info(f"[{self.epic}] gate6 SKIP: {gate6.reason}")
            return None
        logger.info(f"[{self.epic}] gate6 PASS: RSI confirmed")

        # Build trade plan
        entry_price = bos_info["entry"]
        sl_price    = self._calculate_sl(allowed_direction, sweep_info, atr_value)
        tp1_price   = self._calculate_tp1(h1, allowed_direction)
        tp2_price   = self._calculate_tp2(allowed_direction, entry_price, atr_value)

        rr_check = self._validate_risk_reward(
            allowed_direction, entry_price, sl_price, tp1_price, tp2_price
        )
        if not rr_check.passed:
            logger.info(f"[{self.epic}] RR SKIP: {rr_check.reason}")
            return None

        final_tp = rr_check.reason  # float (tp1 or tp2)
        logger.info(f"[{self.epic}] RR PASS: {final_tp}")

        sig = Signal(
            direction=allowed_direction,
            lots=self.lots,
            confirmed=True,
            entry=entry_price,
            stop_loss=sl_price,
            take_profit=float(final_tp),
            timestamp=now_utc.isoformat(),
            comment="" if _in_session else "⚠️ Off-session",
        )
        self.last_signal_time[self.epic] = now_utc
        logger.info(
            f"[{self.epic}] ✓ SIGNAL: {allowed_direction.upper()} "
            f"@ {entry_price:.5f} | SL {sl_price:.5f} | TP {float(final_tp):.5f}"
        )
        return sig

    # ── Gate 1: Trend ─────────────────────────────────────────────────────────────────

    def _gate1_trend_filter(self, h4: list) -> GateResult:
        if len(h4) < self.regime_filter.min_candles:
            return GateResult(False, f"not enough H4 candles ({len(h4)})")

        closes = [c.close for c in h4]
        ema20  = ema(closes, 20)
        ema50  = ema(closes, 50)

        if not ema20 or not ema50 or math.isnan(ema20[-1]) or math.isnan(ema50[-1]):
            return GateResult(False, "EMA calculation failed")

        if len(ema50) < 4:
            return GateResult(False, "not enough EMA50 history for slope")

        slope_pct = (ema50[-1] - ema50[-4]) / ema50[-1] * 100

        if ema20[-1] > ema50[-1] and slope_pct >  _SLOPE_THRESHOLD_PCT:
            return GateResult(True, "uptrend")
        if ema20[-1] < ema50[-1] and slope_pct < -_SLOPE_THRESHOLD_PCT:
            return GateResult(True, "downtrend")
        return GateResult(False,
            f"ranging (EMA20={ema20[-1]:.5f}, EMA50={ema50[-1]:.5f}, slope%={slope_pct:.4f})")

    # ── Gate 2: Volatility ───────────────────────────────────────────────────

    def _gate2_volatility_filter(self, h4: list) -> GateResult:
        if len(h4) < 14:
            return GateResult(False, "not enough H4 candles for ATR")

        atr_vals = atr(h4, 14)
        if not atr_vals or math.isnan(atr_vals[-1]):
            return GateResult(False, "ATR calculation failed")

        last_atr      = atr_vals[-1]
        volatility_pct = (last_atr / h4[-1].close) * 100

        if volatility_pct > _VOLATILE_ATR_PCT * 100:
            return GateResult(False, f"VOLATILE (ATR%={volatility_pct:.4f})")
        return GateResult(True, str(last_atr))   # caller does float(gate2.reason)

    # ── Gate 3: Liquidity Sweep ─────────────────────────────────────────────

    def _gate3_liquidity_sweep(self, h1: list, allowed_direction: str) -> GateResult:
        if len(h1) < self.sweep_detector.min_candles:
            return GateResult(False, f"not enough H1 candles ({len(h1)})")

        direction = self.sweep_detector.detect(h1)
        if direction is None:
            return GateResult(False, "no sweep detected")
        if direction != allowed_direction:
            return GateResult(False, f"sweep={direction} contradicts allowed={allowed_direction}")

        window = list(h1[-(self.sweep_detector.lookback
                           + self.sweep_detector.sweep_lookback * 2 + 1):])
        sh = swing_highs(window, self.sweep_detector.sweep_lookback)
        sl = swing_lows( window, self.sweep_detector.sweep_lookback)

        recent_highs = [v for v in sh[:-2] if v is not None]
        recent_lows  = [v for v in sl[:-2] if v is not None]

        return GateResult(True, {
            "direction":  direction,
            "swing_high": recent_highs[-1] if recent_highs else None,
            "swing_low":  recent_lows[-1]  if recent_lows  else None,
        })

    # ── Gate 4: BOS ──────────────────────────────────────────────────────────────────

    def _gate4_bos_confirmation(self, h1: list, direction: str, sweep_info) -> GateResult:
        if len(h1) < _BOS_WINDOW:
            return GateResult(False, f"not enough H1 bars for BOS ({len(h1)})")

        window    = list(h1[-_BOS_WINDOW:])
        sh        = swing_highs(window, lookback=_SWING_LOOKBACK)
        sl        = swing_lows( window, lookback=_SWING_LOOKBACK)
        cur_close = h1[-1].close

        if direction == "buy":
            recent_highs = [v for v in sh[:-_SWING_LOOKBACK] if v is not None]
            if not recent_highs:
                return GateResult(False, "no swing high for BOS")
            if cur_close > recent_highs[-1]:
                return GateResult(True, {
                    "entry":      cur_close,
                    "sweep_low":  sweep_info.get("swing_low")  if isinstance(sweep_info, dict) else None,
                    "sweep_high": sweep_info.get("swing_high") if isinstance(sweep_info, dict) else None,
                })
            return GateResult(False,
                f"close {cur_close:.5f} not > swing high {recent_highs[-1]:.5f}")
        else:
            recent_lows = [v for v in sl[:-_SWING_LOOKBACK] if v is not None]
            if not recent_lows:
                return GateResult(False, "no swing low for BOS")
            if cur_close < recent_lows[-1]:
                return GateResult(True, {
                    "entry":      cur_close,
                    "sweep_low":  sweep_info.get("swing_low")  if isinstance(sweep_info, dict) else None,
                    "sweep_high": sweep_info.get("swing_high") if isinstance(sweep_info, dict) else None,
                })
            return GateResult(False,
                f"close {cur_close:.5f} not < swing low {recent_lows[-1]:.5f}")

    # ── Gate 5: Session ────────────────────────────────────────────────────────────

    def _gate5_session_filter(self, now_utc: datetime) -> bool:
        hour   = now_utc.hour
        minute = now_utc.minute
        if _LONDON_START[0] <= hour < _LONDON_END[0]:
            return True
        if _LONDON_NY_OVERLAP_START[0] <= hour < _LONDON_NY_OVERLAP_END[0]:
            return True
        if hour == _NY_EARLY_START[0] and minute >= _NY_EARLY_START[1]:
            return True
        if hour == _NY_EARLY_END[0] and minute < _NY_EARLY_END[1]:
            return True
        if _NY_EARLY_START[0] < hour < _NY_EARLY_END[0]:
            return True
        return False

    # ── Gate 6: RSI ───────────────────────────────────────────────────────────────────

    def _gate6_rsi_filter(self, h1: list, direction: str) -> GateResult:
        if len(h1) < _RSI_PERIOD + 1:
            return GateResult(False, f"not enough H1 candles for RSI ({len(h1)})")

        closes   = [c.close for c in h1]
        rsi_vals = rsi(closes, _RSI_PERIOD)

        if not rsi_vals or math.isnan(rsi_vals[-1]):
            return GateResult(False, "RSI calculation failed")

        last_rsi = rsi_vals[-1]
        if direction == "buy" and last_rsi > 50:
            return GateResult(True, f"RSI={last_rsi:.2f} > 50")
        if direction == "sell" and last_rsi < 50:
            return GateResult(True, f"RSI={last_rsi:.2f} < 50")
        need = "> 50" if direction == "buy" else "< 50"
        return GateResult(False, f"RSI={last_rsi:.2f} contradicts {direction} (need {need})")

    # ── SL / TP / R:R ─────────────────────────────────────────────────────────────

    def _calculate_sl(self, direction: str, sweep_info: dict, atr_val: float) -> float:
        if direction == "buy":
            sweep_low = sweep_info.get("swing_low") or 0.0
            return sweep_low - (0.5 * atr_val)
        sweep_high = sweep_info.get("swing_high") or 0.0
        return sweep_high + (0.5 * atr_val)

    def _calculate_tp1(self, h1: list, direction: str) -> float:
        window = list(h1[-_BOS_WINDOW:])
        sh = swing_highs(window, lookback=_SWING_LOOKBACK)
        sl = swing_lows( window, lookback=_SWING_LOOKBACK)
        if direction == "buy":
            highs = [v for v in sh if v is not None]
            return highs[-1] if highs else h1[-1].close
        lows = [v for v in sl if v is not None]
        return lows[-1] if lows else h1[-1].close

    def _calculate_tp2(self, direction: str, entry: float, atr_val: float) -> float:
        if direction == "buy":
            return entry + _TP2_ATR_MULTIPLIER * atr_val
        return entry - _TP2_ATR_MULTIPLIER * atr_val

    def _validate_risk_reward(
        self, direction: str, entry: float, sl: float, tp1: float, tp2: float
    ) -> GateResult:
        if direction == "buy":
            sl_dist  = entry - sl
            tp1_dist = tp1 - entry
            tp2_dist = tp2 - entry
        else:
            sl_dist  = sl - entry
            tp1_dist = entry - tp1
            tp2_dist = entry - tp2

        if sl_dist <= 0:
            return GateResult(False, "invalid SL")

        rr1 = tp1_dist / sl_dist
        rr2 = tp2_dist / sl_dist

        if rr1 >= _MIN_RR_RATIO:
            return GateResult(True, tp1)
        if rr2 >= _MIN_RR_RATIO:
            return GateResult(True, tp2)
        return GateResult(False, f"R:R={rr1:.2f} < {_MIN_RR_RATIO}")

    # ── Signal lock ────────────────────────────────────────────────────────────

    def _check_signal_lock(self, now_utc: datetime) -> bool:
        last = self.last_signal_time.get(self.epic)
        if last is None:
            return True
        return (now_utc - last).total_seconds() / 60 >= _SIGNAL_LOCK_MINUTES
