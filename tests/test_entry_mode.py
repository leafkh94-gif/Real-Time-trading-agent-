"""
entry_mode: immediate BOS-close entry vs. the original retrace-limit behaviour.

The retrace_limit assertions are deliberately golden-valued — they pin the
existing arithmetic so the new branch cannot silently change it.
"""
from __future__ import annotations

import pytest

from strategy import strategy_config as C
from strategy.scoring_strategy import _build_levels

ATR = 10.0


def det(pattern="sweep_bos", direction="buy", *,
        lvl=100.0, ref_low=95.0, ref_high=110.0, confirm=104.0):
    return {"pattern": pattern, "direction": direction, "broken_level": lvl,
            "ref_low": ref_low, "ref_high": ref_high, "confirm_price": confirm,
            "bonus": 5.0}


@pytest.fixture
def restore_modes():
    """entry_mode is mutated by some tests (and by backtest --entry-mode)."""
    saved = {e: c.get("entry_mode") for e, c in C.INSTRUMENTS.items()}
    yield
    for e, m in saved.items():
        C.INSTRUMENTS[e]["entry_mode"] = m


def test_config_modes_are_valid():
    for epic, cfg in C.INSTRUMENTS.items():
        assert cfg["entry_mode"] in C.ENTRY_MODES, epic


def test_defaults_match_the_documented_choice():
    assert C.INSTRUMENTS["US500"]["entry_mode"] == "bos_close"
    assert C.INSTRUMENTS["US30"]["entry_mode"] == "bos_close"
    assert C.INSTRUMENTS["US100"]["entry_mode"] == "bos_close"
    # BTC is the control group for the A/B — do not flip it casually.
    assert C.INSTRUMENTS["BTCUSD"]["entry_mode"] == "retrace_limit"


# ── bos_close ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("pattern", list(C.PATTERNS))
def test_bos_close_enters_at_confirm_price_for_every_pattern(pattern):
    """The mode is per-instrument, so it must apply to all five patterns."""
    lv = _build_levels("US500", det(pattern, confirm=104.0), ATR)
    assert lv["entry"] == pytest.approx(104.0)
    assert lv["_diag"]["entry_mode"] == "bos_close"
    assert lv["_diag"]["entry_dist_atr"] == pytest.approx(0.0)


def test_bos_close_sell_side():
    d = det("sweep_bos", "sell", lvl=100.0, ref_low=90.0, ref_high=105.0, confirm=96.0)
    lv = _build_levels("US500", d, ATR)
    assert lv["entry"] == pytest.approx(96.0)
    assert lv["stop_loss"] > lv["entry"]
    assert lv["take_profit"] < lv["entry"]


def test_bos_close_keeps_rr_at_min_rr():
    lv = _build_levels("US500", det(), ATR)
    assert lv["rr"] == pytest.approx(C.MIN_RR)


def test_bos_close_stop_still_sits_beyond_structure_when_unclipped():
    """Entry moves up but the stop anchor does not — so the stop must remain
    below the structural low unless the ATR band cut it short."""
    d = det(confirm=101.0)                      # close just above the level
    lv = _build_levels("US500", d, ATR)
    if lv["_diag"]["sl_clip"] == "none":
        assert lv["stop_loss"] <= min(d["ref_low"], d["broken_level"])


def test_bos_close_makes_the_max_clip_more_likely(restore_modes):
    """The documented consequence: a far-away confirmation close pushes the
    structural stop distance past atr_max, so config places the stop."""
    # confirm 140 -> structural distance 140-(95-10) = 55 = 5.5 ATR, past the
    # 4.0 ATR ceiling, so the stop is set to 140-40 = 100 — ABOVE the swing low
    # at 95 that it was supposed to sit beyond.
    d = det(confirm=140.0)
    C.INSTRUMENTS["US500"]["entry_mode"] = "bos_close"
    clipped = _build_levels("US500", d, ATR)
    C.INSTRUMENTS["US500"]["entry_mode"] = "retrace_limit"
    limited = _build_levels("US500", d, ATR)
    assert clipped["_diag"]["sl_clip"] == "max"
    assert limited["_diag"]["sl_clip"] != "max"
    # The documented failure mode: the stop no longer protects the structure.
    assert clipped["stop_loss"] > min(d["ref_low"], d["broken_level"])


# ── retrace_limit (golden values — the old path must not move) ───────────────
def test_retrace_limit_breakout_enters_at_half_retrace():
    # lvl=100, ref_high=110 -> half = 105; |105-100| = 5 <= 1.5*ATR (15)
    lv = _build_levels("BTCUSD", det("sweep_bos", confirm=104.0), ATR)
    assert lv["entry"] == pytest.approx(105.0)
    assert lv["_diag"]["entry_mode"] == "retrace_limit"


def test_retrace_limit_breakout_falls_back_when_retrace_too_deep():
    # ref_high=140 -> half = 120; |120-100| = 20 > 1.5*ATR -> lvl + 0.3*ATR = 103
    d = det("sweep_bos", ref_high=140.0)
    lv = _build_levels("BTCUSD", d, ATR)
    assert lv["entry"] == pytest.approx(103.0)


def test_retrace_limit_rejection_enters_at_structural_level():
    lv = _build_levels("BTCUSD", det("sd_rejection", confirm=104.0), ATR)
    assert lv["entry"] == pytest.approx(100.0)


def test_the_two_modes_differ(restore_modes):
    d = det("sd_rejection", confirm=104.0)
    C.INSTRUMENTS["US500"]["entry_mode"] = "bos_close"
    a = _build_levels("US500", d, ATR)
    C.INSTRUMENTS["US500"]["entry_mode"] = "retrace_limit"
    b = _build_levels("US500", d, ATR)
    assert a["entry"] != b["entry"]
    # Immediate entry is worse priced for a buy, by construction.
    assert a["entry"] > b["entry"]


def test_missing_entry_mode_defaults_to_retrace_limit(restore_modes):
    del C.INSTRUMENTS["US500"]["entry_mode"]
    lv = _build_levels("US500", det("sd_rejection"), ATR)
    assert lv["entry"] == pytest.approx(100.0)
    assert lv["_diag"]["entry_mode"] == "retrace_limit"


def test_entry_mode_reaches_signal_context():
    from tests.test_scoring_components import _signals
    sigs = _signals(seeds=range(6))
    if not sigs:
        pytest.skip("no signals generated")
    for s in sigs:
        assert s.context["entry_mode"] in C.ENTRY_MODES


def test_alert_label_reflects_the_mode():
    from main_alerts import _entry_label
    assert "market" in _entry_label("US500", "sweep_bos")
    assert "limit" in _entry_label("BTCUSD", "sd_rejection")
