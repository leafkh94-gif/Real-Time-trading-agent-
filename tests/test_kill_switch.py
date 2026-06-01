"""Tests for core/kill_switch.py"""
import pytest
from pathlib import Path
from core.kill_switch import KillSwitch


@pytest.fixture
def switch(tmp_path):
    return KillSwitch(kill_file=tmp_path / "KILL")


def test_not_tripped_by_default(switch):
    assert switch.check() is False
    assert switch.is_tripped is False
    assert switch.reason is None


def test_trip_programmatic(switch):
    switch.trip("test reason")
    assert switch.check() is True
    assert switch.is_tripped is True
    assert switch.reason == "test reason"


def test_trip_writes_file(switch):
    switch.trip("daily loss")
    assert switch._file.exists()
    content = switch._file.read_text()
    assert "daily loss" in content


def test_trip_detected_from_file(tmp_path):
    """Simulates a separate process writing the kill file (e.g. SSH command)."""
    kill_file = tmp_path / "KILL"
    kill_file.write_text("manual stop", encoding="utf-8")

    switch = KillSwitch(kill_file=kill_file)
    assert switch.check() is True
    assert switch.reason == "manual stop"


def test_file_without_content_defaults_to_manual(tmp_path):
    kill_file = tmp_path / "KILL"
    kill_file.write_text("", encoding="utf-8")
    switch = KillSwitch(kill_file=kill_file)
    assert switch.check() is True
    assert switch.reason == "manual"


def test_remains_tripped_after_file_removed(switch):
    """Once tripped in-process, stays tripped even if file is deleted."""
    switch.trip("test")
    switch._file.unlink()
    assert switch.check() is True


def test_reset_clears_state(switch):
    switch.trip("test")
    switch.reset()
    assert switch.check() is False
    assert not switch._file.exists()


def test_reset_without_file_is_safe(switch):
    switch.reset()  # nothing tripped, no file — must not raise
    assert switch.check() is False
