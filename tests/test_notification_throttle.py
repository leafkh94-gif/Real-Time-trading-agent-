"""
Notification timing must survive the self-chain handoff.

The bot exits every 90 minutes (MAX_RUNTIME_S) so the journal artifact
uploads. Anything that tracks "when did I last notify" in a module global
resets on every handoff, turning a per-6h message into a per-90min one — which
is exactly what happened to the startup banner in production.
"""
from __future__ import annotations

import time

import main_alerts as M


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send_html(self, html):
        self.sent.append(html)


class FakeInstrument:
    def __init__(self, name="S&P 500", last_alert=0.0):
        self.name = name
        self._last_alert = last_alert


def test_startup_interval_is_longer_than_a_run():
    """Otherwise every chained handoff announces itself."""
    assert M.STARTUP_NOTIFY_INTERVAL_S > 90 * 60


def test_startup_timestamp_is_persisted_state_not_a_global():
    s = M.BotState()
    assert hasattr(s, "last_start_notify")
    assert hasattr(s, "last_heartbeat")


def test_state_roundtrip_keeps_notification_timestamps(tmp_path, monkeypatch):
    """If these don't survive the file, the throttle is useless."""
    monkeypatch.setattr(M, "STATE_FILE", str(tmp_path / "state.json"))
    s = M.BotState()
    s.last_start_notify = 1_700_000_000.0
    s.last_heartbeat = 1_700_000_500.0
    M._save_state(s)
    loaded = M._load_state(M.logging.getLogger("t"))
    assert loaded.last_start_notify == 1_700_000_000.0
    assert loaded.last_heartbeat == 1_700_000_500.0


def test_heartbeat_suppressed_when_recently_sent():
    n = FakeNotifier()
    state = M.BotState()
    state.last_heartbeat = time.time() - 60          # a minute ago
    M._maybe_send_heartbeat(n, [FakeInstrument()], M.logging.getLogger("t"), state)
    assert n.sent == []


def test_heartbeat_sent_when_quiet_for_a_day():
    n = FakeNotifier()
    state = M.BotState()
    state.last_heartbeat = time.time() - (M.HEARTBEAT_INTERVAL_S + 60)
    M._maybe_send_heartbeat(n, [FakeInstrument()], M.logging.getLogger("t"), state)
    assert len(n.sent) == 1
    assert "check-in" in n.sent[0]
    assert state.last_heartbeat > time.time() - 5    # timestamp advanced


def test_heartbeat_skipped_when_an_alert_fired_recently():
    """A real alert already proves the bot is alive."""
    n = FakeNotifier()
    state = M.BotState()
    state.last_heartbeat = time.time() - (M.HEARTBEAT_INTERVAL_S + 60)
    recent = FakeInstrument(last_alert=time.time() - 60)
    M._maybe_send_heartbeat(n, [recent], M.logging.getLogger("t"), state)
    assert n.sent == []
    assert state.last_heartbeat > time.time() - 5    # still marked, not retried


def test_repeated_handoffs_send_one_heartbeat_not_many():
    """Simulate a full quiet day of 90-minute restarts."""
    n = FakeNotifier()
    state = M.BotState()                              # carried across restarts
    state.last_heartbeat = time.time() - (M.HEARTBEAT_INTERVAL_S + 60)
    for _ in range(16):                               # 24h / 90min
        M._maybe_send_heartbeat(n, [FakeInstrument()], M.logging.getLogger("t"), state)
    assert len(n.sent) == 1


# ── Rejection funnel persistence ──────────────────────────────────────────────
# The funnel is subject to exactly the trap this file exists for: counters held
# on the strategy objects reset on every 90-minute handoff, so a daily check-in
# would report a fraction of the day and read as a quiet market.
class _FakeStrategy:
    def __init__(self, funnel):
        self.funnel = dict(funnel)


def test_drain_folds_engine_counters_into_persisted_state():
    import logging
    state = M.BotState()
    strategies = {"US500": _FakeStrategy({"no_pattern": 4, "signal": 1}),
                  "US30":  _FakeStrategy({"no_pattern": 2})}
    M._drain_funnels(strategies, state, logging.getLogger("t"))
    assert state.funnel == {"no_pattern": 6, "signal": 1}
    # Drained, not copied: a second drain with no new activity must not double.
    M._drain_funnels(strategies, state, logging.getLogger("t"))
    assert state.funnel == {"no_pattern": 6, "signal": 1}


def test_drain_accumulates_across_scans():
    import logging
    state = M.BotState()
    log = logging.getLogger("t")
    for _ in range(3):
        M._drain_funnels({"US500": _FakeStrategy({"no_pattern": 5})}, state, log)
    assert state.funnel["no_pattern"] == 15


def test_live_gates_are_drained_too():
    import logging
    state = M.BotState()
    M._LIVE_GATES.clear()
    M._LIVE_GATES["cooldown"] += 3
    M._drain_funnels({}, state, logging.getLogger("t"))
    assert state.funnel == {"cooldown": 3}
    assert not M._LIVE_GATES


def test_day_roll_resets_the_funnel():
    import logging
    state = M.BotState(day="2026-08-20", funnel={"no_pattern": 99})
    state.roll_day("2026-08-21", logging.getLogger("t"))
    assert state.funnel == {}


def test_heartbeat_explains_why_nothing_fired():
    """A bare 'no setups' line is indistinguishable from a broken bot."""
    state = M.BotState(funnel={"no_pattern": 120, "below_threshold": 8, "signal": 0})
    n = FakeNotifier()
    M._maybe_send_heartbeat(n, [FakeInstrument()], _logger(), state)
    assert n.sent, "heartbeat did not fire"
    assert "below_threshold" in n.sent[0]


def _logger():
    import logging
    return logging.getLogger("test")
