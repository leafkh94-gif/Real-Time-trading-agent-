"""
Capital.com REST API broker adapter.

Session lifecycle:
  connect()         → POST /session (CST + X-SECURITY-TOKEN)
  _keepalive_loop() → GET /ping every 8 min (session dies after 10 min idle)
  _request()        → auto-reauth on 401

Order lifecycle:
  _submit_order()   → POST /positions → GET /confirms/{dealReference}
  close_position()  → POST /positions/{dealId} + _method:DELETE header

Rate limits (Capital.com):
  ≤ 10 req/s global; ≥ 0.1 s between consecutive order submissions.
"""
import logging
import threading
import time
from datetime import datetime, timezone

import requests as _req

from execution.broker import BrokerAdapter
from execution.models import Order, Signal

logger = logging.getLogger(__name__)

_DEMO_BASE = "https://demo-api-capital.backend-capital.com/api/v1"
_LIVE_BASE = "https://api-capital.backend-capital.com/api/v1"
_PING_INTERVAL = 8 * 60   # seconds; session expires after 10 min idle
_ORDER_MIN_GAP = 0.11     # ≥ 0.1 s between consecutive order submissions
_TIMEOUT = 15             # HTTP request timeout in seconds


class CapitalComBroker(BrokerAdapter):
    """
    Live/demo broker adapter for Capital.com.
    All safety gates are inherited from BrokerAdapter and cannot be bypassed.
    """

    def __init__(self, api_key: str, identifier: str, password: str, demo: bool = True, **kw):
        super().__init__(**kw)
        self._api_key = api_key
        self._identifier = identifier
        self._password = password
        self._base = _DEMO_BASE if demo else _LIVE_BASE
        self._is_demo = demo

        self._cst: str = ""
        self._security_token: str = ""
        self._token_lock = threading.Lock()
        self._last_order_at: float = 0.0

    # ── Public interface ──────────────────────────────────────────────────────

    def connect(self) -> None:
        self._login()
        t = threading.Thread(target=self._keepalive_loop, daemon=True, name="cap-keepalive")
        t.start()
        logger.info("CapitalComBroker: connected (demo=%s)", self._is_demo)

    def reconcile(self) -> list[Order]:
        data = self._request("GET", "/positions").json()
        positions = data.get("positions", [])
        orders: list[Order] = []
        for p in positions:
            pos = p["position"]
            mkt = p["market"]
            self._store.add_position(
                position_id=pos["dealId"],
                symbol=mkt.get("epic", "GOLD"),
                lots=float(pos["dealSize"]),
                direction=pos["direction"].lower(),
                open_price=float(pos["openLevel"]),
                opened_at=pos.get("createdDateUTC", datetime.now(timezone.utc).isoformat()),
            )
            orders.append(Order(
                order_id=pos["dealId"],
                symbol=mkt.get("epic", "GOLD"),
                direction=pos["direction"].lower(),
                lots=float(pos["dealSize"]),
                price=float(pos["openLevel"]),
                opened_at=pos.get("createdDateUTC", ""),
            ))
        logger.info("CapitalComBroker: reconciled %d open positions", len(orders))
        return orders

    def close_position(self, position_id: str) -> float:
        # Capital.com DELETE-body bug workaround: POST + _method:DELETE header
        r = self._request("POST", f"/positions/{position_id}", extra_headers={"_method": "DELETE"})
        deal_ref = r.json().get("dealReference", "")
        pnl = 0.0
        if deal_ref:
            confirm = self._request("GET", f"/confirms/{deal_ref}").json()
            pnl = float(confirm.get("profit", 0.0) or 0.0)
        self._store.remove_position(position_id)
        self._store.add_pnl(pnl)
        self._guard.record_pnl(pnl)
        logger.info("CapitalComBroker: closed %s pnl=%.2f", position_id, pnl)
        return pnl

    def open_position_count(self) -> int:
        try:
            data = self._request("GET", "/positions").json()
            return len(data.get("positions", []))
        except Exception as exc:
            logger.warning("open_position_count: broker unavailable, using state_store: %s", exc)
            return self._store.count_open_positions()

    # ── BrokerAdapter._submit_order ───────────────────────────────────────────

    def _submit_order(self, signal: Signal) -> Order:
        # Enforce ≥ 0.1 s between consecutive order submissions (Capital.com limit)
        elapsed = time.monotonic() - self._last_order_at
        if elapsed < _ORDER_MIN_GAP:
            time.sleep(_ORDER_MIN_GAP - elapsed)

        body: dict = {
            "epic": "GOLD",
            "direction": signal.direction.upper(),
            "size": signal.lots,
        }
        if signal.stop_loss is not None:
            body["stopLevel"] = signal.stop_loss
        if signal.take_profit is not None:
            body["profitLevel"] = signal.take_profit

        resp = self._request("POST", "/positions", json=body)
        self._last_order_at = time.monotonic()

        deal_ref = resp.json()["dealReference"]
        confirm = self._request("GET", f"/confirms/{deal_ref}").json()

        if confirm.get("dealStatus") != "ACCEPTED":
            raise RuntimeError(
                f"deal not accepted: {confirm.get('dealStatus')} — {confirm.get('reason', '')}"
            )

        return Order(
            order_id=confirm["dealId"],
            symbol=signal.symbol,
            direction=signal.direction,
            lots=signal.lots,
            price=float(confirm["level"]),
            opened_at=datetime.now(timezone.utc).isoformat(),
        )

    # ── Session management ────────────────────────────────────────────────────

    def _login(self) -> None:
        r = _req.post(
            f"{self._base}/session",
            headers={"X-CAP-API-KEY": self._api_key, "Content-Type": "application/json"},
            json={"identifier": self._identifier, "password": self._password,
                  "encryptedPassword": False},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        with self._token_lock:
            self._cst = r.headers["CST"]
            self._security_token = r.headers["X-SECURITY-TOKEN"]

    def _keepalive_loop(self) -> None:
        while True:
            time.sleep(_PING_INTERVAL)
            try:
                self._request("GET", "/ping")
                logger.debug("CapitalComBroker: keepalive OK")
            except Exception as exc:
                logger.warning("CapitalComBroker: keepalive error — %s", exc)

    def _auth_headers(self) -> dict[str, str]:
        with self._token_lock:
            return {
                "CST": self._cst,
                "X-SECURITY-TOKEN": self._security_token,
                "Content-Type": "application/json",
            }

    def _request(
        self,
        method: str,
        path: str,
        *,
        extra_headers: dict | None = None,
        **kwargs,
    ) -> _req.Response:
        headers = {**self._auth_headers(), **(extra_headers or {})}
        r = _req.request(method, f"{self._base}{path}", headers=headers, timeout=_TIMEOUT, **kwargs)
        if r.status_code == 401:
            logger.info("CapitalComBroker: session expired — re-authenticating")
            self._login()
            headers = {**self._auth_headers(), **(extra_headers or {})}
            r = _req.request(method, f"{self._base}{path}", headers=headers, timeout=_TIMEOUT, **kwargs)
        r.raise_for_status()
        return r
