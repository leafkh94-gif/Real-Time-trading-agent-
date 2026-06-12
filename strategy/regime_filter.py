"""
Classifies the current market regime from H4 candles.
Used as the outermost gate — volatile regimes are skipped entirely.
"""
import math
from typing import Sequence

from strategy.base import Candle, MarketRegime
from strategy.indicators import atr, ema


class RegimeFilter:
    def __init__(
        self,
        atr_period: int = 14,
        ema_fast: int = 20,
        ema_slow: int = 50,
        volatile_atr_pct: float = 0.018,
    ):
        """
        volatile_atr_pct: if ATR / close > this fraction → VOLATILE (avoid trading).
        ema_fast / ema_slow: short-term vs. long-term EMA cross for direction.
        """
        self.atr_period = atr_period
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.volatile_atr_pct = volatile_atr_pct

    @property
    def min_candles(self) -> int:
        return self.ema_slow + self.atr_period + 1

    def classify(self, candles: Sequence[Candle]) -> MarketRegime:
        if len(candles) < self.min_candles:
            return MarketRegime.RANGING  # not enough data — conservative default

        closes = [c.close for c in candles]
        atr_vals = atr(candles, self.atr_period)
        ema_fast_vals = ema(closes, self.ema_fast)
        ema_slow_vals = ema(closes, self.ema_slow)

        last_close = closes[-1]
        last_atr = atr_vals[-1]

        # Volatility gate
        if not math.isnan(last_atr) and last_atr / last_close > self.volatile_atr_pct:
            return MarketRegime.VOLATILE

        last_fast = ema_fast_vals[-1]
        last_slow = ema_slow_vals[-1]
        prev_fast = ema_fast_vals[-2] if len(ema_fast_vals) >= 2 else last_fast
        prev_slow = ema_slow_vals[-2] if len(ema_slow_vals) >= 2 else last_slow

        if math.isnan(last_fast) or math.isnan(last_slow):
            return MarketRegime.RANGING

        fast_above_slow = last_fast > last_slow
        slow_rising = last_slow > prev_slow

        if fast_above_slow and slow_rising:
            return MarketRegime.TRENDING_UP
        if not fast_above_slow and not slow_rising:
            return MarketRegime.TRENDING_DOWN
        return MarketRegime.RANGING
