"""
Momentum exhaustion detector.

Fires a counter-trend alert after an abnormally large rapid directional move:
  BIG UP move  → SELL  (price extended, potential exhaustion / short entry)
  BIG DOWN move → BUY  (price extended, potential exhaustion / long entry)

The required move is normalised to ATR so the threshold adapts to each
instrument's typical volatility — a 1000-pt US100 move and a $30 Gold spike
both register as roughly the same number of ATRs.

Typical threshold: 3× ATR over 3 H1 bars catches moves like +1000 US100
in 2 hours while ignoring normal intraday fluctuation.
"""
import logging
from typing import Optional, Sequence

from strategy.base import Candle
from strategy.indicators import atr as _atr

logger = logging.getLogger(__name__)


class MomentumDetector:
    def __init__(
        self,
        lookback: int = 3,
        atr_period: int = 14,
        move_atr_mult: float = 3.0,
    ):
        """
        lookback:       H1 bars to measure the directional move across.
        atr_period:     ATR period for normalising move size.
        move_atr_mult:  net move must exceed this many ATRs to fire.
                        3.0 ≈ 3× the average H1 range — clearly abnormal.
        """
        self.lookback = lookback
        self.atr_period = atr_period
        self.move_atr_mult = move_atr_mult

    @property
    def min_candles(self) -> int:
        return self.atr_period + self.lookback + 1

    def detect(self, candles: Sequence[Candle]) -> Optional[str]:
        """Returns 'buy', 'sell', or None."""
        if len(candles) < self.min_candles:
            logger.debug("momentum: not enough candles (%d < %d)", len(candles), self.min_candles)
            return None

        atr_vals = [v for v in _atr(candles, self.atr_period) if v == v]
        if not atr_vals:
            return None

        current_atr = atr_vals[-1]
        if current_atr == 0:
            return None

        threshold = self.move_atr_mult * current_atr

        # Net close-to-close move over the last `lookback` H1 bars
        start_close = candles[-(self.lookback + 1)].close
        end_close   = candles[-1].close
        net_move    = end_close - start_close
        move_in_atr = net_move / current_atr

        logger.info(
            "momentum diag: net_move=%.2f (%.1f ATRs) threshold=%.2f (%.1f ATRs) over %d bars",
            net_move, move_in_atr, threshold, self.move_atr_mult, self.lookback,
        )

        if net_move >= threshold:
            logger.info("momentum: BIG UP move (%.1f ATRs) → SELL signal", move_in_atr)
            return "sell"
        if net_move <= -threshold:
            logger.info("momentum: BIG DOWN move (%.1f ATRs) → BUY signal", abs(move_in_atr))
            return "buy"

        return None
