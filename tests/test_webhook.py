"""
Tests for the TradingView webhook receiver.
Uses FastAPI TestClient — no real network required.
"""
import asyncio
import json
import os
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from webhook.receiver import router, trade_queue, build_signal, _seen


# ── Test app setup ────────────────────────────────────────────────────────────

_app = FastAPI()
_app.include_router(router)
_SECRET = "test-secret-abc"


@pytest.fixture(autouse=True)
def _clear_state(monkeypatch):
    """Reset module-level dedup state and inject WEBHOOK_SECRET for every test."""
    _seen.clear()
    # Drain the queue
    while not trade_queue.empty():
        try:
            trade_queue.get_nowait()
        except Exception:
            break
    monkeypatch.setenv("WEBHOOK_SECRET", _SECRET)
    yield


@pytest.fixture
def client():
    return TestClient(_app)


def _payload(**overrides) -> dict:
    base = {"secret": _SECRET, "action": "buy", "size": 0.05}
    base.update(overrides)
    return base


# ── build_signal ──────────────────────────────────────────────────────────────

def test_build_signal_buy():
    sig = build_signal({"action": "buy", "size": 0.05})
    assert sig.direction == "buy"
    assert sig.lots == 0.05


def test_build_signal_sell():
    sig = build_signal({"action": "sell"})
    assert sig.direction == "sell"


def test_build_signal_invalid_action_raises():
    with pytest.raises(ValueError):
        build_signal({"action": "hold"})


def test_build_signal_defaults_lots():
    sig = build_signal({"action": "buy"})
    assert sig.lots > 0


# ── POST /webhook happy path ──────────────────────────────────────────────────

def test_valid_request_returns_ok(client):
    r = client.post("/webhook", json=_payload())
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_signal_enqueued_on_valid_request(client):
    client.post("/webhook", json=_payload(action="sell", size=0.10))
    assert not trade_queue.empty()
    sig = trade_queue.get_nowait()
    assert sig.direction == "sell"
    assert sig.lots == 0.10


# ── Secret validation ─────────────────────────────────────────────────────────

def test_wrong_secret_returns_401(client):
    r = client.post("/webhook", json=_payload(secret="wrong"))
    assert r.status_code == 401


def test_missing_secret_returns_401(client):
    payload = _payload()
    del payload["secret"]
    r = client.post("/webhook", json=payload)
    assert r.status_code == 401


def test_no_webhook_secret_env_returns_503(client, monkeypatch):
    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)
    r = client.post("/webhook", json=_payload())
    assert r.status_code == 503


# ── Input validation ──────────────────────────────────────────────────────────

def test_bad_json_returns_400(client):
    r = client.post("/webhook", content=b"not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_invalid_action_returns_422(client):
    r = client.post("/webhook", json=_payload(action="hold"))
    assert r.status_code == 422


# ── Idempotency ───────────────────────────────────────────────────────────────

def test_duplicate_id_returns_duplicate_status(client):
    client.post("/webhook", json=_payload(id="tv-001"))
    r = client.post("/webhook", json=_payload(id="tv-001"))
    assert r.status_code == 200
    assert r.json()["status"] == "duplicate"


def test_duplicate_id_not_enqueued_twice(client):
    client.post("/webhook", json=_payload(id="tv-002"))
    client.post("/webhook", json=_payload(id="tv-002"))
    count = 0
    while not trade_queue.empty():
        trade_queue.get_nowait()
        count += 1
    assert count == 1


def test_different_ids_both_enqueued(client):
    client.post("/webhook", json=_payload(id="tv-003"))
    client.post("/webhook", json=_payload(id="tv-004"))
    count = 0
    while not trade_queue.empty():
        trade_queue.get_nowait()
        count += 1
    assert count == 2


def test_no_id_always_enqueued(client):
    """Requests without an id field bypass dedup and always enqueue."""
    client.post("/webhook", json=_payload())
    client.post("/webhook", json=_payload())
    count = 0
    while not trade_queue.empty():
        trade_queue.get_nowait()
        count += 1
    assert count == 2


# ── Telegram notifier helpers ─────────────────────────────────────────────────

def test_escape_html():
    from alerts.notifier import _escape_html
    assert _escape_html("a < b & c > d") == "a &lt; b &amp; c &gt; d"


def test_escape_html_no_special_chars():
    from alerts.notifier import _escape_html
    msg = "opened buy 0.05 lots XAUUSD @ 2345.50"
    assert _escape_html(msg) == msg
