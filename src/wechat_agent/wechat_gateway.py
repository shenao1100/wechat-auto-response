from __future__ import annotations

import base64
import logging
import threading
from datetime import datetime
from typing import Any, Callable, Protocol

from .models import GroupConfig

logger = logging.getLogger(__name__)


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

                def on_message(message: Any, _listener: Any, selected: GroupConfig = group) -> None:
                    callback(selected, normalize_message(message))

                group_user = self.resolve_user(group.id)
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

                    def on_direct(message: Any, _listener: Any, selected: str = target) -> None:
                        direct_callback(selected, normalize_message(message))

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
        messages = [normalize_message(item) for item in self.db.get_messages(user, limit=limit, offset=offset)]
        return sorted(messages, key=lambda item: str(item.get("time") or ""))

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
