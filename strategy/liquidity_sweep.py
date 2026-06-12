"""
Smart-money liquidity sweep detector.

A bearish sweep: price briefly pierces a recent swing low (stop hunt), then
the bar closes back above that low → institutional buyers absorbed the sells
→ signal is 'buy'.

A bullish sweep: price briefly pierces a recent swing high, closes back below
→ signal is 'sell'.

This is the original simple version (no BOS confirmation required) — a single
candle that wicks through a swing level and closes back inside is sufficient.
"""
import logging
from typing import Optional, Sequence

from strategy.base import Candle
from strategy.indicators import swing_highs, swing_lows

logger = logging.getLogger(__name__)


class LiquiditySweepDetector:
    def __init__(self, lookback: int = 20, sweep_lookback: int = 3):
        """
        lookback:       number of recent candles to search for the swing pivot level.
        sweep_lookback: pivot detection window (bars each side of the pivot).
                        3 = meaningful pivot, not too strict, not too loose.
        """
        self.lookback = lookback
        self.sweep_lookback = sweep_lookback

    @property
    def min_candles(self) -> int:
        return self.lookback + self.sweep_lookback * 2 + 2

    def detect(self, candles: Sequence[Candle]) -> Optional[str]:
        """
        Examines the last 2 completed candles for a liquidity sweep pattern.
        Returns 'buy', 'sell', or None.
        """
        if len(candles) < self.min_candles:
            logger.info("sweep diag: not enough candles (%d < %d)", len(candles), self.min_candles)
            return None

        window = list(candles[-(self.lookback + self.sweep_lookback * 2 + 1):])

        sh = swing_highs(window, self.sweep_lookback)
        sl = swing_lows(window, self.sweep_lookback)

        # Pivot levels — exclude the last 2 bars (the sweep candidates)
        recent_highs = [v for v in sh[:-2] if v is not None]
        recent_lows  = [v for v in sl[:-2] if v is not None]

        logger.info(
            "sweep diag: window=%d bars | swing_highs=%d swing_lows=%d",
            len(window), len(recent_highs), len(recent_lows),
        )

        # Check each of the last 2 bars as a potential sweep candle
        for bar in list(candles)[-2:]:
            # Bearish sweep of a swing low → buy signal
            if recent_lows:
                nearest_low = min(recent_lows, key=lambda lv: abs(lv - bar.close))
                if bar.low < nearest_low and bar.close > nearest_low:
                    logger.info("sweep diag: BUY sweep — wick below %.4f, close %.4f", nearest_low, bar.close)
                    return "buy"

            # Bullish sweep of a swing high → sell signal
            if recent_highs:
                nearest_high = min(recent_highs, key=lambda hv: abs(hv - bar.close))
                if bar.high > nearest_high and bar.close < nearest_high:
                    logger.info("sweep diag: SELL sweep — wick above %.4f, close %.4f", nearest_high, bar.close)
                    return "sell"

        logger.info("sweep diag: no sweep in last 2 bars")
        return None
