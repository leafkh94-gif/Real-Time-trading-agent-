"""
Per-trade excursion table — the view the aggregate report hides.

R alone is not actionable: 0.7R is 49 points on one instrument and 210 on
another. These tests pin the points conversion and the filtering.
"""
from __future__ import annotations

import pytest

from strategy import journal


def _e(i, *, status="sl_hit", mfe=0.5, sl=70.0, epic="US500", mfe_present=True):
    e = {
        "id": f"{epic}-{i:03d}", "epic": epic, "pattern": "sd_rejection",
        "direction": "buy", "tier": "A+", "score": 80, "status": status,
        "entry": 5000.0, "stop_loss": 5000.0 - sl,
        "take_profit": 5000.0 + 2 * sl, "take_profit2": 5000.0 + 3 * sl,
        "alert_utc": f"2026-07-{(i % 28) + 1:02d}T14:00:00",
        "sl_distance": sl, "mae_r": 1.0,
    }
    if mfe_present:
        e["mfe_r"] = mfe
    return e


def test_points_conversion():
    rows = journal.trade_rows([_e(1, mfe=0.7, sl=70.0)])
    assert rows[0]["mfe_pts"] == pytest.approx(49.0)
    assert rows[0]["sl_pts"] == pytest.approx(70.0)


def test_same_r_different_points_across_instruments():
    """The whole reason the table exists."""
    rows = journal.trade_rows([_e(1, mfe=0.7, sl=70.0, epic="US500"),
                               _e(2, mfe=0.7, sl=300.0, epic="US30")])
    pts = {r["epic"]: r["mfe_pts"] for r in rows}
    assert pts["US500"] == pytest.approx(49.0)
    assert pts["US30"] == pytest.approx(210.0)


def test_sorted_by_points_descending():
    rows = journal.trade_rows([_e(1, mfe=0.2), _e(2, mfe=1.4), _e(3, mfe=0.8)])
    assert [r["mfe_r"] for r in rows] == [1.4, 0.8, 0.2]


def test_status_filter():
    entries = [_e(1, status="sl_hit"), _e(2, status="tp1_hit"),
               _e(3, status="expired")]
    assert len(journal.trade_rows(entries, statuses={"sl_hit"})) == 1
    assert len(journal.trade_rows(entries, statuses={"tp1_hit", "sl_hit"})) == 2


def test_mfe_range_filter():
    entries = [_e(1, mfe=0.2), _e(2, mfe=0.6), _e(3, mfe=1.2)]
    rows = journal.trade_rows(entries, min_mfe_r=0.5, max_mfe_r=1.0)
    assert [r["mfe_r"] for r in rows] == [0.6]


def test_legacy_entries_skipped_not_zeroed():
    """A schema-1 entry has no MFE; listing it as 0 would be a lie."""
    entries = [_e(1, mfe=0.9), _e(2, mfe_present=False)]
    rows = journal.trade_rows(entries)
    assert len(rows) == 1
    assert rows[0]["mfe_r"] == 0.9


def test_falls_back_to_computed_sl_distance():
    e = _e(1, mfe=0.5, sl=70.0)
    del e["sl_distance"]
    assert journal.trade_rows([e])[0]["sl_pts"] == pytest.approx(70.0)


def test_table_renders_and_is_empty_safe():
    assert "no trades with excursion data" in journal.format_trade_table([])
    out = journal.format_trade_table(journal.trade_rows([_e(1, mfe=0.7)]), "T")
    assert "US500" in out and "49.0" in out and "MFE pts" in out
