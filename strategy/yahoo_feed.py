import logging, time, random, os
import requests
from strategy.base import Candle, MultiTimeframeCandles, TF_H1, TF_H4
from strategy.feed import PriceFeed

logger = logging.getLogger(__name__)

TICKER_MAP = {"GOLD": "GLD", "US500": "SPY", "US100": "QQQ", "US30": "DIA"}
AV_BASE = "https://www.alphavantage.co/query"

class YahooFinanceFeed(PriceFeed):
    def __init__(self, epic):
        self._epic = epic
        self._ticker = TICKER_MAP.get(epic, epic)
        self._api_key = os.environ.get("ALPHAVANTAGE_API_KEY", "")
        logger.info("YahooFinanceFeed: %s -> %s", epic, self._ticker)

    def get_candles(self):
        import pandas as pd
        for attempt in range(1, 4):
            try:
                time.sleep(random.uniform(1, 3))
                resp = requests.get(AV_BASE, params={"function": "TIME_SERIES_INTRADAY", "symbol": self._ticker, "interval": "60min", "outputsize": "full", "apikey": self._api_key}, timeout=30)
                data = resp.json()
                ts_key = "Time Series (60min)"
                if ts_key not in data:
                    logger.warning("AV: no data for %s attempt %d: %s", self._ticker, attempt, list(data.keys()))
                    time.sleep(10 * attempt)
                    continue
                rows = [{"timestamp": dt, "Open": float(v["1. open"]), "High": float(v["2. high"]), "Low": float(v["3. low"]), "Close": float(v["4. close"]), "Volume": float(v["5. volume"])} for dt, v in data[ts_key].items()]
                df = pd.DataFrame(rows)
                df["timestamp"] = pd.to_datetime(df["timestamp"])
                df = df.set_index("timestamp").sort_index()
                h1 = self._to_candles(df)
                h4 = self._to_candles(df.resample("4h").agg({"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna(subset=["Open","Close"]))
                logger.info("AV feed %s: %d H1, %d H4", self._epic, len(h1), len(h4))
                return {TF_H4: h4, TF_H1: h1}
            except Exception as exc:
                logger.warning("AV attempt %d failed for %s: %s", attempt, self._ticker, exc)
                time.sleep(10 * attempt)
        logger.error("AV: all attempts failed for %s", self._ticker)
        return {TF_H4: [], TF_H1: []}

    @staticmethod
    def _to_candles(df):
        candles = []
        for ts, row in df.iterrows():
            try:
                candles.append(Candle(timestamp=str(ts), open=float(row["Open"]), high=float(row["High"]), low=float(row["Low"]), close=float(row["Close"]), volume=float(row.get("Volume", 0) or 0)))
            except (KeyError, TypeError, ValueError):
                continue
        return candles