"""
Capital.com live price feed — returns OHLCV DataFrames for the scoring engine.
All feed instances share ONE session (Capital.com rate-limits creation to 1/s).
"""
from __future__ import annotations

import logging
import random
import threading
import time

import pandas as pd
import requests

logger = logging.getLogger(__name__)

_DEMO_BASE = "https://demo-api-capital.backend-capital.com/api/v1"
_LIVE_BASE = "https://api-capital.backend-capital.com/api/v1"
_PING_INTERVAL = 8 * 60
_TIMEOUT       = 15
_LOGIN_RETRIES = 5
_EMPTY_DF      = pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])


class CapitalComFeed:
    """
    Fetches OHLCV DataFrames for a single epic via the Capital.com REST API.
    Session (CST + X-SECURITY-TOKEN) is shared across all instances.
    """

    _cst:              str  = ""
    _security_token:   str  = ""
    _session_lock           = threading.Lock()
    _keepalive_started: bool = False

    def __init__(self, api_key: str, identifier: str, password: str,
                 epic: str = "US500", demo: bool = True):
        self._api_key    = api_key
        self._identifier = identifier
        self._password   = password
        self._epic       = epic
        self._base       = _DEMO_BASE if demo else _LIVE_BASE
        self._connect()

    # ── Public API ───────────────────────────────────────────────────

    def get_candles(self, resolution: str, max_count: int = 200) -> pd.DataFrame:
        """
        Return a DataFrame with columns [open, high, low, close, volume],
        oldest row first. resolution: 'MINUTE_15', 'HOUR', 'DAY', etc.
        """
        try:
            return self._to_df(self._fetch(resolution, max_count))
        except Exception as exc:
            logger.error("CapitalComFeed fetch failed (%s %s): %s",
                         self._epic, resolution, exc)
            return _EMPTY_DF.copy()

    def _fetch(self, resolution: str, max_count: int) -> list:
        """Fetch raw price rows from Capital.com prices endpoint."""
        r = self._request("GET", f"/prices/{self._epic}",
                          params={"resolution": resolution, "max": max_count})
        return r.json().get("prices", [])

    def _to_df(self, prices: list) -> pd.DataFrame:
        """Convert raw Capital.com price rows to an OHLCV DataFrame.
        Includes a naive-UTC 'time' column (candle open time) used by the
        signal journal to resolve outcomes against post-alert candles."""
        rows = []
        for p in prices:
            def mid(s): return (s["bid"] + s["ask"]) / 2
            rows.append({
                "time":   p.get("snapshotTimeUTC") or p.get("snapshotTime"),
                "open":   mid(p["openPrice"]),
                "high":   mid(p["highPrice"]),
                "low":    mid(p["lowPrice"]),
                "close":  mid(p["closePrice"]),
                "volume": float(p.get("lastTradedVolume", 0)),
            })
        if not rows:
            return _EMPTY_DF.copy()
        df = pd.DataFrame(rows)
        df["time"] = pd.to_datetime(df["time"], errors="coerce", utc=True).dt.tz_localize(None)
        return df

    # ── Session management ───────────────────────────────────────────────

    def _connect(self) -> None:
        cls = CapitalComFeed
        with cls._session_lock:
            if not cls._cst:
                self._login_locked()
            if not cls._keepalive_started:
                cls._keepalive_started = True
                threading.Thread(target=self._keepalive, daemon=True,
                                 name="capital-keepalive").start()
        logger.info("CapitalComFeed ready (epic=%s)", self._epic)

    def _login_locked(self) -> None:
        cls      = CapitalComFeed
        last_exc = None
        for attempt in range(1, _LOGIN_RETRIES + 1):
            try:
                r = requests.post(
                    f"{self._base}/session",
                    headers={"X-CAP-API-KEY": self._api_key,
                             "Content-Type": "application/json"},
                    json={"identifier": self._identifier,
                          "password":   self._password,
                          "encryptedPassword": False},
                    timeout=_TIMEOUT,
                )
                if r.status_code in (400, 401, 403):
                    raise RuntimeError(
                        f"Capital.com rejected credentials "
                        f"(HTTP {r.status_code}): {r.text[:200]}")
                r.raise_for_status()
                cls._cst            = r.headers["CST"]
                cls._security_token = r.headers["X-SECURITY-TOKEN"]
                logger.info("Capital.com session created (attempt %d)", attempt)
                return
            except RuntimeError:
                raise
            except Exception as exc:
                last_exc = exc
                wait     = min(60, 2 ** attempt) + random.uniform(0, 2)
                logger.warning("Login attempt %d failed (%s) — retrying in %.1fs",
                               attempt, exc, wait)
                time.sleep(wait)
        raise RuntimeError(
            f"Capital.com login failed after {_LOGIN_RETRIES} attempts: {last_exc}")

    def _reauth(self, stale_cst: str) -> None:
        cls = CapitalComFeed
        with cls._session_lock:
            if cls._cst == stale_cst:
                self._login_locked()

    def _keepalive(self) -> None:
        while True:
            time.sleep(_PING_INTERVAL)
            try:
                self._request("GET", "/ping")
            except Exception as exc:
                logger.warning("Keepalive failed: %s", exc)

    def _auth_headers(self) -> dict:
        cls = CapitalComFeed
        with cls._session_lock:
            return {"CST": cls._cst,
                    "X-SECURITY-TOKEN": cls._security_token,
                    "Content-Type": "application/json"}

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        headers = self._auth_headers()
        r = requests.request(method, f"{self._base}{path}",
                             headers=headers, timeout=_TIMEOUT, **kwargs)
        if r.status_code == 401:
            self._reauth(headers["CST"])
            r = requests.request(method, f"{self._base}{path}",
                                 headers=self._auth_headers(), timeout=_TIMEOUT, **kwargs)
        r.raise_for_status()
        return r
