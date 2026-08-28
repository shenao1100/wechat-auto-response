from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable

from .models import GroupConfig, MessageBatch, OutboundMessage, ToolExecution
from .store import Store
from .timeutils import get_timezone
from .wechat_gateway import Gateway


def tool_definitions() -> list[dict[str, Any]]:
    def tool(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required or [],
                    "additionalProperties": False,
                },
            },
        }

    return [
        tool(
            "get_chat_history",
            "获取当前群聊更长的历史记录。只有当前上下文不足以判断时调用。",
            {
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                "offset": {"type": "integer", "minimum": 0},
            },
            ["limit"],
        ),
        tool("get_memory", "读取当前群的长期记忆。", {}, []),
        tool(
            "remember",
            "保存对以后判断确实有用且已确认的长期事实。不要保存闲聊或推测。",
            {
                "key": {"type": "string"},
                "value": {"type": "string"},
                "expires_at": {"type": ["string", "null"], "description": "ISO 8601；永久有效则为 null"},
            },
            ["key", "value"],
        ),
        tool(
            "get_recent_forwarded",
            "查看近期已经转发的事件，用于避免语义重复通知。",
            {"hours": {"type": "integer", "minimum": 1, "maximum": 720}},
            [],
        ),
        tool(
            "schedule_reminder",
            "根据群消息中明确的时间安排提醒。仅在时间和事项足够明确时使用。",
            {
                "title": {"type": "string"},
                "content": {"type": "string"},
                "run_at": {"type": "string", "description": "ISO 8601 时间；无时区时按项目时区解释"},
            },
            ["title", "content", "run_at"],
        ),
        tool(
            "ask_forward_target",
            "当消息是否重要取决于缺失的用户偏好或背景事实时，向本群配置的 forward_to 发送一个简短问题。问题答案会写入长期记忆并触发重新评估。不要用它询问可从历史消息获取的事实。",
            {
                "question": {"type": "string", "description": "给用户的单一、具体、可直接回答的问题"},
                "memory_key": {"type": "string", "description": "保存答案的稳定记忆键，如 class.attendance_preference"},
                "target": {"type": ["string", "null"], "description": "必须是本群 forward_to 之一；省略则使用第一个"},
            },
            ["question", "memory_key"],
        ),
        tool("list_schedules", "查看当前群聊创建的待执行日程。", {}, []),
        tool(
            "cancel_schedule",
            "取消当前群聊创建的待执行日程。",
            {"schedule_id": {"type": "integer"}},
            ["schedule_id"],
        ),
        tool(
            "forward_important",
            "确认信息重要，生成摘要并安排转发。本工具不会结束本轮；转发后仍可维护记忆或安排日程。",
            {
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "category": {"type": "string"},
                "importance_score": {"type": "integer", "minimum": 0, "maximum": 100},
                "reason": {"type": "string"},
                "action_required": {"type": "boolean"},
                "suggested_action": {"type": ["string", "null"]},
                "event_time": {"type": ["string", "null"]},
            },
            ["title", "summary", "category", "importance_score", "reason", "action_required"],
        ),
    ]


