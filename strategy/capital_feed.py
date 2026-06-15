"""
Capital.com live price feed.
Fetches real OHLC candles for any instrument via Capital.com REST API.
Used by main_alerts.py to watch multiple markets in real time.

All feed instances share ONE session: Capital.com rate-limits session
creation to 1 per second, so per-instrument logins trigger 429 errors.
The first instance logs in; the rest reuse the same CST/security tokens.
"""
import logging
import random
import threading
import time

import pandas as pd
import requests as _req

from strategy.base import Candle, MultiTimeframeCandles, TF_H1, TF_H4
from strategy.feed import PriceFeed

logger = logging.getLogger(__name__)

_DEMO_BASE = "https://demo-api-capital.backend-capital.com/api/v1"
_LIVE_BASE = "https://api-capital.backend-capital.com/api/v1"
_PING_INTERVAL = 8 * 60
_TIMEOUT = 15
_LOGIN_RETRIES = 5


class CapitalComFeed(PriceFeed):
    """
    Fetches H4 + H1 candles for a given Capital.com epic (e.g. GOLD, US500).
    Handles session auth and auto-reauth on 401. The session (CST + security
    token) is shared across all instances — they all use the same account.
    """

    # Shared session state — class-level, guarded by _session_lock
    _cst: str = ""
    _security_token: str = ""
    _session_lock = threading.Lock()
    _keepalive_started = False

    def __init__(self, api_key: str, identifier: str, password: str,
                 epic: str = "GOLD", demo: bool = True):
        self._api_key = api_key
        self._identifier = identifier
        self._password = password
        self._epic = epic
        self._base = _DEMO_BASE if demo else _LIVE_BASE
        self._connect()

    # ── PriceFeed interface ───────────────────────────────────────────────────

    def get_candles(self) -> MultiTimeframeCandles:
        return {
            TF_H4: self._fetch("HOUR_4", 200),
            TF_H1: self._fetch("HOUR",   200),
        }

    def get_plan_b_candles(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Return (h1_df, m15_df) as pandas DataFrames for Plan B."""
        h1_list  = self._fetch("HOUR",      100)
        m15_list = self._fetch("MINUTE_15", 100)
        return self._to_df(h1_list), self._to_df(m15_list)

    @staticmethod
    def _to_df(candles: list) -> pd.DataFrame:
        if not candles:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        return pd.DataFrame([
            {"open": c.open, "high": c.high, "low": c.low,
             "close": c.close, "volume": c.volume}
            for c in candles
        ])

    # ── Internal ──────────────────────────────────────────────────────────────

    def _connect(self) -> None:
        cls = CapitalComFeed
        with cls._session_lock:
            if not cls._cst:
                self._login_locked()
            if not cls._keepalive_started:
                cls._keepalive_started = True
                t = threading.Thread(target=self._keepalive, daemon=True,
                                     name="capital-keepalive")
                t.start()
        logger.info("CapitalComFeed: ready (epic=%s, shared session)", self._epic)

    def _login_locked(self) -> None:
        """
        Create a session. Caller must hold _session_lock.
        Retries with backoff on 429 (session creation is limited to 1/s) and
        transient network errors; fails fast on bad credentials (400/401/403).
        """
        cls = CapitalComFeed
        last_exc: Exception | None = None
        for attempt in range(1, _LOGIN_RETRIES + 1):
            try:
                r = _req.post(
                    f"{self._base}/session",
                    headers={"X-CAP-API-KEY": self._api_key,
                             "Content-Type": "application/json"},
                    json={"identifier": self._identifier,
                          "password": self._password,
                          "encryptedPassword": False},
                    timeout=_TIMEOUT,
                )
                if r.status_code in (400, 401, 403):
                    raise RuntimeError(
                        f"Capital.com rejected the credentials "
                        f"(HTTP {r.status_code}): {r.text[:200]}"
                    )
                r.raise_for_status()
                cls._cst = r.headers["CST"]
                cls._security_token = r.headers["X-SECURITY-TOKEN"]
                logger.info("CapitalComFeed: session created (attempt %d)", attempt)
                return
            except RuntimeError:
                raise                      # bad credentials — retrying won't help
            except Exception as exc:       # 429 / network — back off and retry
                last_exc = exc
                wait = min(60, 2 ** attempt) + random.uniform(0, 2)
                logger.warning(
                    "CapitalComFeed: login attempt %d failed (%s) — retrying in %.1fs",
                    attempt, exc, wait,
                )
                time.sleep(wait)
        raise RuntimeError(
            f"Capital.com login failed after {_LOGIN_RETRIES} attempts: {last_exc}"
        )

    def _reauth(self, stale_cst: str) -> None:
        """Re-login once when a request 401s. The stale-token check stops
        several instruments from stampeding the session endpoint at once."""
        cls = CapitalComFeed
        with cls._session_lock:
            if cls._cst == stale_cst:      # nobody re-logged-in yet
                self._login_locked()

    def _keepalive(self) -> None:
        while True:
            time.sleep(_PING_INTERVAL)
            try:
                self._request("GET", "/ping")
            except Exception as exc:
                logger.warning("CapitalComFeed keepalive failed: %s", exc)

    def _auth_headers(self) -> dict:
        cls = CapitalComFeed
        with cls._session_lock:
            return {"CST": cls._cst, "X-SECURITY-TOKEN": cls._security_token,
                    "Content-Type": "application/json"}

    def _request(self, method: str, path: str, **kwargs) -> _req.Response:
        headers = self._auth_headers()
        r = _req.request(method, f"{self._base}{path}",
                         headers=headers, timeout=_TIMEOUT, **kwargs)
        if r.status_code == 401:
            self._reauth(headers["CST"])
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
