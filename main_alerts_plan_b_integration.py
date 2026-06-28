"""
Integration example: wiring Plan B into the existing scan loop.

This is NOT a standalone runnable file - it shows the additions to make
in main_alerts.py (or wherever your scan loop / orchestrator lives) to
run Plan B alongside Plan A (GoldStrategy etc.) for the same 4 instruments.

Plan A is untouched. Plan B alerts are clearly labeled "[Plan B]" so the
two never get confused on Telegram.
"""

from datetime import datetime

from strategy.scalping_config import PLAN_B_CONFIG
from strategy.scalping_strategy import ScalpingStrategy
from core.scalping_signal_manager import ScalpingSignalManager

# These already exist in your codebase (Plan A) - shown here for context.
# from strategy.yahoo_feed import YahooFinanceFeed
# from agents.news_agent import get_high_impact_usd_event_times
# from alerts.notifier import send_telegram_message

INSTRUMENTS = {
    "XAUUSD": "GC=F",
    "US500": "^GSPC",
    "US100": "^NDX",
    "US30": "^DJI",
}

# Module-level singletons - created once when the bot starts
scalping_strategy = ScalpingStrategy(PLAN_B_CONFIG)
scalping_signals = ScalpingSignalManager(PLAN_B_CONFIG)


def run_plan_b_for_instrument(instrument: str, symbol: str, feed) -> None:
    """
    Called once per instrument, once per Plan B scan cycle
    (PLAN_B_SCAN_INTERVAL_S = 300s, i.e. every 5 minutes -
    run this on its own faster loop/thread, separate from
    Plan A's 15-minute loop).
    """
    now_utc = datetime.utcnow()

    # 1. Fetch candles
    h1_df = feed.get_candles(symbol, timeframe="H1", bars=100)
    m15_df = feed.get_candles(symbol, timeframe="M15", bars=100)

    # 2. Time-stop check for any open Plan B trade on this instrument
    last_price = float(m15_df["close"].iloc[-1])
    time_stop_msg = scalping_signals.check_time_stop(instrument, last_price, now_utc)
    if time_stop_msg:
        # send_telegram_message(f"[Plan B] {instrument}\n{time_stop_msg}")
        print(f"[Plan B] {instrument}: {time_stop_msg}")

    # 3. Cooldown check - skip scanning for new signals if locked
    if not scalping_signals.can_alert(instrument, now_utc):
        return

    # 4. News blackout window (reuse Plan A's NewsAgent / ForexFactory calendar)
    # high_impact_news_times = get_high_impact_usd_event_times()
    high_impact_news_times: list[datetime] = []

    # 5. Run the 6 gates
    result = scalping_strategy.run(
        h1_df,
        m15_df,
        now_utc=now_utc,
        high_impact_news_times=high_impact_news_times,
    )

    if result.signal is None:
        # Optional: log result.reason for debugging / journaling
        # print(f"[Plan B] {instrument}: no signal ({result.reason})")
        return

    # 6. Build and send the alert
    message = format_plan_b_alert(instrument, result)
    # send_telegram_message(message)
    print(message)

    # 7. Register the alert (starts cooldown + time-stop tracking)
    scalping_signals.register_alert(
        instrument, result.signal, result.entry, result.tp1, now_utc
    )


def format_plan_b_alert(instrument: str, result) -> str:
    direction_emoji = "\U0001F7E2" if result.signal == "BUY" else "\U0001F534"
    return (
        f"{direction_emoji} [PLAN B] {instrument} - {result.signal} (Scalp)\n"
        f"H1 Bias: {result.h1_bias}\n\n"
        f"Entry:       {result.entry:.2f}\n"
        f"Stop Loss:   {result.stop_loss:.2f}\n"
        f"Take Profit 1: {result.tp1:.2f}\n"
        f"Take Profit 2: {result.tp2:.2f}\n"
        f"R:R Ratio:   1 : {result.rr:.2f}\n"
        f"ATR(M15):    {result.atr_m15:.2f}\n\n"
        f"Time Stop: close/review after 2h if TP1 not hit\n"
        f"Gates passed: {', '.join(result.gates_passed)}\n\n"
        f"Alert only - always confirm before trading."
    )


# ----------------------------------------------------------------------
# Suggested loop wiring (pseudocode):
#
#   async def plan_b_loop():
#       while True:
#           for instrument, symbol in INSTRUMENTS.items():
#               run_plan_b_for_instrument(instrument, symbol, yahoo_feed)
#           await asyncio.sleep(PLAN_B_CONFIG.scan_interval_s)  # 300s = 5 min
#
#   # Run plan_b_loop() as a second task alongside the existing
#   # 15-minute Plan A loop (main_alerts.py).
# ----------------------------------------------------------------------
