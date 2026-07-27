"""
analyze_journal.py — what do the losing trades have in common?

Reads a live journal and/or a backtest dump and slices winners against losers
across every recorded dimension. Designed to refuse to overclaim: a level is
only promoted to FINDINGS when it clears a minimum sample size AND its Wilson
95% interval excludes the baseline win rate. Everything else is printed as
counts under INSUFFICIENT DATA with no rate and no ranking.

The MFE section runs first because it answers the question that actually
matters — whether losses are a selection problem (price never went our way) or
an exit problem (it went our way, then came back and stopped us out). Those two
have opposite remedies.

Usage:
    python analyze_journal.py                        # signal_journal.json
    python analyze_journal.py backtest_entries.json
    python analyze_journal.py a.json b.json --min-n 20
    python analyze_journal.py --selftest             # invariant sweep
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys

import numpy as np
import pandas as pd

from strategy import journal
from strategy import strategy_config as C

WIN = {"tp1_hit", "tp2_hit"}
_ISO = "%Y-%m-%dT%H:%M:%S"


# ── statistics (no scipy) ────────────────────────────────────────────────────
def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval — behaves sanely at tiny n, unlike normal approx."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def n_needed(p: float, delta: float) -> int:
    """Rough decided-trades-per-group needed to confirm a lift of `delta`."""
    return int(math.ceil(16 * p * (1 - p) / max(delta, 1e-6) ** 2))


# ── loading ──────────────────────────────────────────────────────────────────
def _band(v, edges, labels):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    for e, lab in zip(edges, labels):
        if v < e:
            return lab
    return labels[-1]


def normalize(entries: list[dict]) -> pd.DataFrame:
    """Flatten entries to a frame. Missing numerics become NaN, never 0 —
    a legacy entry with no mfe_r must not read as 'went nowhere'."""
    rows = []
    for e in entries:
        comp = e.get("components") or {}
        ctx  = e.get("context") or {}
        st   = e.get("status")
        r = {
            "id": e.get("id"), "epic": e.get("epic"), "pattern": e.get("pattern"),
            "direction": e.get("direction"), "tier": e.get("tier"),
            "score": e.get("score"), "status": st,
            "source": e.get("source", "live"),
            "alert_utc": e.get("alert_utc"),
            "outcome": ("win" if st in WIN else "loss" if st == "sl_hit"
                        else "expired" if st == "expired" else "open"),
            "mfe_r": e.get("mfe_r"), "mae_r": e.get("mae_r"),
            "mfe_r_optimistic": e.get("mfe_r_optimistic"),
            "bars_to_fill": e.get("bars_to_fill"),
            "bars_to_resolve": e.get("bars_to_resolve"),
            "sl_distance_atr": e.get("sl_distance_atr"),
            "entry_dist_atr": e.get("entry_dist_atr"),
            "r_realized": e.get("r_realized") if e.get("r_realized") is not None
                          else journal._realized_r(e) if "entry" in e else None,
            "pattern_type": C.PATTERNS.get(e.get("pattern"), {}).get("type"),
        }
        for k in ("pattern", "confirmation", "daily_bias", "session", "additional",
                  "round_number", "volume_confirm", "anchored_vwap",
                  "volume_profile", "choppy"):
            r[f"comp_{k}"] = comp.get(k)
        for k in ("bias_state", "adx", "atr", "atr_pct", "session_label",
                  "avwap_state", "vp_state", "confirm_count", "sl_clip",
                  "raw_sl_dist_atr", "hour_utc", "weekday"):
            r[f"ctx_{k}"] = ctx.get(k)
        rows.append(r)

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["decided"] = df["outcome"].isin(["win", "loss"])
    df["same_bar"] = df["bars_to_resolve"] == 0
    # How much of a verdict rests on unknowable intrabar ordering.
    df["mfe_ambiguous"] = (df["mfe_r_optimistic"].astype(float)
                           - df["mfe_r"].astype(float)) > 0.5
    df["choppy"] = df["comp_choppy"].apply(
        lambda v: None if v is None else bool(v < 0))
    df["round_number"] = df["comp_round_number"].apply(
        lambda v: None if v is None else bool(v > 0))
    df["score_band"] = df["score"].apply(
        lambda v: _band(v, [72, 76, 80, 84, 88],
                        ["<72", "72-75", "76-79", "80-83", "84-87", "88+"]))
    df["sl_atr_band"] = df["sl_distance_atr"].apply(
        lambda v: _band(v, [2.5, 3.0, 3.5], ["<2.5", "2.5-3.0", "3.0-3.5", "3.5+"]))
    df["atr_pct_band"] = df["ctx_atr_pct"].apply(
        lambda v: _band(v, [0.004, 0.008, 0.012],
                        ["<0.4%", "0.4-0.8%", "0.8-1.2%", "1.2%+"]))
    df["entry_dist_band"] = df["entry_dist_atr"].apply(
        lambda v: _band(v, [0.5, 1.0, 1.5], ["<0.5", "0.5-1.0", "1.0-1.5", "1.5+"]))
    df["hour_bucket"] = df["ctx_hour_utc"].apply(
        lambda v: None if v is None or (isinstance(v, float) and math.isnan(v))
        else f"{int(v)//4*4:02d}-{int(v)//4*4+4:02d}")
    return df


def load_paths(paths: list[str]) -> list[dict]:
    out = []
    for p in paths:
        if not os.path.exists(p):
            print(f"  ! {p} not found — skipped")
            continue
        with open(p) as f:
            data = json.load(f)
        for e in data:
            e.setdefault("source", "backtest" if "backtest" in p else "live")
        print(f"  loaded {len(data):>5} entries from {p}")
        out.extend(data)
    return out


# ── sections ─────────────────────────────────────────────────────────────────
def section_mfe(df: pd.DataFrame, min_n: int) -> None:
    losses = df[df["outcome"] == "loss"]
    wins   = df[df["outcome"] == "win"]
    mfe = losses["mfe_r"].dropna().astype(float)

    print("\n" + "=" * 72)
    print("LOSS ANATOMY — how far did price go our way before stopping us out?")
    print("=" * 72)
    if mfe.empty:
        print("  No losses with MFE data (pre-instrumentation entries carry none).")
        return

    edges  = [0.0, 0.25, 0.5, 1.0, 1.5, 2.0, 99]
    labels = ["0.00-0.25R", "0.25-0.50R", "0.50-1.00R",
              "1.00-1.50R", "1.50-2.00R", "2.00R+"]
    notes  = ["direction was simply wrong", "", "",
              "went 1R our way, then reversed to SL", "nearly reached TP1", ""]
    print(f"  n = {len(mfe)} losses with MFE data "
          f"(of {len(losses)} total losses)\n")
    for i, lab in enumerate(labels):
        c = int(((mfe >= edges[i]) & (mfe < edges[i + 1])).sum())
        bar = "█" * min(c, 40)
        print(f"    {lab:<12} {bar:<20} {c:>4}   {notes[i]}")
    print(f"\n  median MFE {mfe.median():.2f}R  |  mean {mfe.mean():.2f}R")

    amb = int(losses["mfe_ambiguous"].fillna(False).sum())
    if amb:
        print(f"  {amb} of {len(losses)} losses filled and resolved in one candle — "
              "intrabar path unknowable, excluded from the verdict")

    if not wins.empty and wins["mae_r"].notna().any():
        mae = wins["mae_r"].dropna().astype(float)
        print(f"\n  WINNER HEAT: avg MAE {mae.mean():.2f}R | worst {mae.max():.2f}R")
        print(f"  -> a stop at {mae.max() + 0.05:.2f}R would have kept every winner")

    # Verdict — thresholds chosen so the two remedies are mutually exclusive.
    clean = losses[~losses["mfe_ambiguous"].fillna(False)]["mfe_r"].dropna().astype(float)
    if clean.empty:
        print("\n  VERDICT: no unambiguous losses — cannot judge.")
        return
    low  = float((clean < 0.5).mean())
    high = float((clean >= 1.0).mean())
    print()
    prefix = "" if len(clean) >= min_n else f"HYPOTHESIS (n={len(clean)}, need {min_n}): "
    if low >= 0.6:
        print(f"  {prefix}SELECTION problem — {low:.0%} of losses never exceeded 0.5R.")
        print("    Entries are directionally wrong. A wider or trailing stop would")
        print("    NOT have helped; the fix belongs at signal selection.")
    elif high >= 0.4:
        print(f"  {prefix}EXIT problem — {high:.0%} of losses reached +1R first.")
        print("    Direction was right and the trade gave it back. The fix is exit")
        print("    management (break-even stop / partial take), not signal quality.")
    else:
        print(f"  {prefix}MIXED — {low:.0%} under 0.5R, {high:.0%} over 1R.")
        print("    No single remedy is indicated by this distribution.")


def section_counterfactuals(df: pd.DataFrame) -> None:
    """Arithmetic on recorded excursions. No strategy change is implied."""
    d = df[df["decided"]].copy()
    d = d[d["mfe_r"].notna() & d["r_realized"].notna()]
    if d.empty:
        print("\nCOUNTERFACTUALS: no decided trades with excursion data.")
        return
    base = d["r_realized"].astype(float)
    print("\n" + "=" * 72)
    print("EXIT-RULE COUNTERFACTUALS  (arithmetic on recorded data)")
    print("=" * 72)
    print(f"  baseline                  : total {base.sum():+7.1f}R  "
          f"per trade {base.mean():+.3f}R   (n={len(d)})")

    # Break-even stop once +1R is reached: losses that touched 1R scratch at 0.
    be = base.where(~((base < 0) & (d["mfe_r"].astype(float) >= 1.0)), 0.0)
    print(f"  break-even stop at +1R    : total {be.sum():+7.1f}R  "
          f"per trade {be.mean():+.3f}R   "
          f"({int(((base < 0) & (d['mfe_r'].astype(float) >= 1.0)).sum())} losses scratched)")

    # Half off at +1R, remainder runs to its actual outcome with a BE stop.
    reached = d["mfe_r"].astype(float) >= 1.0
    half = np.where(reached & (base < 0), 0.5,
                    np.where(reached & (base > 0), 0.5 + base / 2, base))
    half = pd.Series(half, index=d.index)
    print(f"  half off at +1R (BE rest) : total {half.sum():+7.1f}R  "
          f"per trade {half.mean():+.3f}R")

    # Wider stop. Two honesty requirements:
    #  1. Widening the stop widens the risk unit, so a target at an unchanged
    #     PRICE is worth less R (TP1 becomes 2/mult R, not 2R). Ignoring this
    #     makes any wider stop look free.
    #  2. A loss that would have survived the wider stop has an UNKNOWN outcome
    #     — the recorded path stops at the original stop. Assigning it 0R (or
    #     a win) is an assumption, not data. Report bounds instead.
    mae, mfe = d["mae_r"].astype(float), d["mfe_r"].astype(float)
    for mult in (1.25, 1.5):
        survived = (base < 0) & (mae < mult)
        known = base.copy()
        known[base > 0] = base[base > 0] / mult      # targets worth less R
        known[(base < 0) & ~survived] = -1.0         # still stopped out
        pess = known.copy(); pess[survived] = -1.0            # eventually stop out
        opti = known.copy(); opti[survived] = C.MIN_RR / mult  # eventually reach TP1
        print(f"  stop x{mult:<5}              : "
              f"{pess.mean():+.3f}R .. {opti.mean():+.3f}R per trade   "
              f"({int(survived.sum())} losses survive, outcome unknown)")
    print("\n  NOTE: counterfactuals replay the recorded price path; they assume")
    print("        the rule itself would not have changed it. The stop-widening")
    print("        rows are a RANGE because trades that survive a wider stop have")
    print("        no recorded outcome beyond the original stop. Indicative only.")


def section_fills(df: pd.DataFrame) -> None:
    print("\n" + "=" * 72)
    print("FILL / EXPIRY  — a setup that never fills is a different failure")
    print("=" * 72)
    for dim in ("pattern_type", "pattern", "entry_dist_band"):
        if dim not in df or df[dim].isna().all():
            continue
        print(f"\n  by {dim}:")
        for lvl, g in df.groupby(dim, dropna=True):
            filled = int((g["outcome"].isin(["win", "loss"]) |
                          (g["status"] == "filled")).sum())
            exp = int((g["outcome"] == "expired").sum())
            btf = g["bars_to_fill"].dropna()
            print(f"    {str(lvl):<14} n={len(g):>4}  filled={filled:>4}  "
                  f"expired={exp:>3}  fill rate={filled/len(g):.2f}"
                  + (f"  median bars to fill={btf.median():.0f}" if len(btf) else ""))


DIMENSIONS = [
    "pattern", "pattern_type", "tier", "direction", "epic", "score_band",
    "ctx_session_label", "hour_bucket", "ctx_weekday", "ctx_bias_state",
    "choppy", "round_number", "ctx_avwap_state", "ctx_vp_state",
    "ctx_confirm_count", "ctx_sl_clip", "sl_atr_band", "atr_pct_band",
    "entry_dist_band", "source",
]


def section_dimensions(df: pd.DataFrame, min_n: int) -> None:
    d = df[df["decided"]]
    n_all = len(d)
    if n_all == 0:
        print("\nNo decided trades — nothing to slice.")
        return
    base_p = float((d["outcome"] == "win").mean())
    print("\n" + "=" * 72)
    print(f"DIMENSION SWEEP  (decided trades only: n={n_all}, "
          f"baseline win rate {base_p:.2f})")
    print("=" * 72)

    findings, insufficient = [], []
    for dim in DIMENSIONS:
        if dim not in d or d[dim].isna().all():
            continue
        for lvl, g in d.groupby(dim, dropna=True):
            n = len(g)
            k = int((g["outcome"] == "win").sum())
            lo, hi = wilson(k, n)
            row = {
                "dim": dim, "level": str(lvl), "n": n, "w": k, "l": n - k,
                "rate": k / n, "lo": lo, "hi": hi,
                "exp": g["r_realized"].dropna().astype(float).mean()
                       if g["r_realized"].notna().any() else float("nan"),
                "mfe": g["mfe_r"].dropna().astype(float).mean()
                       if g["mfe_r"].notna().any() else float("nan"),
            }
            if n >= min_n and (hi < base_p or lo > base_p):
                findings.append(row)
            else:
                insufficient.append(row)

    if findings:
        print(f"\n  FINDINGS  (n >= {min_n} and 95% CI excludes baseline)\n")
        for r in sorted(findings, key=lambda r: abs(r["rate"] - base_p), reverse=True):
            need = n_needed(base_p, abs(r["rate"] - base_p))
            print(f"    {r['dim']}={r['level']:<16} "
                  f"win rate {r['rate']:.2f} [{r['lo']:.2f}-{r['hi']:.2f}] "
                  f"({r['w']}W/{r['l']}L, n={r['n']})  exp {r['exp']:+.2f}R")
            print(f"        -> need ~{need} decided trades per group to confirm")
    else:
        print(f"\n  FINDINGS: none. No level reaches n={min_n} with a CI that "
              "excludes the baseline.")

    print(f"\n  INSUFFICIENT DATA  (counts only — deliberately no rates or ranking)\n")
    by_dim: dict[str, list] = {}
    for r in insufficient:
        by_dim.setdefault(r["dim"], []).append(r)
    for dim, rows in by_dim.items():
        top = sorted(rows, key=lambda r: -r["n"])[:6]
        cells = "  ".join(f"{r['level']}={r['w']}W/{r['l']}L" for r in top)
        print(f"    {dim:<20} {cells}")


def selftest(df: pd.DataFrame) -> int:
    """Invariant sweep — far stronger than synthetic tests because it runs on
    every real bar in the sample. A violation means resolve() is wrong."""
    bad = 0
    def check(mask, msg):
        nonlocal bad
        n = int(mask.sum())
        if n:
            bad += n
            print(f"  FAIL {msg}: {n} entries")
    sl = df[df["status"] == "sl_hit"]
    check(sl["mae_r"].astype(float) < 1.0 - 1e-6, "sl_hit with mae_r < 1.0")
    t1 = df[df["status"] == "tp1_hit"]
    check(t1["mfe_r_optimistic"].astype(float) < 2.0 - 1e-6,
          "tp1_hit with mfe_r_optimistic < 2.0")
    t2 = df[df["status"] == "tp2_hit"]
    check(t2["mfe_r_optimistic"].astype(float) < 3.0 - 1e-6,
          "tp2_hit with mfe_r_optimistic < 3.0")
    ex = df[df["status"] == "expired"]
    check(ex["mfe_r"].astype(float).fillna(0) > 0, "expired with mfe_r > 0")
    check(df["bars_to_resolve"].dropna().astype(float) < 0, "negative bars_to_resolve")
    check(df["mfe_r"].astype(float) > df["mfe_r_optimistic"].astype(float) + 1e-9,
          "mfe_r above mfe_r_optimistic")
    print("  OK — all excursion invariants hold" if not bad
          else f"  {bad} invariant violations")
    return bad


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", default=None)
    ap.add_argument("--min-n", type=int, default=20)
    ap.add_argument("--since", default=None, help="YYYY-MM-DD")
    ap.add_argument("--epic", default=None)
    ap.add_argument("--json", dest="json_out", default=None)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    paths = args.paths or [journal.JOURNAL_FILE]
    print("Loading:")
    entries = load_paths(paths)
    if not entries:
        sys.exit("No entries loaded.")
    df = normalize(entries)
    if args.since:
        df = df[df["alert_utc"] >= args.since]
    if args.epic:
        df = df[df["epic"] == args.epic]
    if df.empty:
        sys.exit("No entries after filtering.")

    n_dec = int(df["decided"].sum())
    print(f"\n{len(df)} entries | {n_dec} decided | "
          f"sources: {', '.join(sorted(df['source'].unique()))}")
    if len(df["source"].unique()) > 1:
        print("  ! Pooling live and backtest entries: backtest bypasses the")
        print("    blackout/cooldown/caps, so they are different populations.")
    if n_dec < 30:
        print("\n  *** SAMPLE TOO SMALL — everything below is a hypothesis list, ***")
        print("  ***          not a conclusion. Do not tune on it.            ***")

    if args.selftest:
        print("\nSELFTEST")
        sys.exit(1 if selftest(df) else 0)

    section_mfe(df, args.min_n)
    section_counterfactuals(df)
    section_fills(df)
    section_dimensions(df, args.min_n)

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump({"n": len(df), "decided": n_dec,
                       "stats": journal.stats(entries)}, f, indent=1, default=str)
        print(f"\nWrote {args.json_out}")


if __name__ == "__main__":
    main()
