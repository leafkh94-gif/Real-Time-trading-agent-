"""
Tests for main.py — attempt_trade() gate sequence.
The broker and strategy are mocked so these tests isolate loop logic only.
"""
import pytest
from unittest.mock import MagicMock, patch

from core.kill_switch import KillSwitch
from core.risk_limits import RiskGuard, RiskLimits
from core.state_store import StateStore
from alerts.notifier import NullNotifier
from execution.models import Signal
from execution.paper_broker import PaperBroker
from main import attempt_trade


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def kill(tmp_path):
    return KillSwitch(kill_file=tmp_path / "KILL")


@pytest.fixture
def store(tmp_path):
    return StateStore(db_path=tmp_path / "bot.db")


@pytest.fixture
def guard(store, kill):
    return RiskGuard(
        limits=RiskLimits(
            max_position_size_lots=0.10,
            max_daily_loss_usd=100.0,
            max_open_positions=1,
            max_trades_per_day=5,
        ),
        store=store,
        switch=kill,
    )


@pytest.fixture
def notifier():
    return MagicMock(spec=NullNotifier)


@pytest.fixture
def broker(guard, kill, store, notifier, monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    return PaperBroker(
        guard=guard, switch=kill, store=store, notifier=notifier,
        simulated_price=2300.0,
    )


@pytest.fixture
def sig():
    return Signal(direction="buy", lots=0.05)


# ── Happy path ────────────────────────────────────────────────────────────────

def test_attempt_trade_succeeds(broker, guard, notifier, sig, kill):
    result = attempt_trade(sig, broker, guard, notifier)
    assert result is True
    assert broker.open_position_count() == 1


def test_attempt_trade_increments_trade_count(broker, guard, notifier, sig, store):
    attempt_trade(sig, broker, guard, notifier)
    assert store.get_today().trades == 1


# ── Gate: kill switch ─────────────────────────────────────────────────────────

def test_attempt_trade_returns_false_when_kill_switch_active(broker, guard, notifier, sig, kill):
    kill.trip("manual")
    result = attempt_trade(sig, broker, guard, notifier)
    assert result is False
    assert broker.open_position_count() == 0


def test_kill_switch_gate_does_not_alert(broker, guard, notifier, sig, kill):
    kill.trip("manual")
    attempt_trade(sig, broker, guard, notifier)
    notifier.send.assert_not_called()


# ── Gate: risk guard ──────────────────────────────────────────────────────────

def test_attempt_trade_returns_false_on_oversized_lots(broker, guard, notifier, kill):
    result = attempt_trade(Signal("buy", lots=0.50), broker, guard, notifier)
    assert result is False
    assert broker.open_position_count() == 0


def test_attempt_trade_returns_false_on_max_open_positions(broker, guard, notifier, sig):
    broker.place_order(sig)  # fills the 1-position limit
    result = attempt_trade(sig, broker, guard, notifier)
    assert result is False


def test_attempt_trade_returns_false_on_daily_loss_limit(broker, guard, notifier, sig, store, kill):
    store.add_pnl(-100.0)
    result = attempt_trade(sig, broker, guard, notifier)
    assert result is False
    assert kill.is_tripped is True


def test_attempt_trade_returns_false_on_trade_count_limit(broker, guard, notifier, sig, store):
    for _ in range(5):
        store.add_trade()
    result = attempt_trade(sig, broker, guard, notifier)
    assert result is False


# ── Gate: broker runtime error ────────────────────────────────────────────────

def test_attempt_trade_handles_broker_runtime_error(guard, notifier, sig, kill):
    """If the broker raises (e.g. network error), attempt_trade returns False and alerts."""
    bad_broker = MagicMock()
    bad_broker.open_position_count.return_value = 0
    bad_broker.place_order.side_effect = RuntimeError("connection refused")

    result = attempt_trade(sig, bad_broker, guard, notifier)
    assert result is False
    notifier.send.assert_called_once()
    assert "order error" in notifier.send.call_args[0][0].lower()


# ── Loop gate ordering ────────────────────────────────────────────────────────

def test_kill_switch_checked_before_broker(broker, guard, notifier, sig, kill):
    """When kill switch is active the broker must never be called."""
    kill.trip("manual")
    with patch.object(broker, "place_order") as mock_order:
        attempt_trade(sig, broker, guard, notifier)
        mock_order.assert_not_called()


def test_risk_guard_checked_before_broker(broker, guard, notifier):
    """Risk guard must be checked before the broker is called."""
    big_signal = Signal("buy", lots=0.99)
    with patch.object(broker, "place_order", wraps=broker.place_order) as mock_order:
        attempt_trade(big_signal, broker, guard, notifier)
        mock_order.assert_not_called()  # rejected by guard before reaching broker


# ── No side effects on rejection ──────────────────────────────────────────────

def test_rejected_trade_does_not_record_in_state(broker, guard, notifier, sig, store, kill):
    kill.trip("manual")
    attempt_trade(sig, broker, guard, notifier)
    assert store.get_today().trades == 0
    assert store.count_open_positions() == 0
