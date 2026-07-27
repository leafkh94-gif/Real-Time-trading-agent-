"""
Synthetic tests for journal outcome resolution.

These are the regression net for `journal.resolve()`. The status-transition
cases (1-10) predate the excursion instrumentation and must keep passing
byte-for-byte; the excursion cases (11+) cover MFE/MAE and the live-mode
replay semantics.
"""
from __future__ import annotations

import datetime as dt
import json
import os

import pandas as pd
import pytest

from strategy import journal

T0 = dt.datetime(2026, 7, 15, 14, 0)
_ISO = "%Y-%m-%dT%H:%M:%S"


def bars(specs):
    """specs: list of (hours_after_T0, low, high). Close/open sit mid-range."""
    return pd.DataFrame([{
        "time": T0 + dt.timedelta(hours=h),
        "open": (lo + hi) / 2, "high": hi, "low": lo, "close": (lo + hi) / 2,
        "volume": 100.0,
    } for h, lo, hi in specs])


def entry(direction="buy", status="pending", expiry_h=8):
    """Buy: entry 100, SL 95 (risk 5), TP1 110 (+2R), TP2 115 (+3R)."""
    e = {"id": "t", "epic": "US500", "direction": direction, "pattern": "sweep_bos",
         "tier": "A+", "score": 85,
         "entry": 100.0, "stop_loss": 95.0, "take_profit": 110.0, "take_profit2": 115.0,
         "alert_utc": T0.strftime(_ISO),
         "expiry_utc": (T0 + dt.timedelta(hours=expiry_h)).strftime(_ISO),
         "status": status, "filled_utc": None, "resolved_utc": None}
    if direction == "sell":
        e.update({"entry": 100.0, "stop_loss": 105.0,
                  "take_profit": 90.0, "take_profit2": 85.0})
    return e


# ── 1-10: status transitions (must never change) ─────────────────────────────

def test_01_fill_then_tp1():
    e = journal.resolve(entry(), bars([(1, 101, 103), (2, 99.5, 102), (3, 104, 111)]))
    assert e["status"] == "tp1_hit"
    assert e["filled_utc"] is not None


def test_02_fill_then_tp2():
    e = journal.resolve(entry(), bars([(1, 99.5, 102), (2, 105, 116)]))
    assert e["status"] == "tp2_hit"


def test_03_sl_first_within_one_candle():
    """A candle touching both SL and TP1 resolves as a loss (conservative)."""
    e = journal.resolve(entry(), bars([(1, 99.5, 102), (2, 94, 111)]))
    assert e["status"] == "sl_hit"


def test_04_expiry_without_fill():
    e = journal.resolve(entry(expiry_h=3),
                        bars([(1, 101, 103), (2, 102, 104), (4, 103, 105)]))
    assert e["status"] == "expired"


def test_05_pre_alert_candles_never_fill():
    """The structural level is usually touched just before the alert."""
    e = journal.resolve(entry(), bars([(-2, 90, 101), (1, 101, 103)]))
    assert e["status"] == "pending"


def test_06_fill_and_sl_same_candle():
    e = journal.resolve(entry(), bars([(1, 94.5, 102)]))
    assert e["status"] == "sl_hit"


def test_07_sell_direction():
    e = journal.resolve(entry("sell"), bars([(1, 98, 100.5), (2, 89, 95)]))
    assert e["status"] == "tp1_hit"


def test_08_stats_aggregation():
    rows = [entry(status=s) for s in
            ("tp1_hit", "tp2_hit", "sl_hit", "expired", "pending", "filled")]
    o = journal.stats(rows)["overall"]
    assert o["total"] == 6
    assert o["wins"] == 2
    assert o["losses"] == 1
    assert o["expired"] == 1
    assert o["open"] == 2
    assert o["win_rate"] == round(2 / 3, 2)


def test_09_record_and_update_round_trip(tmp_path, monkeypatch):
    jf = tmp_path / "journal.json"
    monkeypatch.setattr(journal, "JOURNAL_FILE", str(jf))

    class FakeSig:
        epic = "US500"; direction = "buy"; pattern = "sweep_bos"
        tier = "A+"; score = 85
        entry = 100.0; stop_loss = 95.0
        take_profit = 110.0; take_profit2 = 115.0
        expiry_utc = T0 + dt.timedelta(hours=8)
        rr = 2.0
        components: dict = {}
        reasons: list = []
        context: dict = {}

    journal.record_signal(FakeSig(), T0)
    changed = journal.update_outcomes(
        lambda epic: bars([(1, 99.5, 102), (2, 104, 111)]), T0)
    assert changed == 1
    assert json.loads(jf.read_text())[0]["status"] == "tp1_hit"


def test_10_engine_tolerates_time_column():
    """The feed adds a 'time' column; the scoring engine must ignore it."""
    import numpy as np
    from strategy.scoring_strategy import ScoringStrategy, MarketData
    np.random.seed(3)
    n = 200
    close = 5000 + np.cumsum(np.random.normal(0.3, 8, n))
    df = pd.DataFrame({
        "time": [T0 + dt.timedelta(hours=i) for i in range(n)],
        "open": close, "high": close + 5, "low": close - 5, "close": close,
        "volume": np.random.randint(100, 999, n).astype(float),
    })
    ScoringStrategy("US500").evaluate(MarketData(
        epic="US500", h1=df, daily=df.iloc[::4].reset_index(drop=True), now_utc=T0))
    # No exception is the assertion.


