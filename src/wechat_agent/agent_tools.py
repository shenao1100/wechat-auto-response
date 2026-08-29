from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable

from .models import ChainRuleConfig, GroupConfig, MessageBatch, OutboundMessage, ToolExecution
from .store import Store
from .timeutils import get_timezone
from .wechat_gateway import Gateway


def parse_chain_message(content: Any) -> dict[str, Any] | None:
    text = str(content or "")
    marker = text.find("#接龙")
    if marker < 0:
        return None
    chain_text = text[marker:].strip()
    lines = chain_text.splitlines()
    title = next((line.strip() for line in lines[1:] if line.strip()), "#接龙")
    entries = []
    for line in lines:
        match = re.match(r"^\s*(\d+)\s*[.．、]\s*(.+?)\s*$", line)
        if match:
            entries.append({"number": int(match.group(1)), "content": match.group(2).strip()})
    return {"text": chain_text, "title": title, "entries": entries}


def match_chain_rule(group: GroupConfig, chain: dict[str, Any]) -> ChainRuleConfig | None:
    if not group.chain_enabled:
        return None
    haystack = re.sub(r"\s+", " ", str(chain.get("text") or "")).casefold()
    for rule in group.chain_rules:
        includes = [value.casefold() for value in rule.match_keywords]
        excludes = [value.casefold() for value in rule.exclude_keywords]
        if rule.enabled and includes and all(value in haystack for value in includes) and not any(
            value in haystack for value in excludes
        ):
            return rule
    return None


_MEMORY_PLACEHOLDER = re.compile(r"{{\s*([^{}]+?)\s*}}")


