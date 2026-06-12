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
from typing import Optional

from execution.models import Signal
from strategy.base import MarketRegime, MultiTimeframeCandles, StrategyBase, TF_H1, TF_H4
from strategy.liquidity_sweep import LiquiditySweepDetector
from strategy.regime_filter import RegimeFilter

logger = logging.getLogger(__name__)


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

        # ── Gate 1: enough data ───────────────────────────────────────────────
        if len(h4) < self.regime_filter.min_candles:
            logger.info("gate1 SKIP: not enough H4 candles (%d < %d)", len(h4), self.regime_filter.min_candles)
            return None
        if len(h1) < self.sweep_detector.min_candles:
            logger.info("gate1 SKIP: not enough H1 candles (%d < %d)", len(h1), self.sweep_detector.min_candles)
            return None

        # ── Gate 2: regime filter (H4) ────────────────────────────────────────
        regime = self.regime_filter.classify(h4)
        logger.info("gate2: regime=%s", regime.value)
        if regime == MarketRegime.VOLATILE:
            logger.info("gate2 SKIP: regime VOLATILE (ATR/close > 1.8%%)")
            return None

        # ── Gate 3: liquidity sweep (H1) ─────────────────────────────────────
        direction = self.sweep_detector.detect(h1)
        if direction is None:
            logger.info("gate3 SKIP: no liquidity sweep detected")
            return None
        logger.info("gate3 PASS: sweep direction=%s  regime=%s", direction, regime.value)

        return Signal(direction=direction, lots=self.lots)
