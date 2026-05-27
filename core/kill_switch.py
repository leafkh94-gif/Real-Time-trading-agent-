"""
File-based kill switch. Checked every main loop iteration.
Trip from SSH/RDP: echo "manual stop" > state/KILL
Clear to resume: del state/KILL  (Windows) or rm state/KILL (Linux)
"""
from pathlib import Path
from datetime import datetime, timezone

KILL_FILE = Path("state/KILL")
KILL_FILE.parent.mkdir(exist_ok=True)


class KillSwitch:
    def __init__(self, kill_file: Path = KILL_FILE):
        self._file = kill_file
        self._file.parent.mkdir(exist_ok=True)
        self._tripped = False
        self._reason: str | None = None

    def check(self) -> bool:
        """Return True if the bot should stop. Checks file on every call."""
        if self._tripped:
            return True
        if self._file.exists():
            self._tripped = True
            self._reason = self._file.read_text(encoding="utf-8").strip() or "manual"
            return True
        return False

    def trip(self, reason: str) -> None:
        """Trip the switch programmatically (e.g. daily loss limit hit)."""
        self._tripped = True
        self._reason = reason
        self._file.write_text(
            f"{datetime.now(timezone.utc).isoformat()} | {reason}",
            encoding="utf-8",
        )

    def reset(self) -> None:
        """Remove the kill file and clear in-memory state. Use only in tests / recovery drills."""
        self._tripped = False
        self._reason = None
        if self._file.exists():
            self._file.unlink()

    @property
    def reason(self) -> str | None:
        return self._reason

    @property
    def is_tripped(self) -> bool:
        return self._tripped


kill_switch = KillSwitch()