class ToolRuntime:
    def __init__(
        self,
        store: Store,
        gateway: Gateway,
        enqueue_outbound: Callable[[OutboundMessage], None],
        timezone_name: str,
        duplicate_window_hours: int,
    ):
        self.store = store
        self.gateway = gateway
        self.enqueue_outbound = enqueue_outbound
        self.timezone = get_timezone(timezone_name)
        self.duplicate_window_hours = duplicate_window_hours

    def execute(self, name: str, arguments: dict[str, Any], batch: MessageBatch) -> ToolExecution:
        handlers = {
            "get_chat_history": self._get_chat_history,
            "get_memory": self._get_memory,
            "remember": self._remember,
            "get_recent_forwarded": self._get_recent_forwarded,
            "schedule_reminder": self._schedule_reminder,
            "ask_forward_target": self._ask_forward_target,
            "list_schedules": self._list_schedules,
            "cancel_schedule": self._cancel_schedule,
            "forward_important": self._forward_important,
        }
        handler = handlers.get(name)
        if handler is None:
            return ToolExecution({"ok": False, "error": f"Unknown tool: {name}"})
        try:
            return handler(arguments, batch)
        except (ValueError, TypeError, KeyError) as exc:
            return ToolExecution({"ok": False, "error": str(exc)})

    def _get_chat_history(self, args: dict[str, Any], batch: MessageBatch) -> ToolExecution:
        limit = min(200, max(1, int(args["limit"])))
        offset = max(0, int(args.get("offset", 0)))
        history = self.gateway.get_history(batch.group.id, limit, offset)
        return ToolExecution({"ok": True, "messages": history, "count": len(history)})

    def _get_memory(self, _args: dict[str, Any], batch: MessageBatch) -> ToolExecution:
        return ToolExecution({"ok": True, "memories": self.store.get_memories(batch.group.id)})

    def _remember(self, args: dict[str, Any], batch: MessageBatch) -> ToolExecution:
        key = str(args["key"]).strip()[:120]
        value = str(args["value"]).strip()[:4000]
        if not key or not value:
            raise ValueError("key and value must not be empty")
        expires_at = args.get("expires_at")
        if expires_at:
            expires_at = self._parse_time(str(expires_at)).isoformat()
        self.store.remember(batch.group.id, key, value, expires_at)
        return ToolExecution({"ok": True, "key": key})

    def _get_recent_forwarded(self, args: dict[str, Any], batch: MessageBatch) -> ToolExecution:
        hours = min(720, max(1, int(args.get("hours", self.duplicate_window_hours))))
        return ToolExecution({"ok": True, "events": self.store.recent_events(batch.group.id, hours)})

    def _schedule_reminder(self, args: dict[str, Any], batch: MessageBatch) -> ToolExecution:
        run_at = self._parse_time(str(args["run_at"]))
        if run_at <= datetime.now(timezone.utc):
            raise ValueError("run_at must be in the future")
        title = str(args["title"]).strip()[:200]
        content = str(args["content"]).strip()[:4000]
        existing = self.store.find_schedule(batch.group.id, title, content, run_at.isoformat())
        if existing is not None:
            return ToolExecution({"ok": True, "schedule_id": existing, "duplicate": True, "run_at": run_at.isoformat()})
        schedule_id = self.store.add_schedule(
            batch.group.id,
            title,
            content,
            run_at.isoformat(),
            list(batch.group.forward_to),
        )
        return ToolExecution({"ok": True, "schedule_id": schedule_id, "run_at": run_at.isoformat()})

    def _list_schedules(self, _args: dict[str, Any], batch: MessageBatch) -> ToolExecution:
        schedules = self.store.list_schedules(batch.group.id)
        for item in schedules:
            item.pop("targets_json", None)
        return ToolExecution({"ok": True, "schedules": schedules})

    def _ask_forward_target(self, args: dict[str, Any], batch: MessageBatch) -> ToolExecution:
        question = str(args["question"]).strip()[:1000]
        memory_key = str(args["memory_key"]).strip()[:120]
        if not question or not memory_key:
            raise ValueError("question and memory_key must not be empty")
        target = str(args.get("target") or batch.group.forward_to[0]).strip()
        if target not in batch.group.forward_to:
            raise ValueError("target must be one of this group's forward_to entries")
        clarification_id = self.store.create_clarification(
            batch.group.id,
            batch.group.name,
            question,
            memory_key,
            batch.messages,
            target,
        )
        self.enqueue_outbound(OutboundMessage((target,), f"clarification:{clarification_id}"))
        return ToolExecution({"ok": True, "clarification_id": clarification_id, "target": target})

    def _cancel_schedule(self, args: dict[str, Any], batch: MessageBatch) -> ToolExecution:
        cancelled = self.store.cancel_schedule(batch.group.id, int(args["schedule_id"]))
        return ToolExecution({"ok": cancelled})

    def _forward_important(self, args: dict[str, Any], batch: MessageBatch) -> ToolExecution:
        score = max(0, min(100, int(args["importance_score"])))
        if score < batch.group.importance_threshold:
            return ToolExecution(
                {
                    "ok": False,
                    "error": f"score {score} is below forwarding threshold {batch.group.importance_threshold}",
                    "instruction": "Reassess using evidence, then finish with a consistent FINAL_DECISION.",
                }
            )
        title = str(args["title"]).strip()[:200]
        summary = str(args["summary"]).strip()[:4000]
        category = str(args["category"]).strip()[:100]
        normalized = re.sub(r"\s+", "", f"{title}|{summary}").lower()
        semantic_fingerprint = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        if self.store.has_recent_event(batch.group.id, semantic_fingerprint, self.duplicate_window_hours):
            return ToolExecution({"ok": True, "duplicate": True})
        fingerprint = f"{semantic_fingerprint}:{int(datetime.now(timezone.utc).timestamp())}"
        text = self._format_forward(args, batch.group)
        inserted, outbox_id = self.store.record_event_and_enqueue(
            batch.group.id,
            fingerprint,
            title,
            summary,
            category,
            score,
            batch.messages,
            list(batch.group.forward_to),
            text,
        )
        if not inserted:
            return ToolExecution({"ok": True, "duplicate": True})

        self.enqueue_outbound(OutboundMessage(batch.group.forward_to, text))
        return ToolExecution({"ok": True, "queued": True, "outbox_id": outbox_id})

    def _parse_time(self, value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=self.timezone)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _format_forward(args: dict[str, Any], group: GroupConfig) -> str:
        lines = [
            "【重要群消息】",
            f"来源：{group.name}",
            f"类型：{args['category']}",
            f"重要度：{args['importance_score']}/100",
            "",
            str(args["title"]),
            str(args["summary"]),
        ]
        if args.get("event_time"):
            lines.extend(["", f"事件时间：{args['event_time']}"])
        if args.get("action_required"):
            lines.extend(["", f"需要处理：{args.get('suggested_action') or '请及时查看并处理'}"])
        lines.extend(["", f"判断依据：{args['reason']}"])
        return "\n".join(lines)
