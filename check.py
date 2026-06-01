"""
check.py — pre-flight diagnostic for the gold trading bot.

Runs each component in isolation and prints PASS / FAIL for every check.
Safe to run any time — uses PaperBroker only, never touches real money.

Usage:
  python check.py
"""
import os
import sys
import traceback

# ── helpers ───────────────────────────────────────────────────────────────────

_passed = 0
_failed = 0

def ok(label: str, detail: str = ""):
    global _passed
    _passed += 1
    suffix = f"  ({detail})" if detail else ""
    print(f"  [PASS] {label}{suffix}")

def fail(label: str, detail: str = ""):
    global _failed
    _failed += 1
    suffix = f"  ({detail})" if detail else ""
    print(f"  [FAIL] {label}{suffix}")

def section(title: str):
    print(f"\n{'='*52}")
    print(f"  {title}")
    print(f"{'='*52}")

# ── 1. Python version ─────────────────────────────────────────────────────────

section("1. Python environment")

v = sys.version_info
if v >= (3, 10):
    ok("Python version", f"{v.major}.{v.minor}.{v.micro}")
else:
    fail("Python version", f"{v.major}.{v.minor} — need 3.10+")

# ── 2. Required packages ──────────────────────────────────────────────────────

section("2. Required packages")

_packages = [
    ("dotenv",    "python-dotenv"),
    ("boto3",     "boto3"),
    ("anthropic", "anthropic"),
    ("requests",  "requests"),
    ("fastapi",   "fastapi"),
    ("uvicorn",   "uvicorn"),
    ("yfinance",  "yfinance"),
]
for mod, pkg in _packages:
    try:
        __import__(mod)
        ok(pkg)
    except ImportError:
        fail(pkg, f"run: pip install {pkg}")

# ── 3. .env / secrets ─────────────────────────────────────────────────────────

section("3. .env configuration")

try:
    from dotenv import load_dotenv
    found = load_dotenv()
    ok(".env file loaded") if found else fail(".env not found", "copy .env.example to .env")
except Exception as e:
    fail(".env load", str(e))

_env_keys = {
    "ANTHROPIC_API_KEY":  ("Claude signal filter", True),
    "CAPITAL_API_KEY":    ("Capital.com trading", False),
    "CAPITAL_IDENTIFIER": ("Capital.com login",   False),
    "CAPITAL_PASSWORD":   ("Capital.com login",   False),
    "TELEGRAM_BOT_TOKEN": ("Telegram alerts",     False),
    "TELEGRAM_CHAT_ID":   ("Telegram alerts",     False),
    "WEBHOOK_SECRET":     ("TradingView webhook",  False),
}
for key, (purpose, required) in _env_keys.items():
    val = os.getenv(key, "")
    if val:
        masked = val[:6] + "..." + val[-3:] if len(val) > 12 else "***"
        ok(key, f"{purpose} — {masked}")
    elif required:
        fail(key, f"required for {purpose}")
    else:
        print(f"  [SKIP] {key}  (optional — {purpose})")

# ── 4. State store (SQLite) ───────────────────────────────────────────────────

section("4. State store (SQLite)")

try:
    import tempfile
    from pathlib import Path
    from core.state_store import StateStore

    with tempfile.TemporaryDirectory() as tmp:
        store = StateStore(db_path=Path(tmp) / "test.db")
        store.add_trade()
        store.add_pnl(10.0)
        stats = store.get_today()
        assert stats.trades == 1 and stats.pnl == 10.0
        store.add_position("p1", "XAUUSD", 0.05, "buy", 2300.0, "2024-01-01T00:00:00Z")
        assert store.count_open_positions() == 1
        store.remove_position("p1")
        assert store.count_open_positions() == 0
        store.close()
    ok("SQLite read/write/delete")
except Exception as e:
    fail("SQLite", traceback.format_exc(limit=1).strip())

# ── 5. Kill switch ────────────────────────────────────────────────────────────

section("5. Kill switch")

try:
    import tempfile
    from pathlib import Path
    from core.kill_switch import KillSwitch

    with tempfile.TemporaryDirectory() as tmp:
        ks = KillSwitch(kill_file=Path(tmp) / "KILL")
        assert not ks.check()
        ks.trip("test")
        assert ks.check()
        ks.reset()
        assert not ks.check()
    ok("trip / check / reset")
except Exception as e:
    fail("Kill switch", str(e))

# ── 6. Risk guard ─────────────────────────────────────────────────────────────

section("6. Risk guard")

try:
    import tempfile
    from pathlib import Path
    from core.kill_switch import KillSwitch
    from core.state_store import StateStore
    from core.risk_limits import RiskGuard, RiskLimits

    with tempfile.TemporaryDirectory() as tmp:
        store = StateStore(db_path=Path(tmp) / "bot.db")
        ks = KillSwitch(kill_file=Path(tmp) / "KILL")
        guard = RiskGuard(limits=RiskLimits(), store=store, switch=ks)

        ok_flag, reason = guard.can_trade(proposed_lots=0.05, open_positions=0)
        assert ok_flag, reason
        ok("normal trade allowed", f"0.05 lots, 0 positions")

        ok_flag, reason = guard.can_trade(proposed_lots=0.99, open_positions=0)
        assert not ok_flag
        ok("oversized trade rejected", reason)

        ok_flag, reason = guard.can_trade(proposed_lots=0.05, open_positions=1)
        assert not ok_flag
        ok("max-position limit enforced", reason)

        store.close()
