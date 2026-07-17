"""
journal.py — persistent record of every alert and its real outcome.

Every sent alert is appended to JOURNAL_FILE as "pending". On each scan cycle
`update_outcomes` replays the closed H1 candles that formed after the alert and
advances each entry through its lifecycle:

    pending -> filled     (price traded through the limit entry)
    pending -> expired    (expiry passed without a fill)
    filled  -> sl_hit / tp1_hit / tp2_hit

Resolution is conservative: within a single candle the stop-loss is assumed to
be hit before any take-profit. `stats()` aggregates fill/win/loss/expiry rates
overall and per pattern — the evidence base for any future strategy tuning.

The same `resolve()` function is reused by backtest.py so live tracking and
historical simulation share identical fill/TP/SL semantics.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os

import pandas as pd

logger = logging.getLogger(__name__)

JOURNAL_FILE = os.getenv("JOURNAL_FILE", "signal_journal.json")

FINAL_STATES = frozenset({"sl_hit", "tp1_hit", "tp2_hit", "expired"})
WIN_STATES   = frozenset({"tp1_hit", "tp2_hit"})

_ISO = "%Y-%m-%dT%H:%M:%S"


def _load() -> list[dict]:
    try:
        with open(JOURNAL_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save(entries: list[dict]) -> None:
    try:
        with open(JOURNAL_FILE, "w") as f:
            json.dump(entries, f, indent=1)
    except OSError as exc:
        logger.warning("Could not save journal: %s", exc)


def entry_from_signal(sig, now: dt.datetime) -> dict:
    """Build a journal entry dict from a Signal (shared with backtest.py)."""
    return {
        "id":           f"{sig.epic}-{now.strftime('%Y%m%dT%H%M')}",
        "epic":         sig.epic,
        "direction":    sig.direction,
        "pattern":      sig.pattern,
        "tier":         sig.tier,
        "score":        sig.score,
        "entry":        sig.entry,
        "stop_loss":    sig.stop_loss,
        "take_profit":  sig.take_profit,
        "take_profit2": sig.take_profit2,
        "alert_utc":    now.strftime(_ISO),
        "expiry_utc":   sig.expiry_utc.strftime(_ISO) if sig.expiry_utc else None,
        "status":       "pending",
        "filled_utc":   None,
        "resolved_utc": None,
    }


def record_signal(sig, now: dt.datetime) -> None:
    """Append a sent alert to the journal as a pending setup."""
    entries = _load()
    entries.append(entry_from_signal(sig, now))
    _save(entries)


def resolve(entry: dict, candles: pd.DataFrame) -> dict:
    """Advance one journal entry against closed candles (needs a 'time' column).

    Only candles that OPENED at/after the alert are considered — the structural
    level was usually touched shortly before the alert, and counting those bars
    would fake fills. Mutates and returns the entry.
    """
    if entry["status"] in FINAL_STATES or candles is None or not len(candles):
        return entry
    alert_t  = dt.datetime.strptime(entry["alert_utc"], _ISO)
    expiry_t = (dt.datetime.strptime(entry["expiry_utc"], _ISO)
                if entry["expiry_utc"] else None)
    buy = entry["direction"] == "buy"

    after = candles[candles["time"] >= alert_t]
    for _, bar in after.iterrows():
        bar_t = bar["time"]
        if entry["status"] == "pending":
            if expiry_t and bar_t >= expiry_t:
                entry["status"] = "expired"
                entry["resolved_utc"] = bar_t.strftime(_ISO)
                return entry
            touched = (bar["low"] <= entry["entry"]) if buy else (bar["high"] >= entry["entry"])
            if touched:
                entry["status"] = "filled"
                entry["filled_utc"] = bar_t.strftime(_ISO)
        if entry["status"] == "filled":
            hit_sl  = (bar["low"] <= entry["stop_loss"])  if buy else (bar["high"] >= entry["stop_loss"])
            hit_tp2 = (bar["high"] >= entry["take_profit2"]) if buy else (bar["low"] <= entry["take_profit2"])
            hit_tp1 = (bar["high"] >= entry["take_profit"])  if buy else (bar["low"] <= entry["take_profit"])
            if hit_sl:                       # conservative: SL first within a candle
                entry["status"] = "sl_hit"
            elif hit_tp2:
                entry["status"] = "tp2_hit"
            elif hit_tp1:
                entry["status"] = "tp1_hit"
            if entry["status"] in FINAL_STATES:
                entry["resolved_utc"] = bar_t.strftime(_ISO)
                return entry
    return entry


def update_outcomes(get_candles, now: dt.datetime) -> int:
    """Resolve all open journal entries. `get_candles(epic)` must return a
    closed-candle H1 DataFrame with a 'time' column. Returns the number of
    entries that changed state."""
    entries = _load()
    open_entries = [e for e in entries if e["status"] not in FINAL_STATES]
    if not open_entries:
        return 0
    changed = 0
    candles_cache: dict[str, pd.DataFrame] = {}
    for e in open_entries:
        epic = e["epic"]
        if epic not in candles_cache:
            try:
                candles_cache[epic] = get_candles(epic)
            except Exception as exc:
                logger.warning("Journal: could not fetch candles for %s: %s", epic, exc)
                candles_cache[epic] = None
        before = e["status"]
        resolve(e, candles_cache[epic])
        if e["status"] != before:
            changed += 1
            logger.info("Journal: %s %s -> %s", e["id"], before, e["status"])
    if changed:
        _save(entries)
    return changed


def stats(entries: list[dict] | None = None) -> dict:
    """Aggregate outcome statistics, overall and per pattern."""
    if entries is None:
        entries = _load()

    def _bucket(rows: list[dict]) -> dict:
        total   = len(rows)
        filled  = [r for r in rows if r["status"] in {"filled", "sl_hit"} | WIN_STATES]
        wins    = [r for r in rows if r["status"] in WIN_STATES]
        losses  = [r for r in rows if r["status"] == "sl_hit"]
        expired = [r for r in rows if r["status"] == "expired"]
        open_n  = [r for r in rows if r["status"] in ("pending", "filled")]
        decided = len(wins) + len(losses)
        return {
            "total":     total,
            "filled":    len(filled),
            "wins":      len(wins),
            "losses":    len(losses),
            "expired":   len(expired),
            "open":      len(open_n),
            "fill_rate": round(len(filled) / total, 2) if total else None,
            "win_rate":  round(len(wins) / decided, 2) if decided else None,
        }

    out = {"overall": _bucket(entries), "per_pattern": {}}
    for pat in sorted({e["pattern"] for e in entries}):
        out["per_pattern"][pat] = _bucket([e for e in entries if e["pattern"] == pat])
    return out


def format_stats(s: dict) -> str:
    """Human-readable summary (used for the weekly Telegram report)."""
    o = s["overall"]
    lines = [
        f"Signals: {o['total']}  |  filled: {o['filled']}  |  open: {o['open']}",
        f"Wins (TP): {o['wins']}  |  Losses (SL): {o['losses']}  |  expired: {o['expired']}",
        f"Fill rate: {o['fill_rate']}  |  Win rate: {o['win_rate']}",
    ]
    for pat, b in s["per_pattern"].items():
        lines.append(f"• {pat}: {b['total']} signals, {b['wins']}W/{b['losses']}L, win rate {b['win_rate']}")
    return "\n".join(lines)
