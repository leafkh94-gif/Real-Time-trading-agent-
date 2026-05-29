"""
XAU/USD gold trading bot — main event loop.

Loop order (spec section 5.8 / Phase 3):
  kill_switch.check()
    → feed.get_candles()
    → strategy.evaluate()
    → attempt_trade()  [kill_switch + risk_guard checked again here — belt-and-suspenders]
    → broker.place_order()
    → alert

Start:  python main.py
Stop:   echo "reason" > state/KILL   (from any terminal or SSH session)
"""
import logging
import os
import signal as _signal
import time

from alerts.notifier import Notifier, build_notifier
from config.settings import settings
from core.kill_switch import kill_switch
from core.log_sanitizer import setup_logging
from core.risk_limits import RiskGuard, RiskLimits
from core.state_store import state_store
from execution.models import Signal
from execution.paper_broker import PaperBroker
from strategy.feed import PriceFeed, RandomWalkFeed
from strategy.gold_strategy import GoldStrategy

logger = logging.getLogger("main")


def attempt_trade(
    sig: Signal,
    broker: PaperBroker,
    guard: RiskGuard,
    notifier: Notifier,
) -> bool:
    """
    Outer pre-trade gate. Returns True if an order was placed, False otherwise.

    Belt-and-suspenders design: the guard is checked here AND inside
    broker.place_order() — both layers are intentional so the main loop
    gets an early-exit without catching RuntimeError on every loop tick.
    """
    if kill_switch.check():
        logger.warning("kill switch active — skipping signal")
        return False

    ok, reason = guard.can_trade(
        proposed_lots=sig.lots,
        open_positions=broker.open_position_count(),
    )
    if not ok:
        logger.warning("trade rejected: %s", reason)
        return False

    try:
        broker.place_order(sig)
        return True
    except RuntimeError as exc:
        logger.error("order failed: %s", exc)
        notifier.send(f"order error: {exc}")
        return False


def _build_broker(guard: RiskGuard, notifier: Notifier) -> PaperBroker:
    return PaperBroker(
        guard=guard,
        switch=kill_switch,
        store=state_store,
        notifier=notifier,
    )


def main(
    feed: PriceFeed | None = None,
    strategy: GoldStrategy | None = None,
    broker: PaperBroker | None = None,
) -> None:
    """
    Entry point. Parameters are injectable for integration testing;
    production callers pass nothing and let the defaults build.
    """
    setup_logging(log_dir=settings.logs_dir)
    logger.info("gold bot starting (env=%s)", os.getenv("ENVIRONMENT", "development"))

    notifier = build_notifier()
    limits = RiskLimits()
    guard = RiskGuard(limits=limits, store=state_store, switch=kill_switch)

    feed = feed or RandomWalkFeed()
    strategy = strategy or GoldStrategy(lots=limits.max_position_size_lots)
    broker = broker or _build_broker(guard, notifier)

    # ── Startup ───────────────────────────────────────────────────────────────
    try:
        broker.connect()
        broker.reconcile()
        logger.info("startup reconcile complete")
        notifier.send("gold bot started — paper trading mode")
    except Exception as exc:
        logger.critical("startup failed: %s", exc)
        notifier.send(f"startup failed: {exc}")
        raise

    # Graceful shutdown on SIGTERM / SIGINT — trips the kill switch so the
    # loop exits cleanly on the next iteration rather than mid-operation.
    def _shutdown(signum, frame):
        logger.info("shutdown signal received (sig=%s)", signum)
        kill_switch.trip("SIGTERM received")

    _signal.signal(_signal.SIGTERM, _shutdown)
    _signal.signal(_signal.SIGINT, _shutdown)

    # ── Main loop ─────────────────────────────────────────────────────────────
    while True:
        if kill_switch.check():
            logger.warning("kill switch active — reason: %s", kill_switch.reason)
            notifier.send(f"bot stopped: {kill_switch.reason}")
            break

        try:
            candles = feed.get_candles()
            sig = strategy.evaluate(candles)
            if sig is not None:
                attempt_trade(sig, broker, guard, notifier)
        except Exception as exc:
            logger.error("loop error: %s", exc, exc_info=True)
            notifier.send(f"loop error: {exc}")

        time.sleep(settings.loop_interval_seconds)


if __name__ == "__main__":
    main()
