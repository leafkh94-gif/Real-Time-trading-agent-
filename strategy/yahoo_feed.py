"""
Yahoo Finance price feed.
Fetches H1 candles for any instrument and resamples them to H4.
No account, no password, no API key required.
"""
import logging
import time
import random
from strategy.base import Candle, MultiTimeframeCandles, TF_H1, TF_H4
from strategy.feed import PriceFeed

logger = logging.getLogger(__name__)

TICKER_MAP: dict[str, str] = {
    "GOLD":  "GC=F",    # CME Gold futures — reliable H1 data, tracks spot within $5-20
    "US500": "^GSPC",
    "US100": "^NDX",
    "US30":  "^DJI",
}


class YahooFinanceFeed(PriceFeed):
    """
    Fetches 60 days of H1 candles from Yahoo Finance and resamples to H4.
    Pass an epic code (GOLD, US500, US100, US30) or a raw Yahoo ticker.
    """

    def __init__(self, epic: str):
        self._epic = epic
        self._ticker = TICKER_MAP.get(epic, epic)
        logger.info("YahooFinanceFeed: %s -> %s", epic, self._ticker)

    def get_candles(self) -> MultiTimeframeCandles:
        import yfinance as yf
        import pandas as pd

        df = None
        for attempt in range(1, 4):
            try:
                time.sleep(random.uniform(2, 5))
                df = yf.download(
                    self._ticker,
                    period="60d",
                    interval="1h",
                    auto_adjust=True,
                    progress=False,
                    actions=False,
                )
                if df is not None and not df.empty:
                    break
                logger.warning("YahooFinanceFeed: empty response for %s (attempt %d)", self._ticker, attempt)
            except Exception as exc:
                wait = 10 * attempt
                logger.warning("YahooFinanceFeed: attempt %d failed for %s: %s — retrying in %ds",
                               attempt, self._ticker, exc, wait)
                time.sleep(wait)

        if df is None or df.empty:
            logger.error("YahooFinanceFeed: all attempts failed for %s", self._ticker)
            return {TF_H4: [], TF_H1: []}

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        h1 = self._to_candles(df)
        df_h4 = (
            df.resample("4h")
            .agg({"Open": "first", "High": "max", "Low": "min",
                  "Close": "last", "Volume": "sum"})
            .dropna(subset=["Open", "Close"])
        )
        h4 = self._to_candles(df_h4)
        logger.debug("YahooFinanceFeed %s: %d H1 candles, %d H4 candles",
                     self._epic, len(h1), len(h4))
        return {TF_H4: h4, TF_H1: h1}

    @staticmethod
    def _to_candles(df) -> list[Candle]:
        candles = []
        for ts, row in df.iterrows():
            try:
                candles.append(Candle(
                    timestamp=str(ts),
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=float(row.get("Volume", 0) or 0),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        return candles
