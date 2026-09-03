"""
Guards against a backtest that reports nothing as though it were a result.

A run with `--bars 2000` came back with "received 0" for all four instruments,
printed "No signals generated over the tested window", exited 0 and uploaded an
empty artifact. Nothing in the pipeline objected. An empty feed and a quiet
market are not the same thing, and only one of them is worth reading.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


def test_bars_above_the_api_ceiling_is_refused():
    """Capital.com returns an EMPTY series above 1000 bars, not a truncated
    one — so a larger request cannot be silently clamped or simply attempted."""
    r = subprocess.run([sys.executable, "backtest.py", "--bars", "2000"],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode != 0
    assert "1000" in (r.stdout + r.stderr)


def test_bars_at_the_ceiling_is_accepted():
    """The guard must reject only what the API rejects. Without credentials the
    run stops at the credential check, which is proof the ceiling let it past."""
    r = subprocess.run([sys.executable, "backtest.py", "--bars", "1000"],
                       cwd=ROOT, capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "CAPITAL_API_KEY": "",
                            "CAPITAL_IDENTIFIER": "", "CAPITAL_PASSWORD": ""})
    assert "exceeds" not in (r.stdout + r.stderr)


def _workflow():
    raw = yaml.safe_load((ROOT / ".github/workflows/backtest.yml").read_text())
    # PyYAML parses the `on:` key as the boolean True.
    return raw


def test_the_workflow_does_not_mask_a_failing_step():
    """Every analysis step pipes into `tee`. A pipeline's status is its LAST
    command, so without pipefail a python step exiting non-zero is masked by
    tee succeeding — which is what let a zero-entry run go green.

    This is the load-bearing half of the fix: the exit code added to
    backtest.py is invisible to CI without it.
    """
    shell = _workflow()["jobs"]["backtest"]["defaults"]["run"]["shell"]
    assert "pipefail" in shell, shell
    assert shell.startswith("bash -e"), shell


def test_every_analysis_step_still_pipes_to_the_report():
    """The report file is the only artifact a human reads; a step that stops
    appending to it silently shrinks the record."""
    steps = _workflow()["jobs"]["backtest"]["steps"]
    runs = [s["run"] for s in steps if "run" in s and "analyze_journal" in s.get("run", "")]
    assert len(runs) >= 3, runs
    for r in runs:
        assert "backtest_report.txt" in r
