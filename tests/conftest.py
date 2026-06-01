"""
Session-wide test fixtures and safety resets.
"""
import pytest
from core.kill_switch import kill_switch as _global_ks


@pytest.fixture(autouse=True)
def _reset_global_kill_switch():
    """
    Reset the module-level kill_switch singleton before every test.
    Without this, a leftover state/KILL file from a real bot run causes
    attempt_trade() to return False for every test in test_main_loop.py.
    """
    _global_ks.reset()
    yield
