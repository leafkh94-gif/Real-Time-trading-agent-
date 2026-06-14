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
_BOS_WINDOW = 30  # H1 bars to search for swing levels when checking BOS
_SWING_LOOKBACK = 5  # bars on each side for pivot detection
_RSI_PERIOD = 14  # RSI period for Gate 6
_MIN_RR_RATIO = 1.5  # Minimum Risk:Reward ratio for signal approval
_SIGNAL_LOCK_MINUTES = 60  # Minutes to lock same market after signal
_SCAN_INTERVAL_MINUTES = 15  # Bot scans markets every 15 minutes

# Volatility thresholds (Gate 2)
_VOLATILE_ATR_PCT = 0.018  # If ATR/Close > 1.8% → VOLATILE

# EMA Slope threshold (Gate 1)
_SLOPE_THRESHOLD_PCT = 0.05  # ±0.05% slope threshold for uptrend/downtrend

# Session times (UTC)
_LONDON_START = (8, 0)  # 08:00 UTC
_LONDON_END = (12, 0)  # 12:00 UTC
_LONDON_NY_OVERLAP_START = (13, 0)  # 13:00 UTC
_LONDON_NY_OVERLAP_END = (17, 0)  # 17:00 UTC
_NY_EARLY_START = (13, 30)  # 13:30 UTC
_NY_EARLY_END = (15, 30)  # 15:30 UTC

# TP levels multiplier (Gate 4)
_TP2_ATR_MULTIPLIER = 2.5


def rsi(prices: Sequence[float], period: int = 14) -> list[float]:
    """
    Calculate Relative Strength Index.
    Returns list same length as prices with NaN for initial period.
    """
    if len(prices) < period:
        return [float("nan")] * len(prices)

    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]

    seed_gains = sum(delta for delta in deltas[:period] if delta > 0) / period
    seed_losses = abs(sum(delta for delta in deltas[:period] if delta < 0)) / period

    gains = [seed_gains]
    losses = [seed_losses]

    for delta in deltas[period:]:
        gain = delta if delta > 0 else 0
        loss = abs(delta) if delta < 0 else 0
        gains.append((gains[-1] * (period - 1) + gain) / period)
        losses.append((losses[-1] * (period - 1) + loss) / period)

    result = [float("nan")] * (period)
    for i, (g, l) in enumerate(zip(gains, losses)):
        if l == 0:
            result.append(100.0 if g > 0 else 0.0)
        else:
            rs = g / l
            result.append(100.0 - (100.0 / (1.0 + rs)))

    return result


class GateResult:
    """Result of a single gate evaluation."""

    def __init__(self, passed: bool, reason: str = ""):
        self.passed = passed
        self.reason = reason


