"""
Core data types and abstract base class for all strategy modules.
Strategy modules output Signal objects only — they never touch the broker.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from execution.models import Signal


@dataclass(frozen=True)
class Candle:
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class MarketRegime(str, Enum):
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGING = "ranging"
    VOLATILE = "volatile"


# Timeframe labels used as dict keys throughout the strategy layer
TF_H4 = "H4"
TF_H1 = "H1"
TF_M15 = "M15"

MultiTimeframeCandles = dict[str, list[Candle]]


class StrategyBase(ABC):
    @abstractmethod
    def evaluate(self, candles: MultiTimeframeCandles) -> Optional[Signal]:
        """
        Analyse the latest candles and return a Signal, or None if no trade.
        Must never call the broker or mutate any state outside strategy/.
        """
        ...

    @property
    def name(self) -> str:
        return self.__class__.__name__