def render_chain_rule(
    rule: ChainRuleConfig,
    memories: list[dict[str, Any]],
) -> tuple[str | None, tuple[str, ...], tuple[str, ...]]:
    values = {str(item.get("key")): str(item.get("value") or "") for item in memories}
    missing: list[str] = []

    def render(value: str) -> str:
        def replace(match: re.Match[str]) -> str:
            key = match.group(1).strip()
            if not values.get(key):
                missing.append(key)
                return ""
            return values[key]

        return _MEMORY_PLACEHOLDER.sub(replace, value).strip()

    entry = render(rule.entry_template)
    identifiers = tuple(value for value in (render(item) for item in rule.self_identifiers) if value)
    missing_keys = tuple(dict.fromkeys(missing))
    return (entry if entry and not missing_keys else None), identifiers, missing_keys


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
        tool("get_memory", "读取所有监听群共享的长期记忆。", {}, []),
        tool(
            "remember",
            "保存所有监听群共享、对以后判断确实有用且已确认的长期事实或用户偏好。不要保存闲聊或推测。",
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
            "当消息是否重要取决于缺失的用户偏好或背景事实时，向本群配置的 forward_to 发送一个简短问题。问题答案会写入共享长期记忆并触发重新评估。不要用它询问可从历史消息获取的事实。",
            {
                "question": {"type": "string", "description": "给用户的单一、具体、可直接回答的问题"},
                "memory_key": {"type": "string", "description": "保存答案的稳定记忆键，如 class.attendance_preference"},
                "target": {"type": ["string", "null"], "description": "必须是本群 forward_to 之一；省略则使用第一个"},
            },
            ["question", "memory_key"],
        ),
        tool("list_schedules", "查看全局待执行日程。所有监听群和 forward_to 私聊共享同一份日程列表。", {}, []),
        tool(
            "cancel_schedule",
            "按日程 ID 取消全局待执行日程，不受当前对话限制。",
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
        tool(
            "mark_history_read",
            "仅在重要信息已通过 forward_important 进入转发流程后调用。按 recent_history/get_chat_history 中的 id 标记构成该事件的历史消息，之后这些消息会以 is_read=true 发送给上游。",
            {
                "message_ids": {
                    "type": "array",
                    "items": {"type": ["integer", "string"]},
                    "minItems": 1,
                    "maxItems": 200,
                }
            },
            ["message_ids"],
        ),
        tool(
            "reply_to_sender",
            "仅用于 forward_to 私聊模式。向当前私聊发送者回复结果、日程确认或需要补充的信息；调用后仍需输出规定的最终回复。",
            {"text": {"type": "string", "description": "简洁、自然且包含必要结果的回复正文"}},
            ["text"],
        ),
        tool(
            "continue_group_chain",
            "按当前群手动配置的接龙规则参与 #接龙。程序会重新读取最新版本、检测本人是否已经参与、按配置模板生成内容、自动编号并持久化去重。",
            {
                "source_message_id": {"type": ["integer", "string"], "description": "包含 #接龙 的历史消息 id"},
            },
            ["source_message_id"],
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
            "mark_history_read": self._mark_history_read,
            "reply_to_sender": self._reply_to_sender,
            "continue_group_chain": self._continue_group_chain,
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
        history = self.store.annotate_history(batch.group.id, history)
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
            item["targets"] = json.loads(item.pop("targets_json", "[]"))
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

    def _mark_history_read(self, args: dict[str, Any], batch: MessageBatch) -> ToolExecution:
        message_ids = args.get("message_ids")
        if not isinstance(message_ids, list):
            raise ValueError("message_ids must be an array")
        requested, newly_marked = self.store.mark_history_read(batch.group.id, message_ids)
        return ToolExecution(
            {"ok": True, "marked": requested, "newly_marked": newly_marked, "message_ids": message_ids}
        )

    def _reply_to_sender(self, args: dict[str, Any], batch: MessageBatch) -> ToolExecution:
        target = next(
            (str(message.get("_direct_target") or "").strip() for message in batch.messages if message.get("_direct_target")),
            "",
        )
        if not target:
            raise ValueError("reply_to_sender is only available for forward_to direct chats")
        text = str(args.get("text") or "").strip()[:4000]
        if not text:
            raise ValueError("text must not be empty")
        outbox_id = self.store.enqueue_message([target], text)
        self.enqueue_outbound(OutboundMessage((target,), text))
        return ToolExecution({"ok": True, "queued": True, "outbox_id": outbox_id, "target": target})

    def _continue_group_chain(self, args: dict[str, Any], batch: MessageBatch) -> ToolExecution:
        if not batch.group.id.endswith("@chatroom"):
            raise ValueError("continue_group_chain is only available in a configured group chat")
        requested_id = str(args.get("source_message_id") or "").strip()
        if not requested_id:
            raise ValueError("source_message_id must not be empty")

        history = self.gateway.get_history(batch.group.id, 200)
        requested = next((item for item in history if str(item.get("id")) == requested_id), None)
        requested_chain = parse_chain_message(requested.get("content")) if requested else None
        if requested_chain is None:
            raise ValueError("source_message_id does not identify a #接龙 message in recent history")
        rule = match_chain_rule(batch.group, requested_chain)
        if rule is None:
            return ToolExecution({"ok": False, "error": "This chain does not match any enabled rule"})
        entry, self_identifiers, missing = render_chain_rule(rule, self.store.get_memories(batch.group.id))
        if missing:
            return ToolExecution({"ok": False, "error": "Missing required shared memory", "missing_memory_keys": missing})
        if not entry:
            return ToolExecution({"ok": False, "error": "The configured entry template rendered as empty"})
        entry = " ".join(entry.splitlines()).strip()[:500]
        if "#接龙" in entry:
            raise ValueError("Configured entry template must render to one entry, not a chain")

        topic = re.sub(r"\s+", "", requested_chain["title"]).casefold()
        candidates = []
        for item in history:
            parsed = parse_chain_message(item.get("content"))
            if parsed and re.sub(r"\s+", "", parsed["title"]).casefold() == topic:
                candidates.append((item, parsed))
        latest, latest_chain = candidates[-1]
        latest_rule = match_chain_rule(batch.group, latest_chain)
        if latest_rule != rule:
            return ToolExecution({"ok": False, "error": "The latest chain version no longer matches this rule"})
        normalized_entry = re.sub(r"\s+", "", entry).casefold()
        normalized_identifiers = [re.sub(r"\s+", "", value).casefold() for value in self_identifiers]
        existing_entries = [re.sub(r"\s+", "", item["content"]).casefold() for item in latest_chain["entries"]]
        sent_by_self = str(latest.get("direction") or "").casefold() in {"self", "outgoing", "sent"}
        identity_found = bool(normalized_identifiers) and any(
            all(identifier in existing for identifier in normalized_identifiers) for existing in existing_entries
        )
        locally_recorded = self.store.has_chain_participation(batch.group.id, latest.get("id"))
        if sent_by_self or locally_recorded or normalized_entry in existing_entries or identity_found:
            return ToolExecution(
                {
                    "ok": True,
                    "already_joined": True,
                    "latest_message_id": latest.get("id"),
                    "entry": entry,
                    "rule": rule.name,
                    "matched_by": (
                        "outgoing_message" if sent_by_self else "local_record" if locally_recorded
                        else "exact_entry" if normalized_entry in existing_entries else "self_identifiers"
                    ),
                }
            )

        next_number = max((item["number"] for item in latest_chain["entries"]), default=0) + 1
        outgoing = f"{latest_chain['text'].rstrip()}\n{next_number}. {entry}"
        inserted, outbox_id = self.store.enqueue_chain_participation(
            batch.group.id,
            latest.get("id"),
            entry,
            outgoing,
        )
        if latest.get("id") is not None:
            self.store.mark_history_read(batch.group.id, [latest["id"]])
        if inserted:
            self.enqueue_outbound(OutboundMessage((batch.group.id,), outgoing))
        return ToolExecution(
            {
                "ok": True,
                "queued": inserted,
                "duplicate": not inserted,
                "outbox_id": outbox_id,
                "latest_message_id": latest.get("id"),
                "number": next_number,
                "entry": entry,
                "rule": rule.name,
            }
        )

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
