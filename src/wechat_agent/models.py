from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class GroupConfig:
    id: str
    name: str
    forward_to: tuple[str, ...]
    system_prompt: str = ""
    history_limit: int = 30
    aggregation_seconds: float = 8.0
    importance_threshold: int = 70
    enabled: bool = True


@dataclass(frozen=True)
class AIConfig:
    base_url: str
    model: str
    api_key_env: str = "AI_API_KEY"
    timeout_seconds: float = 60.0
    temperature: float = 0.1
    max_steps: int = 10


@dataclass(frozen=True)
class AppConfig:
    ai: AIConfig
    groups: tuple[GroupConfig, ...]
    timezone: str = "Asia/Shanghai"
    database_path: str = "data/app.db"
    log_path: str = "logs/app.log"
    poll_interval: float = 1.0
    schedule_poll_seconds: float = 5.0
    send_verify: bool = True
    duplicate_window_hours: int = 24


@dataclass
class MessageBatch:
    group: GroupConfig
    messages: list[dict[str, Any]]
    received_at: datetime = field(default_factory=datetime.now)


@dataclass
class ToolExecution:
    content: dict[str, Any]
    terminal: bool = False
    outcome: str | None = None


@dataclass
class OutboundMessage:
    targets: tuple[str, ...]
    text: str
    schedule_id: int | None = None