class SmartTradingBotStrategy(StrategyBase):
    """
    استراتيجية البوت الذكي للتداول — Smart Trading Bot Strategy

    6-gate alert engine for US100, US500, US30:
    1. Gate 1: Trend Filter (H4 EMA + Slope)
    2. Gate 2: Volatility Filter (H4 ATR)
    3. Gate 3: Liquidity Sweep (H1)
    4. Gate 4: BOS Confirmation (H1)
    5. Gate 5: Session Filter (UTC hours)
    6. Gate 6: RSI Momentum Filter (H1 RSI > or < 50)

    Plus additional checks:
    - Entry/SL/TP calculation
    - Risk:Reward ratio validation (min 1.5)
    - Signal lock (60 min per market)
    """

    def __init__(
        self,
        epic: str = "GOLD",  # Market identifier
        lots: float = 0.05,
        regime_filter: RegimeFilter | None = None,
        sweep_detector: LiquiditySweepDetector | None = None,
    ):
        self.epic = epic
        self.lots = lots
        self.regime_filter = regime_filter or RegimeFilter()
        self.sweep_detector = sweep_detector or LiquiditySweepDetector()
        
        # Signal tracking for 60-min lock
        self.last_signal_time: dict[str, datetime] = {}

    @property
    def name(self) -> str:
        return f"SmartTradingBot_{self.epic}"

    def evaluate(self, candles: MultiTimeframeCandles) -> Optional[Signal]:
        """
        Complete 6-gate evaluation pipeline.
        Returns Signal (with entry/sl/tp) if all gates pass, None otherwise.
        """
        h4 = candles.get(TF_H4, [])
        h1 = candles.get(TF_H1, [])

        logger.info(f"[{self.epic}] evaluate: {len(h4)} H4 candles, {len(h1)} H1 candles")

        # Gate 5: Session Filter (check first, fail fast)
        now_utc = datetime.now(tz=ZoneInfo("UTC"))
        if not self._gate5_session_filter(now_utc):
            logger.info(f"[{self.epic}] gate5 SKIP: outside active session")
            return None

        # Check 60-minute signal lock for this market
        if not self._check_signal_lock(now_utc):
            logger.info(f"[{self.epic}] SKIP: signal lock active")
            return None

        # Gate 1: Trend Filter (H4 EMA + Slope)
        gate1 = self._gate1_trend_filter(h4)
        if not gate1.passed:
            logger.info(f"[{self.epic}] gate1 SKIP: {gate1.reason}")
            return None
        allowed_direction = "buy" if gate1.reason == "uptrend" else "sell"
        logger.info(f"[{self.epic}] gate1 PASS: {allowed_direction}")

        # Gate 2: Volatility Filter (H4 ATR)
        gate2 = self._gate2_volatility_filter(h4)
        if not gate2.passed:
            logger.info(f"[{self.epic}] gate2 SKIP: {gate2.reason}")
            return None
        atr_value = gate2.reason  # Store ATR for later use
        logger.info(f"[{self.epic}] gate2 PASS: ATR={atr_value:.5f}")

        # Gate 3: Liquidity Sweep (H1)
        gate3 = self._gate3_liquidity_sweep(h1, allowed_direction)
        if not gate3.passed:
            logger.info(f"[{self.epic}] gate3 SKIP: {gate3.reason}")
            return None
        sweep_info = gate3.reason  # Dict with sweep details
        logger.info(f"[{self.epic}] gate3 PASS: {allowed_direction} sweep")

        # Gate 4: BOS Confirmation (H1)
        gate4 = self._gate4_bos_confirmation(h1, allowed_direction, sweep_info)
        if not gate4.passed:
            logger.info(f"[{self.epic}] gate4 SKIP: {gate4.reason}")
            return None
        bos_info = gate4.reason
        logger.info(f"[{self.epic}] gate4 PASS: BOS confirmed")

        # Gate 6: RSI Momentum Filter (H1)
        gate6 = self._gate6_rsi_filter(h1, allowed_direction)
        if not gate6.passed:
            logger.info(f"[{self.epic}] gate6 SKIP: {gate6.reason}")
            return None
        logger.info(f"[{self.epic}] gate6 PASS: RSI confirmed")

        # Calculate Entry / SL / TP1 / TP2
        entry_price = bos_info["entry"]
        sl_price = self._calculate_sl(allowed_direction, sweep_info, float(atr_value))
        tp1_price = self._calculate_tp1(h1, allowed_direction)
        tp2_price = self._calculate_tp2(allowed_direction, entry_price, float(atr_value))

        # Risk:Reward validation
        rr_check = self._validate_risk_reward(
            allowed_direction, entry_price, sl_price, tp1_price, tp2_price
        )
        if not rr_check.passed:
            logger.info(f"[{self.epic}] RR SKIP: {rr_check.reason}")
            return None

        final_tp = rr_check.reason  # TP price after RR validation
        logger.info(f"[{self.epic}] RR PASS: {final_tp}")

        # All gates passed — create signal and activate lock
        signal = Signal(
            direction=allowed_direction,
            lots=self.lots,
            confirmed=True,
            entry=entry_price,
            stop_loss=sl_price,
            take_profit=final_tp,
            timestamp=now_utc.isoformat(),
        )
        self.last_signal_time[self.epic] = now_utc
        logger.info(
            f"[{self.epic}] ✓ SIGNAL: {allowed_direction.upper()} "
            f"@ {entry_price:.5f} | SL {sl_price:.5f} | TP {final_tp:.5f}"
        )
        return signal

    # ─────────────────────────────────────────────────────────────────────────
    # GATE 1: Trend Filter (H4 EMA 20/50 + Slope)
    # ─────────────────────────────────────────────────────────────────────────

    def _gate1_trend_filter(self, h4: list) -> GateResult:
        """
        Gate 1: فلتر الاتجاه العام — Trend Filter
        Uses H4 EMA20, EMA50, and slope calculation.

        Uptrend: EMA20 > EMA50 AND slope > +0.05%
        Downtrend: EMA20 < EMA50 AND slope < -0.05%
        Ranging: else → reject
        """
        if len(h4) < self.regime_filter.min_candles:
            return GateResult(False, f"not enough H4 candles ({len(h4)})")

        closes = [c.close for c in h4]
        ema20 = ema(closes, 20)
        ema50 = ema(closes, 50)

        if not ema20 or not ema50 or math.isnan(ema20[-1]) or math.isnan(ema50[-1]):
            return GateResult(False, "EMA calculation failed")

        last_ema20 = ema20[-1]
        last_ema50 = ema50[-1]

        # Calculate slope: (EMA50[0] - EMA50[3]) / EMA50[0] × 100
        if len(ema50) < 4:
            return GateResult(False, "not enough EMA50 history for slope")

        slope = ema50[-1] - ema50[-4]  # Current minus 3 bars ago
        slope_pct = (slope / ema50[-1]) * 100

        if last_ema20 > last_ema50 and slope_pct > _SLOPE_THRESHOLD_PCT:
            return GateResult(True, "uptrend")
        elif last_ema20 < last_ema50 and slope_pct < -_SLOPE_THRESHOLD_PCT:
            return GateResult(True, "downtrend")
        else:
            return GateResult(False, f"ranging (EMA20={last_ema20:.5f}, EMA50={last_ema50:.5f}, slope%={slope_pct:.4f})")

    # ─────────────────────────────────────────────────────────────────────────
    # GATE 2: Volatility Filter (H4 ATR)
    # ─────────────────────────────────────────────────────────────────────────

    def _gate2_volatility_filter(self, h4: list) -> GateResult:
        """
        Gate 2: فلتر التقلبات — Volatility Filter
        
        ATR14 / Close > 1.8% → VOLATILE → reject
        else → pass and return ATR value for later use
        """
        if len(h4) < 14:
            return GateResult(False, "not enough H4 candles for ATR")

        atr_vals = atr(h4, 14)
        if not atr_vals or math.isnan(atr_vals[-1]):
            return GateResult(False, "ATR calculation failed")

        last_atr = atr_vals[-1]
        last_close = h4[-1].close
        volatility_pct = (last_atr / last_close) * 100

        if volatility_pct > _VOLATILE_ATR_PCT * 100:  # 1.8%
            return GateResult(False, f"VOLATILE (ATR%={volatility_pct:.4f})")
        
        return GateResult(True, str(last_atr))  # Pass and return ATR for later

    # ─────────────────────────────────────────────────────────────────────────
    # GATE 3: Liquidity Sweep (H1)
    # ─────────────────────────────────────────────────────────────────────────

    def _gate3_liquidity_sweep(self, h1: list, allowed_direction: str) -> GateResult:
        """
        Gate 3: كاشف اصطياد السيولة — Liquidity Sweep Detector

        Bullish sweep (BUY): Low < Swing Low, Close > Swing Low
        Bearish sweep (SELL): High > Swing High, Close < Swing High
        
        Returns sweep details for Gate 4 use.
        """
        if len(h1) < self.sweep_detector.min_candles:
            return GateResult(False, f"not enough H1 candles ({len(h1)})")

        # Use the sweep detector
        direction = self.sweep_detector.detect(h1)
        if direction is None:
            return GateResult(False, "no sweep detected")

        if direction != allowed_direction:
            return GateResult(False, f"sweep direction {direction} != allowed {allowed_direction}")

        # Retrieve sweep details for later gates
        window = list(h1[-(self.sweep_detector.lookback + self.sweep_detector.sweep_lookback * 2 + 1):])
        sh = swing_highs(window, self.sweep_detector.sweep_lookback)
        sl = swing_lows(window, self.sweep_detector.sweep_lookback)

        recent_highs = [v for v in sh[:-2] if v is not None]
        recent_lows = [v for v in sl[:-2] if v is not None]

        sweep_info = {
            "direction": direction,
            "swing_high": recent_highs[-1] if recent_highs else None,
            "swing_low": recent_lows[-1] if recent_lows else None,
        }

        return GateResult(True, sweep_info)

    # ─────────────────────────────────────────────────────────────────────────
    # GATE 4: BOS Confirmation (H1)
    # ─────────────────────────────────────────────────────────────────────────

    def _gate4_bos_confirmation(
        self, h1: list, direction: str, sweep_info: dict
    ) -> GateResult:
        """
        Gate 4: تأكيد كسر الهيكل — BOS Confirmation
        
        BUY: Close > last Swing High from 20 H1 bars
        SELL: Close < last Swing Low from 20 H1 bars
        
        Returns entry price (BOS close) and other details.
        """
        if len(h1) < _BOS_WINDOW:
            return GateResult(False, f"not enough H1 bars for BOS ({len(h1)})")

        window = list(h1[-_BOS_WINDOW:])
        sh = swing_highs(window, lookback=_SWING_LOOKBACK)
        sl = swing_lows(window, lookback=_SWING_LOOKBACK)

        cur_close = h1[-1].close

        if direction == "buy":
            recent_highs = [v for v in sh[:-_SWING_LOOKBACK] if v is not None]
            if not recent_highs:
                return GateResult(False, "no swing high for BOS")
            if cur_close > recent_highs[-1]:
                return GateResult(
                    True,
                    {
                        "entry": cur_close,
                        "sweep_low": sweep_info.get("swing_low"),
                        "sweep_high": sweep_info.get("swing_high"),
                    },
                )
            else:
                return GateResult(False, f"close {cur_close:.5f} not > swing high {recent_highs[-1]:.5f}")
        else:  # sell
            recent_lows = [v for v in sl[:-_SWING_LOOKBACK] if v is not None]
            if not recent_lows:
                return GateResult(False, "no swing low for BOS")
            if cur_close < recent_lows[-1]:
                return GateResult(
                    True,
                    {
                        "entry": cur_close,
                        "sweep_low": sweep_info.get("swing_low"),
                        "sweep_high": sweep_info.get("swing_high"),
                    },
                )
            else:
                return GateResult(False, f"close {cur_close:.5f} not < swing low {recent_lows[-1]:.5f}")

    # ─────────────────────────────────────────────────────────────────────────
    # GATE 5: Session Filter (UTC hours)
    # ─────────────────────────────────────────────────────────────────────────

    def _gate5_session_filter(self, now_utc: datetime) -> bool:
        """
        Gate 5: فلتر الجلسات — Session Filter
        
        Allow signals only during:
        - London: 08:00-12:00 UTC
        - London/NY overlap: 13:00-17:00 UTC
        - NY early: 13:30-15:30 UTC (subset of overlap)
        """
        hour = now_utc.hour
        minute = now_utc.minute

        # London session
        if _LONDON_START[0] <= hour < _LONDON_END[0]:
            return True

        # London/NY overlap
        if _LONDON_NY_OVERLAP_START[0] <= hour < _LONDON_NY_OVERLAP_END[0]:
            return True

        # NY early (13:30-15:30)
        if hour == _NY_EARLY_START[0] and minute >= _NY_EARLY_START[1]:
            return True
        if hour == _NY_EARLY_END[0] and minute < _NY_EARLY_END[1]:
            return True
        if _NY_EARLY_START[0] < hour < _NY_EARLY_END[0]:
            return True

        return False

    # ─────────────────────────────────────────────────────────────────────────
    # GATE 6: RSI Momentum Filter (H1)
    # ─────────────────────────────────────────────────────────────────────────

    def _gate6_rsi_filter(self, h1: list, direction: str) -> GateResult:
        """
        Gate 6 ✦ جديدة — فلتر الزخم — RSI Momentum Filter
        
        BUY: RSI14_H1 > 50 (bullish momentum)
        SELL: RSI14_H1 < 50 (bearish momentum)
        """
        if len(h1) < _RSI_PERIOD + 1:
            return GateResult(False, f"not enough H1 candles for RSI ({len(h1)})")

        closes = [c.close for c in h1]
        rsi_vals = rsi(closes, _RSI_PERIOD)

        if not rsi_vals or math.isnan(rsi_vals[-1]):
            return GateResult(False, "RSI calculation failed")

        last_rsi = rsi_vals[-1]

        if direction == "buy" and last_rsi > 50:
            return GateResult(True, f"RSI={last_rsi:.2f} > 50")
        elif direction == "sell" and last_rsi < 50:
            return GateResult(True, f"RSI={last_rsi:.2f} < 50")
        else:
            return GateResult(
                False,
                f"RSI={last_rsi:.2f} contradicts {direction} (need {'> 50' if direction == 'buy' else '< 50'})"
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Entry / SL / TP Calculation
    # ─────────────────────────────────────────────────────────────────────────

    def _calculate_sl(self, direction: str, sweep_info: dict, atr: float) -> float:
        """
        حساب وقف الخسارة — Calculate Stop Loss
        
        BUY: SL = Sweep Low - (0.5 × ATR14)
        SELL: SL = Sweep High + (0.5 × ATR14)
        """
        if direction == "buy":
            sweep_low = sweep_info.get("sweep_low", 0)
            return sweep_low - (0.5 * atr)
        else:  # sell
            sweep_high = sweep_info.get("sweep_high", 0)
            return sweep_high + (0.5 * atr)

    def _calculate_tp1(self, h1: list, direction: str) -> float:
        """
        حساب الهدف الأول — Calculate First Take Profit
        
        BUY: nearest Swing High on H1
        SELL: nearest Swing Low on H1
        """
        window = list(h1[-_BOS_WINDOW:])
        sh = swing_highs(window, lookback=_SWING_LOOKBACK)
        sl = swing_lows(window, lookback=_SWING_LOOKBACK)

        if direction == "buy":
            recent_highs = [v for v in sh if v is not None]
            return recent_highs[-1] if recent_highs else h1[-1].close
        else:  # sell
            recent_lows = [v for v in sl if v is not None]
            return recent_lows[-1] if recent_lows else h1[-1].close

    def _calculate_tp2(self, direction: str, entry: float, atr: float) -> float:
        """
        حساب الهدف الثاني — Calculate Second Take Profit
        
        BUY: Entry + (2.5 × ATR14)
        SELL: Entry - (2.5 × ATR14)
        """
        if direction == "buy":
            return entry + (_TP2_ATR_MULTIPLIER * atr)
        else:  # sell
            return entry - (_TP2_ATR_MULTIPLIER * atr)

    # ─────────────────────────────────────────────────────────────────────────
    # Risk:Reward Validation
    # ─────────────────────────────────────────────────────────────────────────

    def _validate_risk_reward(
        self, direction: str, entry: float, sl: float, tp1: float, tp2: float
    ) -> GateResult:
        """
        فحص R:R ✦ جديد — Risk:Reward Validation
        
        R:R = distance(TP) / distance(SL)
        
        If R:R >= 1.5 → use TP1
        Else if TP2 R:R >= 1.5 → use TP2
        Else → reject
        """
        if direction == "buy":
            sl_distance = entry - sl
            tp1_distance = tp1 - entry
            tp2_distance = tp2 - entry
        else:  # sell
            sl_distance = sl - entry
            tp1_distance = entry - tp1
            tp2_distance = entry - tp2

        if sl_distance <= 0:
            return GateResult(False, "invalid SL (not below/above entry)")

        rr_tp1 = tp1_distance / sl_distance if sl_distance > 0 else 0
        rr_tp2 = tp2_distance / sl_distance if sl_distance > 0 else 0

        if rr_tp1 >= _MIN_RR_RATIO:
            return GateResult(True, tp1)
        elif rr_tp2 >= _MIN_RR_RATIO:
            return GateResult(True, tp2)
        else:
            return GateResult(False, f"R:R={rr_tp1:.2f} < {_MIN_RR_RATIO}")

    # ─────────────────────────────────────────────────────────────────────────
    # Signal Lock Management (60 minutes per market)
    # ─────────────────────────────────────────────────────────────────────────

    def _check_signal_lock(self, now_utc: datetime) -> bool:
        """
        Check if 60 minutes have passed since last signal for this market.
        Returns True if allowed (no lock or lock expired), False if locked.
        """
        if self.epic not in self.last_signal_time:
            return True

        last_time = self.last_signal_time[self.epic]
        elapsed_minutes = (now_utc - last_time).total_seconds() / 60

        if elapsed_minutes >= _SIGNAL_LOCK_MINUTES:
            return True
        else:
            return False