except Exception as e:
    fail("Risk guard", traceback.format_exc(limit=2).strip())

# ── 7. Strategy (paper cycles) ────────────────────────────────────────────────

section("7. Strategy — 20 paper cycles")

try:
    from strategy.feed import RandomWalkFeed
    from strategy.gold_strategy import GoldStrategy
    from strategy.signal_filter import MLSignalFilter

    feed = RandomWalkFeed(seed=1)
    strat = GoldStrategy(signal_filter=MLSignalFilter())  # passthrough — no API needed
    signals = 0
    for i in range(20):
        candles = feed.get_candles()
        sig = strat.evaluate(candles)
        if sig:
            signals += 1

    ok("strategy ran 20 cycles", f"{signals} signal(s) generated")
    if signals == 0:
        print("         (0 signals is normal — random-walk data rarely triggers all gates)")
except Exception as e:
    fail("Strategy", traceback.format_exc(limit=2).strip())

# ── 8. Paper broker — full round-trip ─────────────────────────────────────────

section("8. Paper broker — full round-trip trade")

try:
    import tempfile
    from pathlib import Path
    from unittest.mock import MagicMock
    from core.kill_switch import KillSwitch
    from core.state_store import StateStore
    from core.risk_limits import RiskGuard, RiskLimits
    from alerts.notifier import NullNotifier
    from execution.paper_broker import PaperBroker
    from execution.models import Signal

    with tempfile.TemporaryDirectory() as tmp:
        store = StateStore(db_path=Path(tmp) / "bot.db")
        ks    = KillSwitch(kill_file=Path(tmp) / "KILL")
        guard = RiskGuard(limits=RiskLimits(), store=store, switch=ks)
        note  = NullNotifier()

        broker = PaperBroker(
            guard=guard, switch=ks, store=store, notifier=note,
            simulated_price=2300.0,
        )
        broker.connect()
        broker.reconcile()

        sig   = Signal(direction="buy", lots=0.05)
        order = broker.place_order(sig)
        ok("buy order placed", f"order_id={order.order_id[:8]}...")

        assert broker.open_position_count() == 1
        ok("open_position_count == 1")

        broker.simulated_price = 2310.0  # move price up $10 before closing
        pnl = broker.close_position(order.order_id)
        ok("position closed", f"pnl={pnl:+.2f} USD")
        assert broker.open_position_count() == 0
        ok("open_position_count back to 0")

        stats = store.get_today()
        assert stats.trades == 1
        ok("daily trade count recorded", f"trades={stats.trades}")
        store.close()
except Exception as e:
    fail("Paper broker", traceback.format_exc(limit=2).strip())

# ── 9. Anthropic API (live) ───────────────────────────────────────────────────

section("9. Anthropic API (live ping)")

api_key = os.getenv("ANTHROPIC_API_KEY", "")
if not api_key:
    print("  [SKIP] No ANTHROPIC_API_KEY — set it in .env to enable Claude signal filter")
else:
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=8,
            messages=[{"role": "user", "content": "Reply: PONG"}],
        )
        reply = resp.content[0].text.strip()
        ok("Claude API reachable", f"response: {reply}")
    except Exception as e:
        fail("Claude API", str(e))

# ── 10. Webhook secret ────────────────────────────────────────────────────────

section("10. Webhook receiver (offline)")

try:
    import hmac, json
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from webhook.receiver import router, _seen

    _seen.clear()
    _app = FastAPI()
    _app.include_router(router)
    client_wh = TestClient(_app)

    secret = "diag-secret-123"
    os.environ["WEBHOOK_SECRET"] = secret

    r = client_wh.post("/webhook", json={"secret": secret, "action": "buy", "size": 0.05})
    assert r.status_code == 200, f"HTTP {r.status_code}"
    ok("POST /webhook — valid secret accepted")

    r = client_wh.post("/webhook", json={"secret": "wrong", "action": "buy"})
    assert r.status_code == 401
    ok("POST /webhook — wrong secret rejected (401)")

except ImportError:
    print("  [SKIP] fastapi not installed — run: pip install fastapi httpx")
except Exception as e:
    fail("Webhook receiver", traceback.format_exc(limit=2).strip())

# ── 11. Alert bot imports ─────────────────────────────────────────────────────

section("11. Alert bot (import check)")

try:
    import importlib
    mod = importlib.import_module("main_alerts")
    assert hasattr(mod, "WATCHLIST"), "WATCHLIST missing"
    assert hasattr(mod, "main"), "main() missing"
    assert len(mod.WATCHLIST) >= 4, f"expected ≥4 instruments, got {len(mod.WATCHLIST)}"
    epics = [i.epic for i in mod.WATCHLIST]
    ok("main_alerts imports OK", f"watching {', '.join(epics)}")

    from strategy.yahoo_feed import YahooFinanceFeed, TICKER_MAP
    assert "GOLD" in TICKER_MAP and "US500" in TICKER_MAP
    ok("YahooFinanceFeed imports OK", f"tickers: {', '.join(TICKER_MAP.values())}")
except Exception as e:
    fail("main_alerts / yahoo_feed", traceback.format_exc(limit=2).strip())

# ── Summary ───────────────────────────────────────────────────────────────────

print(f"\n{'='*52}")
print(f"  RESULTS: {_passed} passed  |  {_failed} failed")
print(f"{'='*52}\n")

if _failed:
    print("Fix the [FAIL] items above, then re-run: python check.py\n")
    sys.exit(1)
else:
    print("All checks passed. The bot is ready.\n")
