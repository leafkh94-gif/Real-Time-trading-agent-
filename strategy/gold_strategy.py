"""
GoldStrategy — top-level liquidity-sweep strategy.

Despite the name (kept for backward compatibility), this strategy is
instrument-agnostic and is applied to every market in the watchlist:
Gold (XAU/USD), S&P 500, Nasdaq 100, and Dow Jones.

Chains: H4 regime filter → H1 liquidity sweep → Claude/ML signal filter.
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


def _default_signal_filter() -> SignalFilter:
    return MLSignalFilter()  # passthrough — Gate 2 + Gate 3 are sufficient filters


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
        self.signal_filter = signal_filter or _default_signal_filter()

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
            logger.info("gate2 SKIP: regime VOLATILE")
            return None

        # ── Gate 3: liquidity sweep (H1) ─────────────────────────────────────
        direction = self.sweep_detector.detect(h1)
        if direction is None:
            logger.info("gate3 SKIP: no liquidity sweep detected")
            return None
        logger.info("gate3 PASS: sweep direction=%s", direction)

        # ── Gate 4: signal filter (Claude / ML) ───────────────────────────────
        # Regime-direction alignment removed: liquidity sweeps are reversal
        # signals by design, so the sweep direction stands on its own.
        candidate = Signal(direction=direction, lots=self.lots)
        if not self.signal_filter.accept(candidate, h1):
            logger.info("gate5 SKIP: signal filter rejected")
            return None

        logger.info("signal generated: %s %.2f lots (regime=%s)", direction, self.lots, regime.value)
        return candidate
