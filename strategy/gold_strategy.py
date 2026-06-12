"""
GoldStrategy — liquidity sweep + soft BOS confirmation.

Gate 1 (H4 EMA trend):   TRENDING_UP → BUY only
                          TRENDING_DOWN → SELL only
                          RANGING / VOLATILE → skip
Gate 2 (H4 ATR):         VOLATILE → skip  (handled inside RegimeFilter)
Gate 3 (H1 sweep):       wick pierces swing level, closes back — direction must
                          match the H4 trend
Gate 4 (H1 soft BOS):    after a bullish sweep, close breaks above last swing
                          high; after a bearish sweep, close breaks below last
                          swing low.  Returns confirmed=True when this passes.

Returns Signal(confirmed=False) when only Gates 1-3 pass (setup forming).
Returns Signal(confirmed=True)  when all four gates pass (entry confirmed).
Returns None when any gate fails.
"""
import logging
from typing import Optional

from execution.models import Signal
from strategy.base import MarketRegime, MultiTimeframeCandles, StrategyBase, TF_H1, TF_H4
from strategy.indicators import swing_highs, swing_lows
from strategy.liquidity_sweep import LiquiditySweepDetector
from strategy.regime_filter import RegimeFilter

logger = logging.getLogger(__name__)

_BOS_WINDOW = 30   # H1 bars to search for swing levels when checking BOS


class GoldStrategy(StrategyBase):
    def __init__(
        self,
        lots: float = 0.05,
        regime_filter: RegimeFilter | None = None,
        sweep_detector: LiquiditySweepDetector | None = None,
    ):
        self.lots = lots
        self.regime_filter  = regime_filter  or RegimeFilter()
        self.sweep_detector = sweep_detector or LiquiditySweepDetector()

    def evaluate(self, candles: MultiTimeframeCandles) -> Optional[Signal]:
        h4 = candles.get(TF_H4, [])
        h1 = candles.get(TF_H1, [])

        # ── Gate 1+2: regime (H4) ─────────────────────────────────────────────
        if len(h4) < self.regime_filter.min_candles:
            logger.info("gate1 SKIP: not enough H4 candles (%d)", len(h4))
            return None

        regime = self.regime_filter.classify(h4)
        logger.info("gate1: regime=%s", regime.value)

        if regime == MarketRegime.VOLATILE:
            logger.info("gate2 SKIP: VOLATILE — ATR too high")
            return None
        if regime == MarketRegime.RANGING:
            logger.info("gate1 SKIP: RANGING — no directional bias")
            return None

        allowed = "buy" if regime == MarketRegime.TRENDING_UP else "sell"

        # ── Gate 3: liquidity sweep (H1) ──────────────────────────────────────
        if len(h1) < self.sweep_detector.min_candles:
            logger.info("gate3 SKIP: not enough H1 candles (%d)", len(h1))
            return None

        direction = self.sweep_detector.detect(h1)
        if direction is None:
            logger.info("gate3 SKIP: no liquidity sweep detected")
            return None
        if direction != allowed:
            logger.info("gate3 SKIP: sweep=%s contradicts regime=%s", direction, regime.value)
            return None

        logger.info("gate3 PASS: %s sweep  regime=%s", direction, regime.value)

        # ── Gate 4 (soft): BOS confirmation (H1) ─────────────────────────────
        confirmed = self._soft_bos(h1, direction)
        logger.info("gate4: BOS confirmed=%s", confirmed)

        return Signal(direction=direction, lots=self.lots, confirmed=confirmed)

    def _soft_bos(self, h1: list, direction: str) -> bool:
        """
        Soft BOS: checks whether the latest H1 close has broken the most recent
        swing high (buy) or swing low (sell) — no right-side confirmation needed.
        """
        window = list(h1[-_BOS_WINDOW:])
        sh  = swing_highs(window, lookback=3)
        sl_ = swing_lows( window, lookback=3)
        cur_close = h1[-1].close

        if direction == "buy":
            recent_highs = [v for v in sh[:-3] if v is not None]
            if not recent_highs:
                return False
            return cur_close > recent_highs[-1]
        else:
            recent_lows = [v for v in sl_[:-3] if v is not None]
            if not recent_lows:
                return False
            return cur_close < recent_lows[-1]
