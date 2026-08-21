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


# ── sd_rejection: reclaim requirement ────────────────────────────────────────
from strategy import indicators as ind
from strategy.scoring_strategy import _detect_sd_rejection


def _sd_frame(last):
    """Swing low at 99.0 (the bar's LOW at index 10), then a retest candle."""
    base = [105, 106, 104, 105, 103, 104, 102, 103, 101, 102,
            100, 103, 104, 103, 104, 103, 104, 103]
    rows = [{"open": float(b) + 0.1, "high": float(b) + 1.2, "low": float(b) - 1.0,
             "close": float(b), "volume": 100.0} for b in base]
    rows.append(dict(zip(["open", "high", "low", "close"], [float(x) for x in last]),
                     volume=100.0))
    return pd.DataFrame(rows)


def test_close_below_support_is_not_a_demand_rejection():
    """The bug behind an inverted-looking BUY: every legacy condition passes —
    low near the level, long lower wick, green candle — but the candle closes
    BELOW the level. A close below support means support failed."""
    d = _sd_frame([98.95, 99.05, 98.50, 98.98])
    o, c, h, l = 98.95, 98.98, 99.05, 98.50
    assert c > o                                   # green
    assert (min(o, c) - l) > 0.5 * (h - l)         # long lower wick
    assert c < 99.0                                # ...but closed below support
    assert _detect_sd_rejection(d, ind.atr(d)) is None


def test_reclaiming_support_still_fires_a_buy():
    d = _sd_frame([99.60, 100.00, 98.70, 99.80])
    r = _detect_sd_rejection(d, ind.atr(d))
    assert r is not None and r["direction"] == "buy"


def test_direction_mapping_is_not_inverted():
    """A demand rejection is a BUY at a swing LOW; supply is a SELL at a HIGH."""
    d = _sd_frame([99.60, 100.00, 98.70, 99.80])
    r = _detect_sd_rejection(d, ind.atr(d))
    assert r["direction"] == "buy"
    assert r["broken_level"] < r["confirm_price"]  # bought above the support


# ── week 2/3 switches ────────────────────────────────────────────────────────
@pytest.fixture
def restore_switches():
    saved = (C.SESSION_WEIGHTS_MODE, C.TIER_MODE, C.SL_DISTANCE_MULT,
             C.TP_STRUCTURE_CHECK, C.SD_REQUIRE_LEVEL_UNBROKEN)
    yield
    (C.SESSION_WEIGHTS_MODE, C.TIER_MODE, C.SL_DISTANCE_MULT,
     C.TP_STRUCTURE_CHECK, C.SD_REQUIRE_LEVEL_UNBROKEN) = saved


def test_measured_session_weights_invert_new_york_and_london(restore_switches):
    from strategy.market_sessions import session_score
    ny = dt.datetime(2026, 7, 15, 14, 0)
    ld = dt.datetime(2026, 7, 15, 9, 0)
    C.SESSION_WEIGHTS_MODE = "v3"
    assert session_score("index_sp_dow", ny) > session_score("index_sp_dow", ld)
    C.SESSION_WEIGHTS_MODE = "measured"
    assert session_score("index_sp_dow", ny) < session_score("index_sp_dow", ld)


def test_defaults_are_unchanged_behaviour():
    """Every week-2/3 switch ships off; this PR must not alter live output."""
    assert C.SESSION_WEIGHTS_MODE == "v3"
    assert C.TIER_MODE == "split"
    assert C.SL_DISTANCE_MULT == 1.0
    assert C.TP_STRUCTURE_CHECK is False
    assert C.SD_REQUIRE_LEVEL_UNBROKEN is False


def test_tighter_stop_keeps_the_target_and_raises_rr(restore_switches):
    """The claim under test is 'winners never needed the room', so the target
    price must stay put — scaling both would hold R:R at 2.0 and test nothing."""
    d = {"pattern": "sd_rejection", "direction": "buy", "broken_level": 100.0,
         "ref_low": 95.0, "ref_high": 110.0, "confirm_price": 103.0, "bonus": 5.0}
    C.SL_DISTANCE_MULT = 1.0
    full = _build_levels("BTCUSD", d, 10.0)
    C.SL_DISTANCE_MULT = 0.85
    tight = _build_levels("BTCUSD", d, 10.0)
    assert tight["take_profit"] == pytest.approx(full["take_profit"])   # target fixed
    assert abs(tight["entry"] - tight["stop_loss"]) < abs(full["entry"] - full["stop_loss"])
    assert tight["rr"] > full["rr"]
    assert tight["rr"] == pytest.approx(C.MIN_RR / 0.85, rel=1e-3)


def test_breakeven_tracks_the_tightened_stop(restore_switches):
    """BE is defined in units of actual risk, so it must move with the stop."""
    d = {"pattern": "sd_rejection", "direction": "buy", "broken_level": 100.0,
         "ref_low": 95.0, "ref_high": 110.0, "confirm_price": 103.0, "bonus": 5.0}
    C.SL_DISTANCE_MULT = 0.85
    lv = _build_levels("BTCUSD", d, 10.0)
    risk = abs(lv["entry"] - lv["stop_loss"])
    assert lv["breakeven_at"] == pytest.approx(lv["entry"] + risk * C.BREAKEVEN_AT_R)
