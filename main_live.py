"""
Production entry point — FastAPI webhook server + Capital.com broker.

Architecture:
  TradingView alert → POST /webhook → trade_queue → trade_worker → CapitalComBroker

Start (with TLS, required for TradingView webhooks):
  uvicorn main_live:app --host 0.0.0.0 --port 443 \\
      --ssl-keyfile /etc/ssl/private/key.pem \\
      --ssl-certfile /etc/ssl/certs/cert.pem

Stop:
  echo "reason" > state/KILL   (or send SIGTERM / SIGINT)
"""
import asyncio
import logging
import os
import signal as _signal
from contextlib import asynccontextmanager

from fastapi import FastAPI

from alerts.notifier import build_notifier
from config import secrets
from config.settings import settings
from core.kill_switch import kill_switch
from core.log_sanitizer import setup_logging
from core.risk_limits import RiskGuard, RiskLimits
from core.state_store import state_store
from execution.capital_broker import CapitalComBroker
from main import attempt_trade
from webhook.receiver import router as _webhook_router, trade_queue

logger = logging.getLogger("main_live")
_bg_tasks: set[asyncio.Task] = set()


async def _trade_worker(broker: CapitalComBroker, guard: RiskGuard, notifier) -> None:
    """Drain trade_queue, executing each signal via attempt_trade() in a thread."""
    while True:
        signal = await trade_queue.get()
        try:
            await asyncio.to_thread(attempt_trade, signal, broker, guard, notifier)
        except Exception as exc:
            logger.error("trade worker error: %s", exc, exc_info=True)
        finally:
            trade_queue.task_done()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    setup_logging(log_dir=settings.logs_dir)
    logger.info("gold bot (live) starting (env=%s)", os.getenv("ENVIRONMENT", "development"))

    notifier = build_notifier()
    limits = RiskLimits()
    guard = RiskGuard(limits=limits, store=state_store, switch=kill_switch)

    demo = os.getenv("ENVIRONMENT", "development") != "production"
    broker = CapitalComBroker(
        api_key=secrets.get("CAPITAL_API_KEY"),
        identifier=secrets.get("CAPITAL_IDENTIFIER"),
        password=secrets.get("CAPITAL_PASSWORD"),
        demo=demo,
        guard=guard,
        switch=kill_switch,
        store=state_store,
        notifier=notifier,
    )

    try:
        await asyncio.to_thread(broker.connect)
        await asyncio.to_thread(broker.reconcile)
        notifier.send("gold bot (live) started — awaiting TradingView signals")
        logger.info("startup complete — listening for webhooks")
    except Exception as exc:
        logger.critical("startup failed: %s", exc)
        notifier.send(f"startup failed: {exc}")
        raise

    # Graceful shutdown on SIGTERM / SIGINT
    loop = asyncio.get_running_loop()

    def _shutdown(signum, frame):
        logger.info("shutdown signal received (sig=%s)", signum)
        loop.call_soon_threadsafe(kill_switch.trip, "SIGTERM received")

    _signal.signal(_signal.SIGTERM, _shutdown)
    _signal.signal(_signal.SIGINT, _shutdown)

    task = asyncio.create_task(_trade_worker(broker, guard, notifier))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)

    yield   # ── app runs here ─────────────────────────────────────────────────

    task.cancel()
    kill_switch.trip("shutdown")
    notifier.send("gold bot (live) stopped")
    logger.info("shutdown complete")


app = FastAPI(title="Gold Trading Bot", lifespan=_lifespan)
app.include_router(_webhook_router)
