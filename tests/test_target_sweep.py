"""
The target sweep answers one complaint directly: "the trades never even come
close to the target." It re-prices the SAME recorded trades under the SAME stop
and the SAME break-even rule, moving only where TP1 sits.

A trade reached R multiple T if and only if mfe_r >= T — excursion tracking
stops when the trade closes, so any recorded maximum was reached before the
stop or the scratch. The arithmetic must therefore be exact, not approximate.
"""
from __future__ import annotations

import io
import contextlib

import pandas as pd
import pytest

import analyze_journal as A
from strategy import strategy_config as C


def _frame(mfes, outcome="loss"):
    """Minimal settled rows carrying only what the sweep reads."""
    return pd.DataFrame({
        "mfe_r": mfes,
        "settled": [True] * len(mfes),
        "outcome": [outcome] * len(mfes),
        "bars_to_resolve": [3.0] * len(mfes),
    })


def _run(df):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        A.section_targets(df)
    return buf.getvalue()


def _row(text, target):
    for line in text.splitlines():
        if line.strip().startswith(f"{target:.2f}R"):
            return line
    raise AssertionError(f"no row for {target}R in:\n{text}")


def test_a_trade_counts_as_a_hit_exactly_at_the_target(monkeypatch):
    """Boundary: mfe_r == T is a hit. Off-by-one here would misprice every row."""
    monkeypatch.setattr(C, "BREAKEVEN_ENABLED", False)
    out = _run(_frame([1.0, 0.999]))
    assert "      1" in _row(out, 1.0), _row(out, 1.0)   # exactly one hit
    assert "      0" in _row(out, 1.25)


def test_lower_targets_are_hit_at_least_as_often(monkeypatch):
    monkeypatch.setattr(C, "BREAKEVEN_ENABLED", False)
    out = _run(_frame([0.3, 0.8, 1.1, 1.6, 2.4]))
    hits = []
    for t in (0.75, 1.0, 1.25, 1.5, 2.0, 2.5):
        hits.append(int(_row(out, t).split()[1]))
    assert hits == sorted(hits, reverse=True), hits
    assert hits[0] == 4 and hits[-1] == 0      # >=0.75R: 4 of 5;  >=2.5R: none


def test_break_even_cannot_rescue_a_target_below_its_arming_level(monkeypatch):
    """A target at or under the break-even level closes the trade before the
    stop is ever moved, so the scratch column must be empty there."""
    monkeypatch.setattr(C, "BREAKEVEN_ENABLED", True)
    monkeypatch.setattr(C, "BREAKEVEN_AT_R", 1.0)
    out = _run(_frame([0.2, 1.4, 1.4, 0.9]))
    for t in (0.75, 1.0):
        assert int(_row(out, t).split()[3]) == 0, _row(out, t)
    # Above the arming level the two 1.4R runners scratch instead of losing.
    assert int(_row(out, 2.0).split()[3]) == 2, _row(out, 2.0)


def test_totals_match_hand_arithmetic(monkeypatch):
    """3 trades reach 1.2R, 2 reach nothing. At TP1=1.0R that is
    3 wins x 1.0 - 2 losses x 1.0 = +1.0R."""
    monkeypatch.setattr(C, "BREAKEVEN_ENABLED", False)
    out = _run(_frame([1.2, 1.2, 1.2, 0.1, 0.0]))
    row = _row(out, 1.0).split()
    assert int(row[1]) == 3 and int(row[4]) == 2
    assert row[5] == "+1.0R", row


def test_reports_how_long_trades_currently_take():
    out = _run(_frame([0.5, 2.1]))
    assert "time to resolve" in out
    assert "resolved within 2 hours" in out


def test_survives_a_frame_with_no_excursion_data():
    df = pd.DataFrame({"mfe_r": [None, None], "settled": [True, True],
                       "outcome": ["loss", "loss"], "bars_to_resolve": [1.0, 1.0]})
    assert "no settled trades" in _run(df)
