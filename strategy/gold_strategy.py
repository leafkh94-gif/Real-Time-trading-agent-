"""
Alert strategy — liquidity sweep across US indices.

Chains three gates in sequence:
  Gate 1 — Data sufficiency  (H4 ≥ 65 candles, H1 ≥ 28 candles)
  Gate 2 — Regime filter     (H4 ATR/close > 1.8% → VOLATILE → skip)
  Gate 3 — Liquidity sweep   (H1 wick pierces swing level + closes back)

Returns a Signal (direction + lots) or None.
Never touches the broker or any execution layer.
"""
import logging
import math
from datetime import datetime, time
from typing import Optional
from zoneinfo import ZoneInfo

from execution.models import Signal
from strategy.base import MarketRegime, MultiTimeframeCandles, StrategyBase, TF_H1, TF_H4
from strategy.liquidity_sweep import LiquiditySweepDetector
from strategy.regime_filter import RegimeFilter

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_LONDON_OPEN  = time(3,  0)
_LONDON_CLOSE = time(12, 0)
_NY_OPEN      = time(9, 30)
_NY_CLOSE     = time(16, 0)

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
        h1 = candles.get(TF_H1, [])
        d1 = candles.get(TF_D1, [])

        if len(h1) < _MIN_H1:
            logger.info("gate1 SKIP: not enough H1 candles (%d < %d)", len(h1), _MIN_H1)
            return None

        # ── Gate 2: regime filter (H4) ────────────────────────────────────────
        regime = self.regime_filter.classify(h4)
        logger.info("gate2: regime=%s", regime.value)
        if regime == MarketRegime.VOLATILE:
            logger.info("gate2 SKIP: regime VOLATILE (ATR/close > 1.8%%)")
            return None

        # ── Gate 1: price confirms direction ──────────────────────────────────
        direction = self._price_confirmation(h1)
        if direction is None:
            logger.info("gate1 SKIP: no price confirmation (breakout/bounce)")
            return None
        logger.info("gate3 PASS: sweep direction=%s  regime=%s", direction, regime.value)

        return Signal(direction=direction, lots=self.lots)
