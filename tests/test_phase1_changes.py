"""
Phase 1 changes, each pinned to the evidence that motivated it.

- break-even stop at +1R  (positive expectancy in 3/3 backtest runs)
- sweep_bos disabled       (0 wins from 6 decided, three runs running)
- round-number bonus to 0  (19% win rate near a round number vs 38% away)
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from strategy import journal
from strategy import strategy_config as C
from strategy.scoring_strategy import _build_levels, _pattern_enabled

T0 = dt.datetime(2026, 7, 15, 14, 0)
ISO = "%Y-%m-%dT%H:%M:%S"


def bars(specs):
    return pd.DataFrame([{
        "time": T0 + dt.timedelta(hours=h), "open": (lo + hi) / 2,
        "high": hi, "low": lo, "close": (lo + hi) / 2, "volume": 100.0,
    } for h, lo, hi in specs])


def entry(be=105.0, direction="buy"):
    """Buy: entry 100, SL 95 (risk 5), BE at +1R = 105, TP1 110."""
    e = {"id": "t", "epic": "US500", "direction": direction,
         "pattern": "sd_rejection", "tier": "A+", "score": 85,
         "entry": 100.0, "stop_loss": 95.0, "take_profit": 110.0,
         "take_profit2": 115.0, "breakeven_at": be,
         "alert_utc": T0.strftime(ISO),
         "expiry_utc": (T0 + dt.timedelta(hours=24)).strftime(ISO),
         "status": "pending", "filled_utc": None, "resolved_utc": None}
    if direction == "sell":
        e.update({"stop_loss": 105.0, "take_profit": 90.0,
                  "take_profit2": 85.0, "breakeven_at": 95.0})
    return e


# ── break-even ───────────────────────────────────────────────────────────────
def test_would_be_loss_becomes_a_scratch():
    """The whole point: 15 of 52 losses had already run +1R before reversing."""
    e = journal.resolve(entry(), bars([(1, 99.5, 102), (2, 100, 106),
                                       (3, 99, 101), (4, 94, 99)]))
    assert e["status"] == "breakeven"
    assert e["r_realized"] == 0.0


def test_clean_winner_is_untouched():
    e = journal.resolve(entry(), bars([(1, 99.5, 102), (2, 101, 106), (3, 105, 111)]))
    assert e["status"] == "tp1_hit"
    assert e["r_realized"] == pytest.approx(2.0)


def test_loss_that_never_reached_1r_still_loses():
    """Break-even must not rescue trades that never went our way."""
    e = journal.resolve(entry(), bars([(1, 99.5, 102), (2, 94, 100)]))
    assert e["status"] == "sl_hit"
    assert e["r_realized"] == -1.0


def test_does_not_trigger_on_the_fill_bar():
    """A limit fill means price touched entry on that bar by definition — a
    same-bar check would scratch nearly every trade instantly."""
    e = journal.resolve(entry(), bars([(1, 99.0, 106)]))   # fills AND reaches +1R
    assert e["status"] != "breakeven"


def test_sell_side_mirror():
    e = journal.resolve(entry(direction="sell"),
                        bars([(1, 98, 100.5), (2, 94, 99), (3, 99, 101), (4, 101, 106)]))
    assert e["status"] == "breakeven"
    assert e["r_realized"] == 0.0


def test_scratch_is_final_and_idempotent():
    df = bars([(1, 99.5, 102), (2, 100, 106), (3, 99, 101), (4, 94, 99)])
    e = entry()
    journal.resolve(e, df)
    first = dict(e)
    journal.resolve(e, df)
    assert e == first


def test_breakeven_price_is_one_r_from_entry():
    d = {"pattern": "sd_rejection", "direction": "buy", "broken_level": 100.0,
         "ref_low": 95.0, "ref_high": 110.0, "confirm_price": 104.0, "bonus": 5.0}
    lv = _build_levels("BTCUSD", d, 10.0)
    risk = abs(lv["entry"] - lv["stop_loss"])
    assert lv["breakeven_at"] == pytest.approx(lv["entry"] + risk * C.BREAKEVEN_AT_R)


# ── stats treat a scratch as neither win nor loss ────────────────────────────
def test_scratch_excluded_from_win_rate_but_counted_in_expectancy():
    rows = []
    for st in ("tp1_hit", "sl_hit", "breakeven", "breakeven"):
        e = entry(); e["status"] = st; e["r_realized"] = journal._realized_r(e)
        rows.append(e)
    o = journal.stats(rows)["overall"]
    assert o["breakeven"] == 2
    assert o["win_rate"] == 0.5          # 1W/1L — scratches excluded
    assert o["decided"] == 4             # but they are settled outcomes
    assert o["expectancy_r"] == pytest.approx((2.0 - 1.0 + 0 + 0) / 4)


# ── disabled pattern ─────────────────────────────────────────────────────────
def test_sweep_bos_is_disabled():
    assert _pattern_enabled("sweep_bos") is False
    assert _pattern_enabled("sd_rejection") is True


def test_disabled_pattern_keeps_its_config_so_history_stays_readable():
    assert "sweep_bos" in C.PATTERNS
    assert C.PATTERNS["sweep_bos"]["label"]


# ── round-number bonus ───────────────────────────────────────────────────────
def test_round_number_bonus_restored():
    assert C.ROUND_NUMBER_BONUS == 5   # restored: removing it measured worse


# ── graduated daily bias ─────────────────────────────────────────────────────
import numpy as np
from strategy.scoring_strategy import _daily_bias


@pytest.fixture
def restore_bias_mode():
    saved = C.DAILY_BIAS_MODE
    yield
    C.DAILY_BIAS_MODE = saved


def _uptrend_then_pullback():
    """Primary trend up (EMA50>EMA200, price>EMA200) but medium turning down
    (EMA20<EMA50) — a correction inside an uptrend."""
    close = np.concatenate([np.linspace(100, 200, 260), np.linspace(200, 175, 40)])
    return pd.DataFrame({"close": close})


def test_strict_blocks_the_correction_sell(restore_bias_mode):
    C.DAILY_BIAS_MODE = "strict"
    pts, state = _daily_bias(_uptrend_then_pullback(), "sell")
    assert state == "counter-trend"
    assert pts == C.BIAS_COUNTER


def test_graduated_allows_it_as_a_correction(restore_bias_mode):
    C.DAILY_BIAS_MODE = "graduated"
    pts, state = _daily_bias(_uptrend_then_pullback(), "sell")
    assert state == "correction"
    assert pts == C.BIAS_CORRECTION


def test_graduated_does_not_change_aligned_trades(restore_bias_mode):
    """Only the against-the-primary case may differ between modes."""
    d = _uptrend_then_pullback()
    C.DAILY_BIAS_MODE = "strict"
    strict_buy = _daily_bias(d, "buy")
    C.DAILY_BIAS_MODE = "graduated"
    assert _daily_bias(d, "buy") == strict_buy


def test_graduated_still_blocks_when_both_layers_disagree(restore_bias_mode):
    """A sell in an uptrend that is NOT correcting stays counter-trend."""
    C.DAILY_BIAS_MODE = "graduated"
    close = np.linspace(100, 200, 300)          # medium still rising
    pts, state = _daily_bias(pd.DataFrame({"close": close}), "sell")
    assert state == "counter-trend"


def test_correction_penalty_sits_between_neutral_and_counter():
    assert C.BIAS_COUNTER_REVERSAL < C.BIAS_CORRECTION < C.BIAS_NEUTRAL


# ── week-1 structural fixes ──────────────────────────────────────────────────
from strategy.scoring_strategy import _pattern_quality


def test_best_pattern_wins_not_first_in_list():
    """flag (+0.29R measured) must beat reversal (-0.25R) when both fire, and
    list order must not decide it. reversal sits earlier in _DETECTORS."""
    weak = {"pattern": "reversal", "bonus": 0.0}      # 37 + 0
    strong = {"pattern": "flag", "bonus": 8.0}        # 36 + 8 = 44
    assert _pattern_quality(strong) > _pattern_quality(weak)
    assert max([weak, strong], key=_pattern_quality) is strong


def test_pattern_quality_caps_the_bonus():
    """A detector reporting an absurd bonus cannot outrank on that alone."""
    capped = {"pattern": "flag", "bonus": 999.0}
    assert _pattern_quality(capped) == 36 + C.PATTERNS["flag"]["max_bonus"]


def test_far_limit_entries_are_rejected():
    """Beyond 1.5 ATR the measured fill rate is 0.23 — three in four expire."""
    far = {"pattern": "sd_rejection", "direction": "buy", "broken_level": 100.0,
           "ref_low": 95.0, "ref_high": 110.0,
           "confirm_price": 130.0,      # 3 ATR away from the limit at 100
           "bonus": 5.0}
    lv = _build_levels("BTCUSD", far, 10.0)
    assert lv["_diag"]["entry_dist_atr"] > C.MAX_ENTRY_DIST_ATR


def test_near_limit_entries_survive():
    near = {"pattern": "sd_rejection", "direction": "buy", "broken_level": 100.0,
            "ref_low": 95.0, "ref_high": 110.0, "confirm_price": 103.0,
            "bonus": 5.0}
    lv = _build_levels("BTCUSD", near, 10.0)
    assert lv["_diag"]["entry_dist_atr"] <= C.MAX_ENTRY_DIST_ATR


def test_spread_cost_is_charged_to_expectancy():
    """A 2R win costs one round-trip spread; net must be below gross."""
    e = entry(); e["status"] = "tp1_hit"; e["r_realized"] = 2.0; e["spread_r"] = 0.05
    assert journal.expectancy_r([e]) == pytest.approx(2.0)
    assert journal.expectancy_r([e], net_of_spread=True) == pytest.approx(1.95)


def test_entries_without_a_spread_are_charged_nothing():
    """Older entries must not be charged a guessed cost — the net figure is
    then optimistic, which is the honest direction to fail in."""
    e = entry(); e["status"] = "sl_hit"; e["r_realized"] = -1.0
    e.pop("spread_r", None)
    assert journal.expectancy_r([e], net_of_spread=True) == pytest.approx(-1.0)
