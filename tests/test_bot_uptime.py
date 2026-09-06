"""
The live bot's coverage depends on one number, and it was set against it.

Measured from 959 production runs: every single one carries `event: schedule`.
Not one was produced by the "self-chain" step, because GitHub deliberately
suppresses workflow runs dispatched with the built-in GITHUB_TOKEN. The cron
is therefore the only restart, and scheduled workflows fire late — observed
gaps between a run ending and the next starting ranged from 46 minutes to
3h51m. With a 90-minute run length that left the bot down roughly two thirds
of the time, which is its own answer to "the bot barely sends".
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
JOB_TIMEOUT_CEILING_MIN = 360   # GitHub's hard limit for a hosted job


def _job():
    raw = yaml.safe_load((ROOT / ".github/workflows/alert_bot.yml").read_text())
    return raw["jobs"]["run"]


def _max_runtime_s():
    for step in _job()["steps"]:
        env = step.get("env") or {}
        if "MAX_RUNTIME_S" in env:
            return int(env["MAX_RUNTIME_S"])
    raise AssertionError("MAX_RUNTIME_S not found")


def test_a_run_spans_most_of_the_cron_gap():
    """Cron ticks hourly but fires late. A run must last long enough that a
    delayed tick is an overlap, not an outage."""
    assert _max_runtime_s() >= 4 * 3600, (
        f"{_max_runtime_s()}s leaves the bot offline between cron ticks")


def test_the_bot_exits_before_the_runner_kills_it():
    """The journal and state upload in steps AFTER the bot exits. If the runner
    timeout hits first the job is cancelled and that run's journal is lost."""
    timeout_s = _job()["timeout-minutes"] * 60
    assert _max_runtime_s() < timeout_s, (
        f"MAX_RUNTIME_S {_max_runtime_s()}s >= job timeout {timeout_s}s")
    # Leave room for pip install, cache restore and the upload steps.
    assert timeout_s - _max_runtime_s() >= 900


def test_job_timeout_is_under_githubs_ceiling():
    assert _job()["timeout-minutes"] < JOB_TIMEOUT_CEILING_MIN


def test_the_self_chain_reports_its_own_failure():
    """`curl -s` with no status check printed "Next run queued." unconditionally,
    which is exactly why a chain that had never once fired looked healthy in
    959 consecutive logs."""
    step = next(s for s in _job()["steps"]
                if "Self-chain" in s.get("name", ""))
    run = step["run"]
    assert "http_code" in run, "dispatch status is not captured"
    assert "::warning::" in run, "a failed dispatch is not surfaced"
    assert "Next run queued." not in run, "still claims success unconditionally"
