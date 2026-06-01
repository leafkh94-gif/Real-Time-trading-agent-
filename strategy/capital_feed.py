"""
Capital.com live price feed.
Fetches real OHLC candles for any instrument via Capital.com REST API.
Used by main_alerts.py to watch multiple markets in real time.
"""
import logging
import threading
import time

import requests as _req

from strategy.base import Candle, MultiTimeframeCandles, TF_H1, TF_H4
from strategy.feed import PriceFeed

logger = logging.getLogger(__name__)

_DEMO_BASE = "https://demo-api-capital.backend-capital.com/api/v1"
_LIVE_BASE = "https://api-capital.backend-capital.com/api/v1"
_PING_INTERVAL = 8 * 60
_TIMEOUT = 15


class CapitalComFeed(PriceFeed):
    """
    Fetches H4 + H1 candles for a given Capital.com epic (e.g. GOLD, US500).
    Handles session auth and auto-reauth on 401.
    """

    def __init__(self, api_key: str, identifier: str, password: str,
                 epic: str = "GOLD", demo: bool = True):
        self._api_key = api_key
        self._identifier = identifier
        self._password = password
        self._epic = epic
        self._base = _DEMO_BASE if demo else _LIVE_BASE
        self._cst = ""
        self._security_token = ""
        self._lock = threading.Lock()
        self._connect()

    # ── PriceFeed interface ───────────────────────────────────────────────────

    def get_candles(self) -> MultiTimeframeCandles:
        return {
            TF_H4: self._fetch("HOUR_4", 200),
            TF_H1: self._fetch("HOUR",   200),
        }

    # ── Internal ──────────────────────────────────────────────────────────────

    def _connect(self) -> None:
        self._login()
        t = threading.Thread(target=self._keepalive, daemon=True, name=f"feed-{self._epic}")
        t.start()
        logger.info("CapitalComFeed: connected (epic=%s)", self._epic)

    def _login(self) -> None:
        r = _req.post(
            f"{self._base}/session",
            headers={"X-CAP-API-KEY": self._api_key, "Content-Type": "application/json"},
            json={"identifier": self._identifier, "password": self._password,
                  "encryptedPassword": False},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        with self._lock:
            self._cst = r.headers["CST"]
            self._security_token = r.headers["X-SECURITY-TOKEN"]

    def _keepalive(self) -> None:
        while True:
            time.sleep(_PING_INTERVAL)
            try:
                self._request("GET", "/ping")
            except Exception as exc:
                logger.warning("CapitalComFeed keepalive failed: %s", exc)

    def _auth_headers(self) -> dict:
        with self._lock:
            return {"CST": self._cst, "X-SECURITY-TOKEN": self._security_token,
                    "Content-Type": "application/json"}

    def _request(self, method: str, path: str, **kwargs) -> _req.Response:
        r = _req.request(method, f"{self._base}{path}",
                         headers=self._auth_headers(), timeout=_TIMEOUT, **kwargs)
        if r.status_code == 401:
            self._login()
            r = _req.request(method, f"{self._base}{path}",
                             headers=self._auth_headers(), timeout=_TIMEOUT, **kwargs)
        r.raise_for_status()
        return r

    def _fetch(self, resolution: str, max_candles: int) -> list[Candle]:
        try:
            r = self._request("GET", f"/prices/{self._epic}",
                              params={"resolution": resolution, "max": max_candles})
            return self._parse(r.json())
        except Exception as exc:
            logger.error("CapitalComFeed fetch failed (%s %s): %s",
                         self._epic, resolution, exc)
            return []

    @staticmethod
    def _parse(data: dict) -> list[Candle]:
        candles = []
        for p in data.get("prices", []):
            def mid(side): return (side["bid"] + side["ask"]) / 2
            candles.append(Candle(
                timestamp=p["snapshotTime"],
                open=mid(p["openPrice"]),
                high=mid(p["highPrice"]),
                low=mid(p["lowPrice"]),
                close=mid(p["closePrice"]),
                volume=float(p.get("lastTradedVolume", 0)),
            ))
        return candles
