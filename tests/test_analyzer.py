"""
The analyzer must find a planted signal when the sample is large enough, and
must refuse to report one when it isn't. Both directions matter: a tool that
only ever says "insufficient data" is useless, and one that finds patterns in
five trades is worse than useless.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

import analyze_journal as A

T0 = dt.datetime(2026, 6, 1, 12, 0)


def _entry(i, *, status, bias="aligned-up", mfe=0.2, mae=1.0, pattern="reversal"):
    risk = 5.0
    r = {"tp1_hit": 2.0, "tp2_hit": 3.0, "sl_hit": -1.0}.get(status)
    return {
        "id": f"E{i}", "epic": "US500", "direction": "buy", "pattern": pattern,
        "tier": "A+", "score": 80, "status": status,
        "entry": 100.0, "stop_loss": 100.0 - risk,
        "take_profit": 110.0, "take_profit2": 115.0,
        "alert_utc": (T0 + dt.timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%S"),
        "expiry_utc": None, "filled_utc": None, "resolved_utc": None,
        "schema": 2, "sl_distance": risk, "sl_distance_atr": 2.0,
        "entry_dist_atr": 0.3,
        "components": {"pattern": 40.0, "confirmation": 10, "daily_bias": 15,
                       "session": 10, "additional": 5, "round_number": 5,
                       "volume_confirm": 0, "anchored_vwap": 0,
                       "volume_profile": 0, "choppy": 0},
        "reasons": [],
        "context": {"bias_state": bias, "adx": 25.0, "atr": 2.5,
                    "atr_pct": 0.0005, "session_label": "new_york",
                    "avwap_state": "aligned", "vp_state": "aligned",
                    "confirm_count": 3, "sl_clip": "none",
                    "raw_sl_dist_atr": 2.0, "hour_utc": 14, "weekday": 2},
        "mfe_r": mfe, "mae_r": mae, "mfe_r_optimistic": mfe,
        "bars_to_fill": 1, "bars_to_resolve": 3, "r_realized": r,
    }


def test_planted_signal_is_found_at_large_n(capsys):
    """Counter-trend trades all lose, aligned trades all win, n=120."""
    entries = []
    for i in range(60):
        entries.append(_entry(i, status="sl_hit", bias="counter-trend"))
    for i in range(60, 120):
        entries.append(_entry(i, status="tp1_hit", bias="aligned-up", mfe=2.0, mae=0.3))
    df = A.normalize(entries)
    A.section_dimensions(df, min_n=20)
    out = capsys.readouterr().out
    assert "FINDINGS" in out
    assert "ctx_bias_state=counter-trend" in out
    assert "ctx_bias_state=aligned-up" in out


def test_small_sample_reports_nothing(capsys):
    """The same planted signal at n=8 must NOT be promoted to a finding."""
    entries = [_entry(i, status="sl_hit", bias="counter-trend") for i in range(5)]
    entries += [_entry(i, status="tp1_hit", bias="aligned-up", mfe=2.0)
                for i in range(5, 8)]
    df = A.normalize(entries)
    A.section_dimensions(df, min_n=20)
    out = capsys.readouterr().out
    assert "FINDINGS: none" in out
    assert "INSUFFICIENT DATA" in out


def test_verdict_selection_problem(capsys):
    """Losses that never moved in our favour -> selection problem."""
    entries = [_entry(i, status="sl_hit", mfe=0.1) for i in range(40)]
    A.section_mfe(A.normalize(entries), min_n=20)
    out = capsys.readouterr().out
    assert "SELECTION problem" in out
    assert "HYPOTHESIS" not in out


def test_verdict_exit_problem(capsys):
    """Losses that reached +1R first -> exit problem."""
    entries = [_entry(i, status="sl_hit", mfe=1.4) for i in range(40)]
    A.section_mfe(A.normalize(entries), min_n=20)
    out = capsys.readouterr().out
    assert "EXIT problem" in out


def test_verdict_is_flagged_as_hypothesis_when_small(capsys):
    entries = [_entry(i, status="sl_hit", mfe=0.1) for i in range(5)]
    A.section_mfe(A.normalize(entries), min_n=20)
    out = capsys.readouterr().out
    assert "HYPOTHESIS" in out


def test_legacy_entries_are_excluded_not_zeroed(capsys):
    """Schema-1 entries have no mfe_r; they must not read as 'went nowhere'."""
    legacy = _entry(1, status="sl_hit")
    for k in ("mfe_r", "mae_r", "mfe_r_optimistic", "context", "components"):
        legacy.pop(k, None)
    df = A.normalize([legacy])
    assert df["mfe_r"].isna().all()
    A.section_mfe(df, min_n=20)
    assert "No losses with MFE data" in capsys.readouterr().out


def test_selftest_catches_a_broken_invariant(capsys):
    bad = _entry(1, status="sl_hit", mae=0.4)      # sl_hit must have mae >= 1
    assert A.selftest(A.normalize([bad])) > 0
    assert "FAIL" in capsys.readouterr().out


def test_selftest_passes_on_consistent_data(capsys):
    ok = [_entry(i, status="sl_hit", mae=1.0) for i in range(5)]
    ok += [_entry(i, status="tp1_hit", mfe=2.1, mae=0.2) for i in range(5, 10)]
    assert A.selftest(A.normalize(ok)) == 0


def test_counterfactual_breakeven_scratches_qualifying_losses(capsys):
    """A loss that reached +1R becomes a scratch under a break-even stop."""
    entries = [_entry(i, status="sl_hit", mfe=1.5) for i in range(10)]
    A.section_counterfactuals(A.normalize(entries))
    out = capsys.readouterr().out
    assert "10 losses scratched" in out
    assert "baseline" in out
