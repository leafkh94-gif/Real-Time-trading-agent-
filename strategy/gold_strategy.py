"""
GoldStrategy — top-level strategy for XAU/USD.
Chains: H4 regime filter → H1 liquidity sweep → alignment check → ML filter.
Outputs a Signal or None. Never touches the broker or any core module.
"""
import logging
from typing import Optional

from execution.models import Signal
from strategy.base import MarketRegime, MultiTimeframeCandles, StrategyBase, TF_H1, TF_H4
from strategy.liquidity_sweep import LiquiditySweepDetector
from strategy.regime_filter import RegimeFilter
from strategy.signal_filter import MLSignalFilter, SignalFilter

logger = logging.getLogger(__name__)


class GoldStrategy(StrategyBase):
    def __init__(
        self,
        lots: float = 0.05,
        regime_filter: RegimeFilter | None = None,
        sweep_detector: LiquiditySweepDetector | None = None,
        signal_filter: SignalFilter | None = None,
    ):
        self.lots = lots
        self.regime_filter = regime_filter or RegimeFilter()
        self.sweep_detector = sweep_detector or LiquiditySweepDetector()
        self.signal_filter = signal_filter or MLSignalFilter()

    def evaluate(self, candles: MultiTimeframeCandles) -> Optional[Signal]:
        h4 = candles.get(TF_H4, [])
        h1 = candles.get(TF_H1, [])

        # ── Gate 1: enough data ───────────────────────────────────────────────
        if len(h4) < self.regime_filter.min_candles:
            logger.debug("not enough H4 candles (%d < %d)", len(h4), self.regime_filter.min_candles)
            return None
        if len(h1) < self.sweep_detector.min_candles:
            logger.debug("not enough H1 candles (%d < %d)", len(h1), self.sweep_detector.min_candles)
            return None

        # ── Gate 2: regime filter (H4) ────────────────────────────────────────
        regime = self.regime_filter.classify(h4)
        if regime == MarketRegime.VOLATILE:
            logger.debug("regime VOLATILE — skipping")
            return None

        # ── Gate 3: liquidity sweep (H1) ─────────────────────────────────────
        direction = self.sweep_detector.detect(h1)
        if direction is None:
            logger.debug("no liquidity sweep detected")
            return None

        # ── Gate 4: regime-direction alignment ────────────────────────────────
        if regime == MarketRegime.TRENDING_UP and direction != "buy":
            logger.debug("sweep direction %s misaligns with TRENDING_UP — skipping", direction)
            return None
        if regime == MarketRegime.TRENDING_DOWN and direction != "sell":
            logger.debug("sweep direction %s misaligns with TRENDING_DOWN — skipping", direction)
            return None

        # ── Gate 5: ML filter ─────────────────────────────────────────────────
        candidate = Signal(direction=direction, lots=self.lots)
        if not self.signal_filter.accept(candidate, h1):
            logger.debug("ML filter rejected signal")
            return None

        logger.info("signal generated: %s %.2f lots (regime=%s)", direction, self.lots, regime.value)
        return candidate
