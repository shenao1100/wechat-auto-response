from __future__ import annotations

import base64
import html
import logging
import re
import threading
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, Callable, Protocol

from .models import GroupConfig

logger = logging.getLogger(__name__)

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


UNCERTAIN_BUT_SENT_MARKERS = (
    "已操作发送，但数据库未确认",
    "已操作发送但数据库未确认",
)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return {"base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


def decode_wechat_content(content: Any, message_type: str) -> tuple[str | None, str]:
    """Decode WeChat 4.x zstd message frames into sender and useful text."""
    if isinstance(content, bytes):
        raw = content
        if raw.startswith(ZSTD_MAGIC):
            try:
                import zstandard

                raw = zstandard.ZstdDecompressor().decompress(raw)
            except Exception as exc:
                logger.warning("Failed to decompress WeChat message body: %s", exc)
                return None, f"[{message_type}]"
        text = raw.decode("utf-8", errors="replace").strip("\x00\ufeff \r\n")
    else:
        text = str(content or "").strip()

    sender: str | None = None
    prefix = re.match(r"^([^:\r\n]{2,}):\r?\n([\s\S]*)$", text)
    if prefix:
        candidate = prefix.group(1).strip()
        text = prefix.group(2).strip()
        if not candidate.endswith("@chatroom"):
            sender = candidate

    if text.startswith("<?xml") or text.startswith("<msg") or text.startswith("<sysmsg"):
        sender = sender or _sender_from_message_xml(text)
        text = _summarize_message_xml(text, message_type)
    return sender, text or f"[{message_type}]"


def _sender_from_message_xml(text: str) -> str | None:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    for path in (".//fromusername", ".//link[@name='username']//username"):
        value = root.findtext(path)
        if value and value.strip() and not value.strip().endswith("@chatroom"):
            return value.strip()
    return None


def _summarize_message_xml(text: str, message_type: str) -> str:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        title = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", text, re.S)
        return html.unescape(title.group(1).strip()) if title else f"[{message_type}]"

    appmsg = root.find(".//appmsg")
    if appmsg is not None:
        title = (appmsg.findtext("title") or "").strip()
        description = (appmsg.findtext("des") or "").strip()
        url = html.unescape((appmsg.findtext("url") or "").strip())
        parts = [f"[{message_type}] {title}" if title else f"[{message_type}]"]
        if description:
            parts.append(description)
        if url:
            parts.append(url)
        return "\n".join(parts)

    replacement = root.findtext(".//replacemsg")
    if replacement and replacement.strip():
        return replacement.strip()
    template = root.findtext(".//content_template/template")
    if template:
        rendered = template
        for link in root.findall(".//content_template/link_list/link"):
            name = link.get("name")
            if not name:
                continue
            values = [
                (node.text or "").strip()
                for node in link.findall(".//nickname") + link.findall(".//plain")
                if (node.text or "").strip()
            ]
            rendered = rendered.replace(f"${name}$", "、".join(values))
        rendered = re.sub(r"\$[A-Za-z0-9_]+\$", "", rendered)
        rendered = rendered.strip()
        if rendered:
            return rendered
    candidates = []
    for tag in ("title", "content", "plain"):
        for node in root.findall(f".//{tag}"):
            value = (node.text or "").strip()
            if value and value not in candidates:
                candidates.append(value)
    return "\n".join(candidates[:5]) or f"[{message_type}]"


def normalize_message(message: Any) -> dict[str, Any]:
    if isinstance(message, dict):
        data = message
    elif hasattr(message, "__dict__"):
        data = vars(message)
    else:
        data = {"content": str(message)}
    safe = _json_safe(data)
    return {
        "id": safe.get("local_id") or safe.get("id") or safe.get("msg_id"),
        "time": safe.get("create_time") or safe.get("time") or safe.get("timestamp"),
        "sender": safe.get("sender_username") or safe.get("sender") or safe.get("sender_id"),
        "sender_name": safe.get("sender_name") or safe.get("nickname"),
        "type": safe.get("type") or safe.get("local_type") or "unknown",
        "content": safe.get("content") or safe.get("text") or "",
        "direction": safe.get("attr") or safe.get("direction"),
        "sort_seq": safe.get("sort_seq"),
    }


def send_response_accepted(response: Any) -> tuple[bool, bool]:
    """Return (accepted, unverified).

    WeChat GUI sends are not idempotent. If the UI action completed but the local
    DB has not confirmed it yet, retrying can send the same text twice. Such a
    response is accepted with an unverified marker to provide at-most-once
    behavior. Failures before the send action remain retryable.
    """
    if isinstance(response, bool):
        return response, False
    message = ""
    if isinstance(response, dict):
        message = str(response.get("message") or "")
    elif hasattr(response, "message"):
        message = str(response.message or "")
    if any(marker in message for marker in UNCERTAIN_BUT_SENT_MARKERS):
        return True, True
    if isinstance(response, dict) and response.get("status") is not None:
        return str(response.get("status")) == "成功", False
    if hasattr(response, "is_success"):
        marker = response.is_success
        return bool(marker() if callable(marker) else marker), False
    if hasattr(response, "success") and not callable(response.success):
        return bool(response.success), False
    return bool(response), False


class Gateway(Protocol):
    def get_history(self, group_id: str, limit: int, offset: int = 0) -> list[dict[str, Any]]: ...
    def send_text(self, target: str, text: str, verify: bool) -> bool: ...


class WeChatGateway:
    def __init__(self, poll_interval: float = 1.0, account: str | None = None):
        from wechatauto import WeChatDB
        from wechatauto.db import Listener

        self.db = WeChatDB(account=account)
        self.listener = Listener(self.db, interval=poll_interval)
        self._resolved_users: dict[str, str] = {}
        self._sender_names: dict[str, str] = {}
        self._subscription_lock = threading.RLock()
        self._subscriptions: list[tuple[str, Callable[..., None]]] = []
        self._group_callback: Callable[[GroupConfig, dict[str, Any]], None] | None = None
        self._direct_callback: Callable[[str, dict[str, Any]], None] | None = None

    def resolve_user(self, value: str) -> str:
        cached = self._resolved_users.get(value)
        if cached:
            return cached
        hits = self.db.search_contact(value)
        exact = [
            item for item in hits
            if value in {str(item.get("username") or ""), str(item.get("remark") or ""), str(item.get("nick_name") or "")}
        ]
        resolved = str(exact[0]["username"]) if len(exact) == 1 else value
        self._resolved_users[value] = resolved
        return resolved

    def start(
        self,
        groups: tuple[GroupConfig, ...],
        callback: Callable[[GroupConfig, dict[str, Any]], None],
        direct_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._group_callback = callback
        self._direct_callback = direct_callback
        self.reconfigure(groups)
        self.listener.start()

    def reconfigure(self, groups: tuple[GroupConfig, ...]) -> None:
        """Replace live subscriptions without resetting listener watermarks."""
        if self._group_callback is None:
            raise RuntimeError("gateway must be started before it can be reconfigured")
        callback = self._group_callback
        direct_callback = self._direct_callback
        with self._subscription_lock:
            previous_users = {user for user, _callback in self._subscriptions}
            for user, registered_callback in self._subscriptions:
                self.listener.remove_listener(user, registered_callback)
            self._subscriptions.clear()

            listened_users: set[str] = set()
            for group in groups:
                if not group.enabled:
                    continue

                group_user = self.resolve_user(group.id)

                def on_message(
                    message: Any,
                    _listener: Any,
                    selected: GroupConfig = group,
                    selected_user: str = group_user,
                ) -> None:
                    callback(selected, self._normalize_db_message(selected_user, message))

                if group_user not in previous_users:
                    self._advance_watermark(group_user)
                self.listener.add_listener(group_user, on_message)
                self._subscriptions.append((group_user, on_message))
                listened_users.add(group_user)
                logger.info("Listening to group %s (%s)", group.name, group.id)

            if direct_callback is not None:
                targets = dict.fromkeys(target for group in groups if group.enabled for target in group.forward_to)
                for target in targets:
                    target_user = self.resolve_user(target)
                    if target_user in listened_users:
                        continue

                    def on_direct(
                        message: Any,
                        _listener: Any,
                        selected: str = target,
                        selected_user: str = target_user,
                    ) -> None:
                        direct_callback(selected, self._normalize_db_message(selected_user, message))

                    if target_user not in previous_users:
                        self._advance_watermark(target_user)
                    self.listener.add_listener(target_user, on_direct)
                    self._subscriptions.append((target_user, on_direct))
                    listened_users.add(target_user)
                    logger.info("Listening for clarification replies from %s", target)

            logger.info("Live WeChat subscriptions updated: groups=%d targets=%d", len(groups), len(listened_users))

    def _advance_watermark(self, user: str) -> None:
        """Start a newly enabled subscription at now, not at its previous disable time."""
        messages = self.db.get_messages(user, limit=1)
        self.listener._watermark[user] = messages[0]["sort_seq"] if messages else 0

    def stop(self) -> None:
        self.listener.stop()

    def get_history(self, group_id: str, limit: int, offset: int = 0) -> list[dict[str, Any]]:
        user = self.resolve_user(group_id)
        messages = [self._normalize_db_message(user, item) for item in self._raw_history_rows(user, limit, offset)]
        return sorted(messages, key=lambda item: (int(item.get("time") or 0), int(item.get("sort_seq") or 0)))

    def _raw_history_rows(self, user: str, limit: int, offset: int) -> list[dict[str, Any]]:
        """Read raw bodies in one DB query so zstd history decoding is not N+1."""
        try:
            found = self.db._msg_conn(user)
            if not found:
                return []
            connection, table = found
            try:
                rows = connection.execute(
                    "SELECT local_id, local_type, real_sender_id, create_time, "
                    "message_content, sort_seq FROM %s "
                    "ORDER BY sort_seq DESC LIMIT ? OFFSET ?" % table,
                    (limit, offset),
                ).fetchall()
            finally:
                connection.close()
            return [
                {
                    "local_id": row["local_id"],
                    "type": self.db._msg_type_name(row["local_type"]),
                    "sender_id": row["real_sender_id"],
                    "create_time": row["create_time"],
                    "content": row["message_content"],
                    "sort_seq": row["sort_seq"],
                }
                for row in rows
            ]
        except Exception:
            logger.exception("Raw history query failed; falling back to wechatauto get_messages")
            return self.db.get_messages(user, limit=limit, offset=offset)

    def _normalize_db_message(self, user: str, message: Any) -> dict[str, Any]:
        data = dict(message) if isinstance(message, dict) else dict(vars(message))
        local_id = data.get("local_id") or data.get("id")
        content = data.get("content")
        if local_id is not None and re.fullmatch(r"\[[^\]]+\]", str(content or "").strip()):
            raw = self.db.get_message_row(user, int(local_id))
            if raw is not None:
                content = raw.get("content")
                data.setdefault("sender_id", raw.get("sender_id"))
                data.setdefault("sort_seq", raw.get("sort_seq"))
        message_type = str(data.get("type") or data.get("local_type") or "unknown")
        decoded_sender, decoded_content = decode_wechat_content(content, message_type)
        sender_id = data.get("sender_id") or data.get("real_sender_id")
        sender = decoded_sender or data.get("sender_username") or data.get("sender") or sender_id
        if str(sender or "").isdigit() and decoded_sender is None:
            sender = str(sender_id or "")
        sender_text = str(sender or "")
        sender_name = "我" if sender_id == 2 else self._sender_display_name(sender_text)
        data.update(
            {
                "content": decoded_content,
                "sender_username": sender_text,
                "sender_name": sender_name,
                "direction": "self" if sender_id == 2 else "incoming",
            }
        )
        return normalize_message(data)

    def _sender_display_name(self, sender: str) -> str | None:
        if not sender:
            return None
        if sender in self._sender_names:
            return self._sender_names[sender]
        name = str(self.db.get_nickname(sender) or sender)
        self._sender_names[sender] = name
        return name

    def list_groups(self) -> list[dict[str, Any]]:
        groups: dict[str, dict[str, Any]] = {}
        try:
            for rel, path, _key in self.db._db_files:
                if not str(path).lower().endswith("contact.db"):
                    continue
                connection = self.db._open(rel)
                try:
                    rows = connection.execute(
                        """SELECT username, nick_name, remark FROM contact
                           WHERE username LIKE '%@chatroom'
                           ORDER BY CASE WHEN remark!='' THEN remark ELSE nick_name END COLLATE NOCASE"""
                    ).fetchall()
                finally:
                    connection.close()
                for row in rows:
                    username = str(row["username"])
                    nickname = str(row["nick_name"] or "")
                    remark = str(row["remark"] or "")
                    groups[username] = {
                        "username": username,
                        "name": remark or nickname or username,
                        "nickname": nickname,
                        "remark": remark,
                        "message_count": 0,
                    }
                break
        except Exception:
            logger.exception("Full group enumeration failed; falling back to message chats")
        for item in self.db.list_message_chats():
            username = str(item.get("username") or "")
            if not username.endswith("@chatroom"):
                continue
            existing = groups.setdefault(
                username,
                {
                    "username": username,
                    "name": str(item.get("name") or username),
                    "nickname": str(item.get("name") or ""),
                    "remark": "",
                    "message_count": 0,
                },
            )
            existing["message_count"] = int(item.get("message_count") or 0)
        return sorted(groups.values(), key=lambda item: (str(item["name"]).casefold(), item["username"]))

    def search_contacts(self, keyword: str = "") -> list[dict[str, Any]]:
        results = self.db.search_contact(keyword)
        return [
            {
                "username": str(item.get("username") or ""),
                "name": str(item.get("remark") or item.get("nick_name") or item.get("username") or ""),
                "nickname": str(item.get("nick_name") or ""),
                "remark": str(item.get("remark") or ""),
                "is_group": str(item.get("username") or "").endswith("@chatroom"),
            }
            for item in results
        ]

    def send_text(self, target: str, text: str, verify: bool) -> bool:
        from wechatauto.guia import quick_send

        response = quick_send(text, target, verify=verify)
        accepted, unverified = send_response_accepted(response)
        if unverified:
            logger.warning(
                "WeChat completed the UI send to %s but DB verification timed out; "
                "accepting it without retry to prevent duplicate delivery",
                target,
            )
        return accepted
