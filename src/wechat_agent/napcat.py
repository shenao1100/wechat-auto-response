from __future__ import annotations

import json
import logging
import queue
import threading
import time
import uuid
from typing import Any, Callable

from .models import GroupConfig, NapCatConfig
from .wechat_gateway import WeChatGateway

logger = logging.getLogger(__name__)


def _message_text(message: Any) -> str:
    if isinstance(message, str):
        return message.strip()
    if isinstance(message, list):
        parts = []
        for segment in message:
            if not isinstance(segment, dict):
                continue
            data = segment.get("data") or {}
            if segment.get("type") == "text":
                parts.append(str(data.get("text") or ""))
            elif segment.get("type"):
                parts.append(f"[{segment['type']}]")
        return "".join(parts).strip()
    return str(message or "").strip()


class NapCatClient:
    """OneBot 11 WebSocket client for NapCat private messages."""

    def __init__(self, config: NapCatConfig, callback: Callable[[str, dict[str, Any]], None]):
        self.config = config
        self.callback = callback
        self._ws: Any = None
        self._ws_lock = threading.RLock()
        self._pending: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._targets: tuple[str, ...] = ()

    def start(self, targets: tuple[str, ...]) -> None:
        if not self.config.enabled:
            logger.info("NapCat WebSocket is disabled")
            return
        self._stop.clear()
        self._targets = targets
        self._thread = threading.Thread(target=self._run, args=(set(targets),), name="napcat-websocket", daemon=True)
        self._thread.start()

    def reconfigure(self, targets: tuple[str, ...], config: NapCatConfig | None = None) -> None:
        changed = (config is not None and config != self.config) or targets != self._targets
        if config is not None:
            self.config = config
        if changed:
            self.stop()
            self.start(targets)

    def stop(self) -> None:
        self._stop.set()
        with self._ws_lock:
            ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                logger.debug("NapCat WebSocket close failed", exc_info=True)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)

    def send_private(self, user_id: str, text: str) -> bool:
        if not self.config.enabled:
            raise RuntimeError("NapCat WebSocket is not enabled")
        response = self._request("send_private_msg", {"user_id": int(user_id), "message": text})
        return response.get("status") == "ok" and response.get("retcode", 0) == 0

    def friend_list(self) -> list[dict[str, Any]]:
        if not self.config.enabled:
            return []
        response = self._request("get_friend_list", {})
        if response.get("status") != "ok" or response.get("retcode", 0) != 0:
            raise RuntimeError(str(response.get("message") or "NapCat 获取好友列表失败"))
        result = []
        for item in response.get("data") or []:
            user_id = str(item.get("user_id") or "").strip()
            if user_id:
                result.append({
                    "user_id": user_id,
                    "name": str(item.get("remark") or item.get("nickname") or user_id),
                    "nickname": str(item.get("nickname") or ""),
                    "remark": str(item.get("remark") or ""),
                })
        return sorted(result, key=lambda item: (item["name"].casefold(), item["user_id"]))

    def _connect(self) -> Any:
        import websocket

        token = self.config.access_token
        headers = [f"Authorization: Bearer {token}"] if token else []
        return websocket.create_connection(
            self.config.websocket_url,
            timeout=self.config.request_timeout_seconds,
            header=headers,
        )

    def _run(self, targets: set[str]) -> None:
        while not self._stop.is_set():
            ws = None
            try:
                ws = self._connect()
                # request_timeout_seconds limits API calls and the opening
                # handshake; it must not expire an otherwise healthy stream.
                ws.settimeout(None)
                with self._ws_lock:
                    self._ws = ws
                logger.info("Connected to NapCat WebSocket: %s", self.config.websocket_url)
                while not self._stop.is_set():
                    raw = ws.recv()
                    if not raw:
                        raise RuntimeError("NapCat WebSocket closed")
                    self._handle(json.loads(raw), targets)
            except Exception as exc:
                if not self._stop.is_set():
                    logger.warning("NapCat WebSocket disconnected: %s", exc)
                    self._fail_pending(exc)
                    self._stop.wait(self.config.reconnect_seconds)
            finally:
                with self._ws_lock:
                    if self._ws is ws:
                        self._ws = None
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass

    def _handle(self, payload: dict[str, Any], targets: set[str]) -> None:
        if payload.get("status") == "failed" and int(payload.get("retcode") or 0) in {1401, 1403}:
            raise RuntimeError(str(payload.get("message") or "NapCat WebSocket authentication failed"))
        echo = payload.get("echo")
        if echo is not None:
            with self._pending_lock:
                waiter = self._pending.get(str(echo))
            if waiter:
                waiter.put(payload)
            return
        if payload.get("post_type") != "message" or payload.get("message_type") != "private":
            return
        user_id = str(payload.get("user_id") or (payload.get("sender") or {}).get("user_id") or "")
        if user_id not in targets:
            return
        self.callback(user_id, {
            "id": payload.get("message_id") or payload.get("time"),
            "time": payload.get("time"),
            "sender": user_id,
            "sender_name": str((payload.get("sender") or {}).get("nickname") or user_id),
            "type": "text",
            "content": _message_text(payload.get("message")),
            "direction": "incoming",
        })

    def _request(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        echo = uuid.uuid4().hex
        waiter: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[echo] = waiter
        temporary_ws = None
        try:
            with self._ws_lock:
                ws = self._ws
                if ws is None:
                    temporary_ws = self._connect()
                    ws = temporary_ws
                ws.send(json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False))
            if temporary_ws is not None:
                deadline = time.monotonic() + self.config.request_timeout_seconds
                while True:
                    payload = json.loads(temporary_ws.recv())
                    if str(payload.get("echo")) == echo:
                        return payload
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"NapCat request timed out: {action}")
            return waiter.get(timeout=self.config.request_timeout_seconds)
        finally:
            with self._pending_lock:
                self._pending.pop(echo, None)
            if temporary_ws is not None:
                try:
                    temporary_ws.close()
                except Exception:
                    pass

    def _fail_pending(self, exc: Exception) -> None:
        with self._pending_lock:
            pending = list(self._pending.values())
        for waiter in pending:
            try:
                waiter.put_nowait({"status": "failed", "retcode": -1, "message": str(exc)})
            except queue.Full:
                pass


