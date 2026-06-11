"""
Smart-money liquidity sweep with structure confirmation (CHOCH / BOS).

Full two-step pattern required before any signal fires:

  Step 1 — Sweep (مسح السيولة)
    A candle's wick pierces a recent swing level and closes back inside.
    This is the stop-hunt: institutional players trigger retail stops,
    absorb the liquidity, then reverse.

  Step 2 — Break of Structure / Change of Character (كسر الهيكل)
    A subsequent candle closes beyond the most recent swing point on the
    OPPOSITE side, confirming the reversal is real and not just noise.

BUY example:
  - Candle wicks below a recent swing LOW (sweeps bearish liquidity).
  - A later candle closes ABOVE the most recent swing HIGH before the sweep.
  → Confirmed: institutions absorbed sell stops and are pushing price up.

SELL example:
  - Candle wicks above a recent swing HIGH (sweeps bullish liquidity).
  - A later candle closes BELOW the most recent swing LOW before the sweep.
  → Confirmed: institutions distributed into buy stops and are pushing down.

Quality gates on the sweep candle:
  wick_depth   ≥ min_wick_atr  × ATR  — real pierce, not a 1-point touch
  close_margin ≥ min_close_atr × ATR  — strong rejection, not a drift back
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
        min_wick_atr: float = 0.05,
        min_close_atr: float = 0.05,
        sweep_search: int = 14,
    ):
        """
        lookback:       candles used for the swing pivot detection window.
        sweep_lookback: bars each side required to confirm a pivot (3 = balanced).
        atr_period:     ATR period for wick / close quality thresholds.
        min_wick_atr:   wick must pierce the level by at least this × ATR.
        min_close_atr:  close must recover by at least this × ATR.
        sweep_search:   how many recent bars to search for a valid sweep.
        """
        self.lookback       = lookback
        self.sweep_lookback = sweep_lookback
        self.atr_period     = atr_period
        self.min_wick_atr   = min_wick_atr
        self.min_close_atr  = min_close_atr
        self.sweep_search   = sweep_search

    @property
    def min_candles(self) -> int:
        return max(
            self.lookback + self.sweep_lookback * 2 + self.sweep_search + 2,
            self.atr_period + 1,
        )

    def detect(self, candles: Sequence[Candle]) -> Optional[str]:
        """
        Returns 'buy', 'sell', or None.
        Both a quality sweep AND a subsequent break of structure are required.
        Iterates newest-to-oldest so the most recent confirmed setup is returned.
        """
        if len(candles) < self.min_candles:
            return None

        # ATR-based quality thresholds (v == v strips NaN)
        atr_vals  = _atr(candles, self.atr_period)
        valid_atr = [v for v in atr_vals if v == v]
        if not valid_atr:
            return None
        current_atr = valid_atr[-1]
        min_wick    = self.min_wick_atr  * current_atr
        min_close   = self.min_close_atr * current_atr

        # Full pivot detection window
        n_window = self.lookback + self.sweep_lookback * 2 + 1
        window   = list(candles[-n_window:])
        n        = len(window)

        sh = swing_highs(window, self.sweep_lookback)
        sl = swing_lows( window, self.sweep_lookback)

        high_pivots = [(i, v) for i, v in enumerate(sh) if v is not None]
        low_pivots  = [(i, v) for i, v in enumerate(sl) if v is not None]

        # Search for a sweep in the last `sweep_search` bars (not the current bar —
        # the BOS must occur after the sweep, so sweep can't be bar[-1]).
        sweep_start = max(0, n - 1 - self.sweep_search)

        # ── BUY: sweep of swing low + break above swing high ──────────────────
        for sweep_idx in range(n - 2, sweep_start - 1, -1):
            bar = window[sweep_idx]

            prior_lows = [(i, v) for i, v in low_pivots if i < sweep_idx]
            if not prior_lows:
                continue
            _, sweep_level = prior_lows[-1]          # most recent swing low before this bar

            wick_depth  = sweep_level - bar.low       # how far the wick pierced below
            close_above = bar.close - sweep_level     # how far the close recovered above

            if wick_depth < min_wick or close_above < min_close:
                continue                              # sweep too weak — skip

            # Sweep is valid. Find the BOS reference level.
            prior_highs = [(i, v) for i, v in high_pivots if i < sweep_idx]
            if not prior_highs:
                continue
            _, bos_level = prior_highs[-1]            # most recent swing high before sweep

            # BOS confirmed if any bar after the sweep closes above bos_level
            if any(window[j].close > bos_level for j in range(sweep_idx + 1, n)):
                return "buy"

        # ── SELL: sweep of swing high + break below swing low ─────────────────
        for sweep_idx in range(n - 2, sweep_start - 1, -1):
            bar = window[sweep_idx]

            prior_highs = [(i, v) for i, v in high_pivots if i < sweep_idx]
            if not prior_highs:
                continue
            _, sweep_level = prior_highs[-1]

            wick_height = bar.high - sweep_level
            close_below = sweep_level - bar.close

            if wick_height < min_wick or close_below < min_close:
                continue

            prior_lows = [(i, v) for i, v in low_pivots if i < sweep_idx]
            if not prior_lows:
                continue
            _, bos_level = prior_lows[-1]

            if any(window[j].close < bos_level for j in range(sweep_idx + 1, n)):
                return "sell"

        return None
