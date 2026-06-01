"""
Price feed interface and a random-walk implementation for paper trading / testing.
Production feeds (broker REST/WebSocket) implement PriceFeed and are injected
into main.py — the strategy and main loop never care which feed is active.
"""
import math
import random
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone

from strategy.base import Candle, MultiTimeframeCandles, TF_H1, TF_H4, TF_M15


class PriceFeed(ABC):
    @abstractmethod
    def get_candles(self) -> MultiTimeframeCandles:
        """Return the latest candles keyed by timeframe label."""
        ...


class RandomWalkFeed(PriceFeed):
    """
    Generates synthetic XAU/USD-like candles using a Gaussian random walk.
    Suitable for paper trading demos and unit tests — not for backtesting.

    Candles are generated fresh on each call to get_candles() so consecutive
    calls return independent (non-accumulating) price series.
    """

    def __init__(
        self,
        start_price: float = 2300.0,
        daily_volatility: float = 0.008,
        n_candles: int = 200,
        seed: int | None = None,
    ):
        self.start_price = start_price
        self.daily_volatility = daily_volatility
        self.n_candles = n_candles
        self._rng = random.Random(seed)

    def get_candles(self) -> MultiTimeframeCandles:
        return {
            TF_H4: self._generate(bars=self.n_candles, minutes_per_bar=240),
            TF_H1: self._generate(bars=self.n_candles, minutes_per_bar=60),
            TF_M15: self._generate(bars=self.n_candles, minutes_per_bar=15),
        }

    def _generate(self, bars: int, minutes_per_bar: int) -> list[Candle]:
        """Build OHLCV candles from a Gaussian close-price walk."""
        # Per-bar volatility scaled from daily
        bar_vol = self.daily_volatility * math.sqrt(minutes_per_bar / (24 * 60))
        price = self.start_price
        candles: list[Candle] = []
        now = datetime.now(timezone.utc)

        for i in range(bars):
            ts = (now - timedelta(minutes=minutes_per_bar * (bars - i))).isoformat()
            pct = self._rng.gauss(0, bar_vol)
            close = round(price * (1 + pct), 2)
            high = round(max(price, close) * (1 + abs(self._rng.gauss(0, bar_vol * 0.5))), 2)
            low = round(min(price, close) * (1 - abs(self._rng.gauss(0, bar_vol * 0.5))), 2)
            open_ = round(price, 2)
            volume = round(abs(self._rng.gauss(500, 150)), 2)
            candles.append(Candle(timestamp=ts, open=open_, high=high, low=low, close=close, volume=volume))
            price = close

        return candles