# ── 11+: excursion instrumentation ───────────────────────────────────────────

def test_11_mfe_mae_on_a_winner():
    # fill bar 1 (low 99.5), bar 2 runs to 110 = +2R, TP1 hit
    e = journal.resolve(entry(), bars([(1, 99.5, 102), (2, 100, 110)]))
    assert e["status"] == "tp1_hit"
    assert e["mfe_r"] == pytest.approx(2.0)
    assert e["mae_r"] == pytest.approx(0.1)     # low 99.5 on the fill bar
    assert e["bars_to_fill"] == 1
    assert e["r_realized"] == pytest.approx(2.0)


def test_12_same_bar_fill_and_sl_splits_mfe():
    """Intrabar order is unknowable: conservative mfe_r is 0, optimistic isn't."""
    e = journal.resolve(entry(), bars([(1, 94.5, 108)]))
    assert e["status"] == "sl_hit"
    assert e["bars_to_resolve"] == 0
    assert e["mfe_r"] == pytest.approx(0.0)
    assert e["mfe_r_optimistic"] == pytest.approx(1.6)   # (108-100)/5
    assert e["mae_r"] >= 1.0
    assert e["r_realized"] == pytest.approx(-1.0)


def test_13_gap_through_stop_records_overshoot():
    e = journal.resolve(entry(), bars([(1, 99.5, 102), (2, 92.5, 100)]))
    assert e["status"] == "sl_hit"
    assert e["mae_r"] == pytest.approx(1.5)     # (100-92.5)/5


def test_14_sell_mirror_excursions():
    e = journal.resolve(entry("sell"), bars([(1, 98, 100.5), (2, 90, 101)]))
    assert e["status"] == "tp1_hit"
    assert e["mfe_r"] == pytest.approx(2.0)     # (100-90)/5
    assert e["mae_r"] == pytest.approx(0.2)     # (101-100)/5


def test_15_resolve_is_idempotent():
    """Live mode re-resolves the same open entry every scan."""
    df = bars([(1, 99.5, 102), (2, 100, 104)])
    e = entry()
    journal.resolve(e, df)
    first = dict(e)
    journal.resolve(e, df)
    assert e == first


def test_16_incremental_replay_matches_single_pass():
    """The live-mode correctness test: overlapping windows must not double-count."""
    df = bars([(1, 99.5, 102), (2, 100, 104), (3, 101, 106), (4, 100, 111)])
    incremental = entry()
    journal.resolve(incremental, df.iloc[:2])
    journal.resolve(incremental, df)            # overlapping window, as live does
    single = journal.resolve(entry(), df)
    for k in ("status", "mfe_r", "mfe_r_optimistic", "mae_r",
              "bars_since_alert", "bars_to_fill", "bars_to_resolve"):
        assert incremental[k] == single[k], k


def test_17_expired_has_no_excursion():
    e = journal.resolve(entry(expiry_h=3), bars([(1, 101, 103), (4, 103, 105)]))
    assert e["status"] == "expired"
    assert e["mfe_r"] == 0.0 and e["mae_r"] == 0.0
    assert e["bars_to_fill"] is None
    assert e["r_realized"] is None


def test_18_legacy_entry_without_new_fields():
    """Entries written before the schema change must not raise."""
    legacy = entry()
    for k in ("mfe_r", "mae_r", "mfe_r_optimistic", "bars_since_alert"):
        legacy.pop(k, None)
    e = journal.resolve(legacy, bars([(1, 99.5, 102), (2, 100, 111)]))
    assert e["status"] == "tp1_hit"
    assert e["mfe_r"] == pytest.approx(2.2)


def test_19_tp1_and_tp2_in_one_bar_takes_tp2():
    e = journal.resolve(entry(), bars([(1, 99.5, 102), (2, 100, 116)]))
    assert e["status"] == "tp2_hit"
    assert e["r_realized"] == pytest.approx(3.0)


def test_20_zero_risk_entry_does_not_divide_by_zero():
    e = entry()
    e["stop_loss"] = e["entry"]                 # risk == 0
    journal.resolve(e, bars([(1, 99.5, 102), (2, 100, 111)]))
    assert e["mfe_r"] == 0.0 and e["mae_r"] == 0.0


def test_21_missing_bars_do_not_corrupt_excursions():
    """max() is order- and gap-independent."""
    df = bars([(1, 99.5, 102), (5, 100, 108), (9, 100, 111)])
    e = journal.resolve(entry(expiry_h=24), df)
    assert e["status"] == "tp1_hit"
    assert e["mfe_r"] == pytest.approx(2.2)


def test_22_invariants_hold_for_every_terminal_state():
    cases = [
        (bars([(1, 99.5, 102), (2, 94, 100)]), "sl_hit"),
        (bars([(1, 99.5, 102), (2, 100, 111)]), "tp1_hit"),
        (bars([(1, 99.5, 102), (2, 100, 116)]), "tp2_hit"),
    ]
    for df, expected in cases:
        e = journal.resolve(entry(), df)
        assert e["status"] == expected
        if expected == "sl_hit":
            assert e["mae_r"] >= 1.0 - 1e-6
        if expected == "tp1_hit":
            assert e["mfe_r_optimistic"] >= 2.0 - 1e-6
        if expected == "tp2_hit":
            assert e["mfe_r_optimistic"] >= 3.0 - 1e-6
