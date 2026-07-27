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
from typing import Optional

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
    """Build a journal entry dict from a Signal (shared with backtest.py).

    Note: `rr` is recorded for completeness but is a constant 2.0 by
    construction (_build_levels derives TP1 from MIN_RR), so it carries no
    information as an analysis dimension — use sl_distance_atr instead.
    """
    ctx  = dict(getattr(sig, "context", {}) or {})
    atr  = ctx.get("atr") or 0.0
    risk = abs(sig.entry - sig.stop_loss)
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
        # ── diagnostics (schema 2) ────────────────────────────────────────
        "schema":          2,
        "rr":              getattr(sig, "rr", None),
        "sl_distance":     round(risk, 4),
        "sl_distance_atr": round(risk / atr, 3) if atr else None,
        "entry_dist_atr":  ctx.get("entry_dist_atr"),
        "components":      dict(getattr(sig, "components", {}) or {}),
        "reasons":         list(getattr(sig, "reasons", []) or []),
        "context":         ctx,
        # excursion fields, filled in by resolve()
        "mfe_r": 0.0, "mae_r": 0.0, "mfe_r_optimistic": 0.0,
        "bars_since_alert": 0, "bars_to_fill": None, "bars_to_resolve": None,
        "last_bar_utc": None, "r_realized": None,
    }


def _realized_r(entry: dict) -> Optional[float]:
    """R multiple actually banked. None for setups that never took a position,
    so they can't pollute expectancy averages."""
    risk = abs(entry["entry"] - entry["stop_loss"])
    if risk <= 0:
        return None
    st = entry["status"]
    if st == "sl_hit":
        return -1.0
    if st == "tp1_hit":
        return round(abs(entry["take_profit"] - entry["entry"]) / risk, 3)
    if st == "tp2_hit":
        return round(abs(entry["take_profit2"] - entry["entry"]) / risk, 3)
    return None


def load() -> list[dict]:
    """Public read of the journal (for main_alerts and analyze_journal.py)."""
    return _load()


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
    # Backward compatibility: entries written before schema 2 lack these.
    entry.setdefault("mfe_r", 0.0)
    entry.setdefault("mae_r", 0.0)
    entry.setdefault("mfe_r_optimistic", 0.0)
    entry.setdefault("bars_since_alert", 0)
    entry.setdefault("bars_to_fill", None)
    entry.setdefault("bars_to_resolve", None)
    entry.setdefault("last_bar_utc", None)
    entry.setdefault("r_realized", None)

    alert_t  = dt.datetime.strptime(entry["alert_utc"], _ISO)
    expiry_t = (dt.datetime.strptime(entry["expiry_utc"], _ISO)
                if entry["expiry_utc"] else None)
    buy  = entry["direction"] == "buy"
    E    = entry["entry"]
    risk = abs(E - entry["stop_loss"])          # R denominator, in price points
    # Live mode re-resolves open entries every scan on an overlapping window, so
    # bar counters need a watermark to stay exactly-once. MFE/MAE use running
    # max() and are therefore idempotent under replay without any guard.
    last_seen = (dt.datetime.strptime(entry["last_bar_utc"], _ISO)
                 if entry["last_bar_utc"] else None)

    after = candles[candles["time"] >= alert_t]
    for _, bar in after.iterrows():
        bar_t = bar["time"]
        if pd.isna(bar_t):
            continue
        if last_seen is None or bar_t > last_seen:
            entry["bars_since_alert"] += 1
            entry["last_bar_utc"] = bar_t.strftime(_ISO)
            last_seen = bar_t

        was_pending = entry["status"] == "pending"
        if entry["status"] == "pending":
            if expiry_t and bar_t >= expiry_t:
                entry["status"] = "expired"
                entry["resolved_utc"] = bar_t.strftime(_ISO)
                return entry
            touched = (bar["low"] <= entry["entry"]) if buy else (bar["high"] >= entry["entry"])
            if touched:
                entry["status"] = "filled"
                entry["filled_utc"] = bar_t.strftime(_ISO)
                if entry["bars_to_fill"] is None:
                    entry["bars_to_fill"] = entry["bars_since_alert"]

        # Excursion accounting — only while a position is open.
        # On the fill bar the intrabar path is unknowable, so mirror the SL-first
        # convention: the conservative mfe_r credits no favourable move on that
        # bar, while mfe_r_optimistic credits it in full. The gap between the two
        # measures how much any conclusion rests on intrabar guesswork.
        if entry["status"] == "filled" and risk > 0:
            fav = max(float((bar["high"] - E) if buy else (E - bar["low"])) / risk, 0.0)
            adv = max(float((E - bar["low"]) if buy else (bar["high"] - E)) / risk, 0.0)
            entry["mfe_r_optimistic"] = round(max(entry["mfe_r_optimistic"], fav), 3)
            entry["mfe_r"] = round(max(entry["mfe_r"], 0.0 if was_pending else fav), 3)
            entry["mae_r"] = round(max(entry["mae_r"], adv), 3)

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
                if entry["bars_to_resolve"] is None and entry["bars_to_fill"] is not None:
                    entry["bars_to_resolve"] = entry["bars_since_alert"] - entry["bars_to_fill"]
                entry["r_realized"] = _realized_r(entry)
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


def _mean(vals: list) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 3) if vals else None