class HybridGateway:
    """Keep WeChat group I/O while routing forward_to through NapCat."""

    def __init__(self, config: NapCatConfig, wechat: WeChatGateway | None = None):
        self.wechat = wechat or WeChatGateway()
        self.napcat = NapCatClient(config, self._on_private_message)
        self._direct_callback: Callable[[str, dict[str, Any]], None] | None = None
        self._targets: tuple[str, ...] = ()
        self._wechat_targets: tuple[str, ...] = ()
        self._qq_targets: tuple[str, ...] = ()

    def start(self, groups: tuple[GroupConfig, ...], callback: Callable[..., None], direct_callback: Callable[..., None] | None = None) -> None:
        self._direct_callback = direct_callback
        self._set_targets(groups)
        self.wechat.start(
            groups,
            callback,
            self._on_wechat_private if direct_callback is not None else None,
            self._wechat_targets,
        )
        self.napcat.start(self._qq_targets)

    def reconfigure(self, groups: tuple[GroupConfig, ...]) -> None:
        self._set_targets(groups)
        self.wechat.reconfigure(groups, self._wechat_targets)
        self.napcat.reconfigure(self._qq_targets)

    def reconfigure_napcat(self, config: NapCatConfig, groups: tuple[GroupConfig, ...]) -> None:
        self._set_targets(groups)
        self.napcat.reconfigure(self._qq_targets, config)

    def _set_targets(self, groups: tuple[GroupConfig, ...]) -> None:
        self._targets = tuple(dict.fromkeys(target for group in groups if group.enabled for target in group.forward_to))
        self._qq_targets = tuple(target for target in self._targets if target.isdecimal())
        self._wechat_targets = tuple(target for target in self._targets if target not in self._qq_targets)

    def stop(self) -> None:
        self.napcat.stop()
        self.wechat.stop()

    def get_history(self, group_id: str, limit: int, offset: int = 0) -> list[dict[str, Any]]:
        return self.wechat.get_history(group_id, limit, offset)

    def list_contacts(self) -> list[dict[str, Any]]:
        return self.napcat.friend_list()

    def list_groups(self) -> list[dict[str, Any]]:
        return self.wechat.list_groups()

    def search_contacts(self, keyword: str = "") -> list[dict[str, Any]]:
        return self.wechat.search_contacts(keyword)

    def send_text(self, target: str, text: str, verify: bool) -> bool:
        if target.endswith("@chatroom") or target in self._wechat_targets:
            return self.wechat.send_text(target, text, verify)
        if not self.napcat.config.enabled:
            return self.wechat.send_text(target, text, verify)
        return self.napcat.send_private(target, text)

    def _on_private_message(self, target: str, message: dict[str, Any]) -> None:
        if self._direct_callback is not None:
            message["_channel"] = "qq"
            self._direct_callback(target, message)

    def _on_wechat_private(self, target: str, message: dict[str, Any]) -> None:
        if self._direct_callback is not None:
            message["_channel"] = "wechat"
            self._direct_callback(target, message)
