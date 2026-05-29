"""
ML-based signal filter. Accepts or rejects a candidate signal based on
feature-engineered inputs from recent candles.

Phase 3 ships a passthrough implementation that accepts every signal.
Replace MLSignalFilter._predict() with a trained model (scikit-learn,
ONNX, etc.) before going live — the interface is stable.
"""
from abc import ABC, abstractmethod
from typing import Sequence

from execution.models import Signal
from strategy.base import Candle


class SignalFilter(ABC):
    @abstractmethod
    def accept(self, signal: Signal, candles: Sequence[Candle]) -> bool: ...


class MLSignalFilter(SignalFilter):
    """
    Passthrough filter — accepts every signal.
    Wire in a real model by overriding _predict():
        return your_model.predict(self._features(candles)) == 1
    """

    def accept(self, signal: Signal, candles: Sequence[Candle]) -> bool:
        return self._predict(signal, candles)

    def _predict(self, signal: Signal, candles: Sequence[Candle]) -> bool:
        return True

    def _features(self, candles: Sequence[Candle]) -> list[float]:
        """
        Placeholder feature vector. Replace with engineered features when
        training the real model.
        """
        if not candles:
            return []
        last = candles[-1]
        return [last.close, last.high - last.low, last.volume]