def _median(vals: list) -> Optional[float]:
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    m = len(vals) // 2
    return round(vals[m] if len(vals) % 2 else (vals[m - 1] + vals[m]) / 2, 3)


def expectancy_r(rows: list[dict]) -> Optional[float]:
    """Mean R per decided trade. Uses each entry's own recorded r_realized,
    falling back to its stored levels — never a hardcoded MIN_RR, so it stays
    correct if level construction ever changes."""
    rs = []
    for r in rows:
        if r["status"] not in WIN_STATES | {"sl_hit"}:
            continue
        v = r.get("r_realized")
        if v is None:
            v = _realized_r(r)
        if v is not None:
            rs.append(v)
    return _mean(rs)


def stats(entries: list[dict] | None = None,
          since: dt.datetime | None = None) -> dict:
    """Aggregate outcome statistics, overall and per pattern.

    `since` filters by alert time (cohort by when the signal fired, not when it
    resolved). Note a recent-cohort win rate is biased DOWNWARD: losers resolve
    at 1R while winners need 2-3R and take longer, so a 7-day window
    systematically looks worse than the cumulative figure.
    """
    if entries is None:
        entries = _load()
    if since is not None:
        entries = [e for e in entries
                   if dt.datetime.strptime(e["alert_utc"], _ISO) >= since]

    def _bucket(rows: list[dict]) -> dict:
        total   = len(rows)
        filled  = [r for r in rows if r["status"] in {"filled", "sl_hit"} | WIN_STATES]
        wins    = [r for r in rows if r["status"] in WIN_STATES]
        losses  = [r for r in rows if r["status"] == "sl_hit"]
        expired = [r for r in rows if r["status"] == "expired"]
        open_n  = [r for r in rows if r["status"] in ("pending", "filled")]
        decided = len(wins) + len(losses)
        # Absent MFE (schema-1 entries) must stay None, never 0 — a legacy loss
        # counted as mfe_r=0 would fake a "direction was wrong" verdict.
        loss_mfe = [r.get("mfe_r") for r in losses if r.get("mfe_r") is not None]
        win_mae  = [r.get("mae_r") for r in wins   if r.get("mae_r") is not None]
        return {
            "total":     total,
            "filled":    len(filled),
            "wins":      len(wins),
            "losses":    len(losses),
            "expired":   len(expired),
            "open":      len(open_n),
            "fill_rate": round(len(filled) / total, 2) if total else None,
            "win_rate":  round(len(wins) / decided, 2) if decided else None,
            # ── schema 2 additions ────────────────────────────────────────
            "tp1":            sum(1 for r in rows if r["status"] == "tp1_hit"),
            "tp2":            sum(1 for r in rows if r["status"] == "tp2_hit"),
            "decided":        decided,
            "expectancy_r":   expectancy_r(rows),
            "n_with_mfe":     len(loss_mfe),
            "med_mfe_loss":   _median(loss_mfe),
            "avg_mfe_loss":   _mean(loss_mfe),
            "losses_mfe_ge_1r": sum(1 for v in loss_mfe if v >= 1.0),
            "avg_mae_win":    _mean(win_mae),
            "max_mae_win":    round(max(win_mae), 3) if win_mae else None,
        }

    out = {"overall": _bucket(entries), "per_pattern": {}}
    for pat in sorted({e["pattern"] for e in entries}):
        out["per_pattern"][pat] = _bucket([e for e in entries if e["pattern"] == pat])
    return out


def format_stats(s: dict) -> str:
    """Human-readable summary (used for the weekly Telegram report).

    Rule: never print a rate without its raw counts beside it — "0.38" alone
    invites over-reading, "0.38 (3W/5L, n=8)" does not.
    """
    o = s["overall"]
    exp = "n/a" if o["expectancy_r"] is None else f"{o['expectancy_r']:+.2f}R"
    lines = [
        f"Signals: {o['total']}  |  filled: {o['filled']}  |  open: {o['open']}",
        f"Wins: {o['wins']} (TP1 {o['tp1']} / TP2 {o['tp2']})  |  "
        f"Losses: {o['losses']}  |  expired: {o['expired']}",
        f"Fill rate: {o['fill_rate']}  |  "
        f"Win rate: {o['win_rate']} ({o['wins']}W/{o['losses']}L, n={o['decided']})  |  "
        f"Expectancy: {exp}",
    ]
    if o["n_with_mfe"]:
        lines.append(
            f"Loss anatomy: median MFE {o['med_mfe_loss']}R  |  "
            f"{o['losses_mfe_ge_1r']} of {o['n_with_mfe']} losses reached +1R first")
    if o["max_mae_win"] is not None:
        lines.append(
            f"Winner heat: avg MAE {o['avg_mae_win']}R  |  worst {o['max_mae_win']}R")
    for pat, b in s["per_pattern"].items():
        lines.append(
            f"• {pat}: {b['total']} signals, {b['wins']}W/{b['losses']}L "
            f"(TP1 {b['tp1']}/TP2 {b['tp2']}), win rate {b['win_rate']}, n={b['decided']}")
    if o["decided"] < 20:
        lines.append(
            f"⚠️ n={o['decided']} decided — at this sample size a 0% and a 50% "
            "win rate are statistically indistinguishable. Not actionable.")
    return "\n".join(lines)
