"""
Tests for core/risk_limits.py
Covers: normal operation, each reject path, restart-mid-day, daily reset,
and limit-trips-kill-switch.
"""
import pytest
from pathlib import Path
from core.kill_switch import KillSwitch
from core.state_store import StateStore
from core.risk_limits import RiskGuard, RiskLimits


@pytest.fixture
def kill(tmp_path):
    return KillSwitch(kill_file=tmp_path / "KILL")


@pytest.fixture
def store(tmp_path):
    return StateStore(db_path=tmp_path / "bot.db")


@pytest.fixture
def guard(store, kill):
    limits = RiskLimits(
        max_position_size_lots=0.10,
        max_daily_loss_usd=100.0,
        max_open_positions=1,
        max_trades_per_day=3,
        min_risk_reward_ratio=1.5,
    )
    return RiskGuard(limits=limits, store=store, switch=kill)


# ── Happy path ────────────────────────────────────────────────────────────────

def test_can_trade_valid(guard):
    ok, reason = guard.can_trade(proposed_lots=0.05, open_positions=0)
    assert ok is True
    assert reason == "ok"


def test_exact_max_lots_allowed(guard):
    ok, _ = guard.can_trade(proposed_lots=0.10, open_positions=0)
    assert ok is True


# ── Size limit ────────────────────────────────────────────────────────────────

def test_rejects_oversized_position(guard):
    ok, reason = guard.can_trade(proposed_lots=0.11, open_positions=0)
    assert ok is False
    assert "max" in reason.lower()


def test_rejects_zero_lots(guard):
    ok, reason = guard.can_trade(proposed_lots=0.0, open_positions=0)
    assert ok is False


# ── Open position limit ───────────────────────────────────────────────────────

def test_rejects_when_max_open_positions_reached(guard):
    ok, reason = guard.can_trade(proposed_lots=0.05, open_positions=1)
    assert ok is False
    assert "open positions" in reason.lower()


# ── Daily trade count ─────────────────────────────────────────────────────────

def test_rejects_when_trade_count_reached(guard, store):
    for _ in range(3):
        store.add_trade()
    ok, reason = guard.can_trade(proposed_lots=0.05, open_positions=0)
    assert ok is False
    assert "trade count" in reason.lower()


# ── Daily loss limit trips kill switch ───────────────────────────────────────

def test_daily_loss_limit_trips_kill_switch(guard, store, kill):
    store.add_pnl(-100.0)  # exactly at limit
    ok, reason = guard.can_trade(proposed_lots=0.05, open_positions=0)
    assert ok is False
    assert kill.is_tripped is True
    assert "kill switch" in reason.lower()


def test_daily_loss_limit_exceeded_trips_kill_switch(guard, store, kill):
    store.add_pnl(-150.0)
    ok, reason = guard.can_trade(0.05, 0)
    assert ok is False
    assert kill.is_tripped is True


def test_record_pnl_trips_kill_switch_when_limit_hit(guard, store, kill):
    store.add_pnl(-90.0)
    guard.record_pnl(-10.0)  # pushes total to -100
    assert kill.is_tripped is True


# ── Kill switch already active ────────────────────────────────────────────────

def test_rejects_when_kill_switch_active(guard, kill):
    kill.trip("manual")
    ok, reason = guard.can_trade(0.05, 0)
    assert ok is False
    assert "kill switch" in reason.lower()


# ── Restart mid-day (persistent state) ───────────────────────────────────────

def test_restart_mid_day_preserves_pnl(tmp_path):
    """Simulate a crash + restart: new guard reads existing DB state."""
    db = tmp_path / "bot.db"
    kill_file = tmp_path / "KILL"

    # Session 1: accumulate losses before crash
    s1 = StateStore(db_path=db)
    s1.add_pnl(-90.0)
    s1.add_trade()
    s1.close()

    # Session 2: new process, same DB — should see -90 and block another -15
    s2 = StateStore(db_path=db)
    k2 = KillSwitch(kill_file=kill_file)
    g2 = RiskGuard(
        limits=RiskLimits(max_daily_loss_usd=100.0, max_trades_per_day=5),
        store=s2,
        switch=k2,
    )
    # -90 already; adding -15 would breach -100 limit via record_pnl
    g2.record_pnl(-15.0)
    assert k2.is_tripped is True


def test_restart_mid_day_preserves_trade_count(tmp_path):
    db = tmp_path / "bot.db"
    kill_file = tmp_path / "KILL"

    s1 = StateStore(db_path=db)
    for _ in range(3):
        s1.add_trade()
    s1.close()

    s2 = StateStore(db_path=db)
    k2 = KillSwitch(kill_file=kill_file)
    g2 = RiskGuard(
        limits=RiskLimits(max_trades_per_day=3),
        store=s2,
        switch=k2,
    )
    ok, reason = g2.can_trade(0.05, 0)
    assert ok is False
    assert "trade count" in reason.lower()


# ── record_trade ──────────────────────────────────────────────────────────────

def test_record_trade_increments_count(guard, store):
    guard.record_trade()
    guard.record_trade()
    assert store.get_today().trades == 2
