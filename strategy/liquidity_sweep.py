"""
Smart-money liquidity sweep detector.

A bearish sweep: price briefly pierces a recent swing low (stop hunt), then
the bar closes back above that low → institutional buyers absorbed the sells
→ signal is 'buy'.

A bullish sweep: price briefly pierces a recent swing high, closes back below
→ signal is 'sell'.
"""
from typing import Optional, Sequence

from strategy.base import Candle
from strategy.indicators import swing_highs, swing_lows


class LiquiditySweepDetector:
    def __init__(self, lookback: int = 20, sweep_lookback: int = 5):
        """
        lookback:       number of recent candles to search for the swing pivot level.
        sweep_lookback: pivot detection window (bars each side of the pivot).
        """
        self.lookback = lookback
        self.sweep_lookback = sweep_lookback

    @property
    def min_candles(self) -> int:
        return self.lookback + self.sweep_lookback * 2 + 2

    def detect(self, candles: Sequence[Candle]) -> Optional[str]:
        """
        Examines the most-recently completed candle (index -1).
        Returns 'buy', 'sell', or None.
        """
        if len(candles) < self.min_candles:
            return None

        last = candles[-1]
        window = list(candles[-(self.lookback + self.sweep_lookback * 2 + 1):])

        sh = swing_highs(window, self.sweep_lookback)
        sl = swing_lows(window, self.sweep_lookback)

        # Collect confirmed pivot levels (exclude the very last bar — it is the signal bar)
        recent_highs = [v for v in sh[:-1] if v is not None]
        recent_lows = [v for v in sl[:-1] if v is not None]

        # Bearish sweep of a swing low → buy signal
        if recent_lows:
            nearest_low = min(recent_lows, key=lambda lv: abs(lv - last.close))
            swept_below = last.low < nearest_low
            closed_above = last.close > nearest_low
            if swept_below and closed_above:
                return "buy"

        # Bullish sweep of a swing high → sell signal
        if recent_highs:
            nearest_high = min(recent_highs, key=lambda hv: abs(hv - last.close))
            swept_above = last.high > nearest_high
            closed_below = last.close < nearest_high
            if swept_above and closed_below:
                return "sell"

        return None
