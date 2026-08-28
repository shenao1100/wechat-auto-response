from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .models import GroupConfig, MessageBatch


@dataclass
class _Buffer:
    group: GroupConfig
    messages: list[dict[str, Any]] = field(default_factory=list)
    first_at: float = field(default_factory=time.monotonic)
    deadline: float = field(default_factory=time.monotonic)


class MessageAggregator:
    """Debounces chat messages while enforcing a maximum 30-second batch window."""

    def __init__(self, max_batch_seconds: float = 30.0, max_messages: int = 50):
        self.max_batch_seconds = max_batch_seconds
        self.max_messages = max(1, max_messages)
        self._buffers: dict[str, _Buffer] = {}
        self._ready: deque[MessageBatch] = deque()
        self._condition = threading.Condition()

    def submit(self, group: GroupConfig, message: dict[str, Any]) -> None:
        now = time.monotonic()
        with self._condition:
            buffer = self._buffers.get(group.id)
            if buffer is None:
                buffer = _Buffer(group=group, first_at=now)
                self._buffers[group.id] = buffer
            buffer.messages.append(message)
            buffer.deadline = min(now + group.aggregation_seconds, buffer.first_at + self.max_batch_seconds)
            if len(buffer.messages) >= self.max_messages:
                self._buffers.pop(group.id, None)
                self._ready.append(MessageBatch(buffer.group, buffer.messages, datetime.now()))
            self._condition.notify_all()

    def pop_ready(self, stop_event: threading.Event) -> MessageBatch | None:
        with self._condition:
            while not stop_event.is_set():
                if self._ready:
                    return self._ready.popleft()
                if not self._buffers:
                    self._condition.wait(timeout=1.0)
                    continue
                now = time.monotonic()
                ready = [item for item in self._buffers.values() if item.deadline <= now]
                if ready:
                    selected = min(ready, key=lambda item: item.deadline)
                    self._buffers.pop(selected.group.id, None)
                    return MessageBatch(selected.group, selected.messages, datetime.now())
                next_deadline = min(item.deadline for item in self._buffers.values())
                self._condition.wait(timeout=min(1.0, max(0.01, next_deadline - now)))
        return None

    def wake(self) -> None:
        with self._condition:
            self._condition.notify_all()
