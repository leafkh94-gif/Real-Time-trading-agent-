"""
Smart-money liquidity sweep detector.

A bearish sweep: price briefly pierces a recent swing low (stop hunt), then
the bar closes back above that low → institutional buyers absorbed the sells
→ signal is 'buy'.

A bullish sweep: price briefly pierces a recent swing high, closes back below
→ signal is 'sell'.

Two ATR-based quality gates prevent noise wicks from firing:
  wick_depth   ≥ min_wick_atr  × ATR  (default 0.3×)
  close_margin ≥ min_close_atr × ATR  (default 0.2×)
"""
from typing import Optional, Sequence

from strategy.base import Candle
from strategy.indicators import atr as _atr, swing_highs, swing_lows


class LiquiditySweepDetector:
    def __init__(
        self,
        lookback: int = 20,
        sweep_lookback: int = 3,
        atr_period: int = 14,
        min_wick_atr: float = 0.2,
        min_close_atr: float = 0.2,
    ):
        """
        lookback:       recent candles to search for swing pivot levels.
        sweep_lookback: bars each side required to confirm a pivot (3 = meaningful, not too strict).
        atr_period:     ATR period used for quality gate thresholds.
        min_wick_atr:   wick must pierce at least this fraction of ATR below/above the level.
        min_close_atr:  close must recover at least this fraction of ATR back above/below the level.
        """
        self.lookback       = lookback
        self.sweep_lookback = sweep_lookback
        self.atr_period     = atr_period
        self.min_wick_atr   = min_wick_atr
        self.min_close_atr  = min_close_atr

    @property
    def min_candles(self) -> int:
        return max(
            self.lookback + self.sweep_lookback * 2 + 2,
            self.atr_period + 1,
        )

    def detect(self, candles: Sequence[Candle]) -> Optional[str]:
        """
        Examines only the most recent completed candle for a liquidity sweep.
        Returns 'buy', 'sell', or None.
        """
        if len(candles) < self.min_candles:
            return None

        # ATR for quality thresholds (NaN stripped via v == v)
        atr_vals  = _atr(candles, self.atr_period)
        valid_atr = [v for v in atr_vals if v == v]
        if not valid_atr:
            return None
        current_atr = valid_atr[-1]
        min_wick    = self.min_wick_atr  * current_atr
        min_close   = self.min_close_atr * current_atr

        # Build pivot window, excluding the last bar (the sweep candidate itself)
        window = list(candles[-(self.lookback + self.sweep_lookback * 2 + 1):])
        sh = swing_highs(window, self.sweep_lookback)
        sl = swing_lows( window, self.sweep_lookback)

        recent_highs = [v for v in sh[:-1] if v is not None]
        recent_lows  = [v for v in sl[:-1] if v is not None]

        bar = candles[-1]   # only the most recent completed candle

        # ── Bearish sweep of a swing low → buy (institutional absorption) ─────
        if recent_lows:
            level       = recent_lows[-1]       # most recent pivot low by time
            wick_depth  = level - bar.low        # how far the wick pierced below
            close_above = bar.close - level      # how far the close recovered above

            if wick_depth >= min_wick and close_above >= min_close:
                return "buy"

        # ── Bullish sweep of a swing high → sell (institutional distribution) ─
        if recent_highs:
            level       = recent_highs[-1]      # most recent pivot high by time
            wick_height = bar.high - level       # how far the wick pierced above
            close_below = level - bar.close      # how far the close fell back below

            if wick_height >= min_wick and close_below >= min_close:
                return "sell"

        return None
