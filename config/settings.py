"""Non-secret configuration. Safe to commit."""
from dataclasses import dataclass


@dataclass(frozen=True)
class BotSettings:
    loop_interval_seconds: float = 5.0
    broker_timeout_seconds: float = 10.0
    log_level: str = "INFO"
    state_dir: str = "state"
    logs_dir: str = "logs"


settings = BotSettings()
