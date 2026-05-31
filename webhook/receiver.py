"""
TradingView webhook receiver (FastAPI router).

TradingView fires POST /webhook the instant an alert triggers.
The handler validates the shared secret, converts the payload to a
Signal, and enqueues it — returning HTTP 200 immediately to beat
TradingView's strict 3-second server timeout.

Expected TradingView alert message body (JSON):
  {
    "secret": "{{your shared secret}}",
    "action": "buy",          // or "sell"
    "id":     "{{timenow}}", // optional — used for idempotency
    "size":   0.05            // optional — falls back to DEFAULT_LOTS
  }

Idempotency: if an `id` field is present, duplicate IDs within
DEDUP_WINDOW seconds are silently acknowledged and dropped.
"""
import asyncio
import hmac
import json
import logging
import os
import time

from fastapi import APIRouter, HTTPException, Request

from execution.models import Signal

logger = logging.getLogger(__name__)

router = APIRouter()
trade_queue: asyncio.Queue = asyncio.Queue()

_DEFAULT_LOTS = 0.05
_DEDUP_WINDOW = 60.0          # seconds to retain seen webhook IDs
_seen: dict[str, float] = {}  # id → monotonic timestamp


def _purge_stale() -> None:
    cutoff = time.monotonic() - _DEDUP_WINDOW
    stale = [k for k, v in _seen.items() if v < cutoff]
    for k in stale:
        del _seen[k]


def build_signal(data: dict) -> Signal:
    """Extract direction + size from a TradingView payload → Signal."""
    action = str(data.get("action", "")).lower()
    if action not in ("buy", "sell"):
        raise ValueError(f"action must be 'buy' or 'sell', got {action!r}")
    lots = float(data.get("size", _DEFAULT_LOTS))
    return Signal(direction=action, lots=lots)


@router.post("/webhook")
async def webhook(request: Request):
    # Parse body
    raw = await request.body()
    try:
        data = json.loads(raw)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")

    # Validate shared secret (constant-time comparison)
    expected = os.getenv("WEBHOOK_SECRET", "")
    if not expected:
        logger.error("WEBHOOK_SECRET is not set — rejecting all webhooks")
        raise HTTPException(status_code=503, detail="server misconfigured")
    if not hmac.compare_digest(str(data.get("secret", "")), expected):
        raise HTTPException(status_code=401, detail="unauthorized")

    # Idempotency check
    webhook_id = str(data.get("id", ""))
    if webhook_id:
        _purge_stale()
        if webhook_id in _seen:
            logger.info("webhook: duplicate id=%r — dropped", webhook_id)
            return {"status": "duplicate"}
        _seen[webhook_id] = time.monotonic()

    # Build and enqueue signal
    try:
        signal = build_signal(data)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    await trade_queue.put(signal)
    logger.info("webhook: enqueued %s %.2f lots", signal.direction, signal.lots)
    return {"status": "ok"}
