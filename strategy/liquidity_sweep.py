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
import logging
from typing import Optional, Sequence

from strategy.base import Candle
from strategy.indicators import atr as _atr, swing_highs, swing_lows

logger = logging.getLogger(__name__)


class LiquiditySweepDetector:
    def __init__(
        self,
        lookback: int = 20,
        sweep_lookback: int = 3,
        atr_period: int = 14,
        min_wick_atr: float = 0.01,
        min_close_atr: float = 0.01,
        sweep_search: int = 20,
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
            logger.info("sweep diag: not enough candles (%d < %d)", len(candles), self.min_candles)
            return None

        # ATR-based quality thresholds (v == v strips NaN)
        atr_vals  = _atr(candles, self.atr_period)
        valid_atr = [v for v in atr_vals if v == v]
        if not valid_atr:
            logger.info("sweep diag: ATR unavailable")
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

        # Per-leg diagnostics: count where each candidate sweep died so the logs
        # reveal whether the gate is starved by missing pivots, weak sweeps, or
        # an unconfirmed BOS — rather than a single opaque "no sweep" message.
        diag = {
            "no_prior_pivot": 0,   # no swing level to sweep before this bar
            "weak_sweep":     0,   # wick/close did not meet ATR quality threshold
            "valid_sweep":    0,   # sweep candle qualified
            "no_bos_pivot":   0,   # sweep ok but no opposite pivot for a BOS level
            "bos_unconfirmed": 0,  # BOS level exists but no later close broke it
        }

        # ── BUY: sweep of swing low + break above swing high ──────────────────
        for sweep_idx in range(n - 2, sweep_start - 1, -1):
            bar = window[sweep_idx]

            prior_lows = [(i, v) for i, v in low_pivots if i < sweep_idx]
            if not prior_lows:
                diag["no_prior_pivot"] += 1
                continue
            _, sweep_level = prior_lows[-1]          # most recent swing low before this bar

            wick_depth  = sweep_level - bar.low       # how far the wick pierced below
            close_above = bar.close - sweep_level     # how far the close recovered above

            if wick_depth < min_wick or close_above < min_close:
                diag["weak_sweep"] += 1
                continue                              # sweep too weak — skip

            diag["valid_sweep"] += 1

            # Sweep is valid. Find the BOS reference level.
            prior_highs = [(i, v) for i, v in high_pivots if i < sweep_idx]
            if not prior_highs:
                diag["no_bos_pivot"] += 1
                continue
            _, bos_level = prior_highs[-1]            # most recent swing high before sweep

            # BOS confirmed if any bar after the sweep closes above bos_level
            if any(window[j].close > bos_level for j in range(sweep_idx + 1, n)):
                return "buy"
            diag["bos_unconfirmed"] += 1

        # ── SELL: sweep of swing high + break below swing low ─────────────────
        for sweep_idx in range(n - 2, sweep_start - 1, -1):
            bar = window[sweep_idx]

            prior_highs = [(i, v) for i, v in high_pivots if i < sweep_idx]
            if not prior_highs:
                diag["no_prior_pivot"] += 1
                continue
            _, sweep_level = prior_highs[-1]

            wick_height = bar.high - sweep_level
            close_below = sweep_level - bar.close

            if wick_height < min_wick or close_below < min_close:
                diag["weak_sweep"] += 1
                continue

            diag["valid_sweep"] += 1

            prior_lows = [(i, v) for i, v in low_pivots if i < sweep_idx]
            if not prior_lows:
                diag["no_bos_pivot"] += 1
                continue
            _, bos_level = prior_lows[-1]

            if any(window[j].close < bos_level for j in range(sweep_idx + 1, n)):
                return "sell"
            diag["bos_unconfirmed"] += 1

        # No setup. Log why, with the structural context, so the live logs pinpoint
        # the failing leg instead of a generic skip.
        logger.info(
            "sweep diag: NO SETUP | window=%d bars | swing_highs=%d swing_lows=%d | "
            "atr=%.4f min_wick=%.4f | candidates: no_prior_pivot=%d weak_sweep=%d "
            "valid_sweep=%d no_bos_pivot=%d bos_unconfirmed=%d",
            n, len(high_pivots), len(low_pivots),
            current_atr, min_wick,
            diag["no_prior_pivot"], diag["weak_sweep"], diag["valid_sweep"],
            diag["no_bos_pivot"], diag["bos_unconfirmed"],
        )
        return None
