from __future__ import annotations

import json
import hashlib
import logging
import queue
import threading
from pathlib import Path
from typing import Any

from .agent import AgentRunner
from .agent_tools import ToolRuntime
from .aggregator import MessageAggregator
from .ai_client import OpenAICompatibleClient
from .config import load_config
from .models import AppConfig, MessageBatch
from .store import Store
from .timeutils import get_timezone
from .wechat_gateway import Gateway, WeChatGateway

logger = logging.getLogger(__name__)


class AgentService:
    def __init__(
        self,
        config: AppConfig,
        gateway: Gateway | None = None,
        client: Any | None = None,
        config_path: str | None = None,
    ):
        self.config = config
        self.config_path = str(Path(config_path).resolve()) if config_path else None
        self._config_lock = threading.RLock()
        self._config_signature: tuple[Any, ...] | None = None
        self._gateway_started = False
        self._owns_client = client is None
        self.store = Store(config.database_path)
        recovered_outbox = self.store.recover_outbox()
        recovered_schedules = self.store.recover_sending_schedules()
        if recovered_outbox or recovered_schedules:
            logger.warning(
                "Recovered interrupted delivery state: outbox=%d schedules=%d",
                recovered_outbox,
                recovered_schedules,
            )
        self.client = client or OpenAICompatibleClient(config.ai)
        self.gateway = gateway or WeChatGateway(poll_interval=config.poll_interval)
        self.stop_event = threading.Event()
        self._shutdown_lock = threading.Lock()
        self._shutdown_done = False
        self.aggregator = MessageAggregator()
        self._active_inbox_ids: set[int] = set()
        self._active_inbox_lock = threading.Lock()
        self.agent_queue: queue.Queue[tuple[MessageBatch, int] | None] = queue.Queue()
        self.outbound_wake = threading.Event()
        self.runtime = ToolRuntime(
            self.store,
            self.gateway,
            lambda _job: self.outbound_wake.set(),
            config.timezone,
            config.duplicate_window_hours,
        )
        self.agent = self._build_agent(config)
        self.threads: list[threading.Thread] = []
        if self.config_path:
            self._config_signature = self._configuration_fingerprint()

    def _build_agent(self, config: AppConfig) -> AgentRunner:
        return AgentRunner(
            self.client,
            self.runtime,
            self.gateway,
            self.store,
            config.ai.model,
            config.ai.max_steps,
            config.timezone,
            config.duplicate_window_hours,
        )

    def reload_config(self) -> dict[str, Any]:
        """Atomically apply reloadable configuration to future messages."""
        if not self.config_path:
            return {"applied": False, "reason": "config path is unavailable"}
        new_config = load_config(self.config_path)
        with self._config_lock:
            old_config = self.config
            static_changes = [
                name for name in ("database_path", "log_path", "poll_interval")
                if getattr(old_config, name) != getattr(new_config, name)
            ]
            if static_changes:
                raise ValueError(f"这些配置仍需重启：{', '.join(static_changes)}")
            if new_config == old_config:
                self._config_signature = self._configuration_fingerprint()
                return {"applied": True, "changed": False, "groups": len(new_config.groups)}

            if new_config.ai != old_config.ai:
                if not self._owns_client:
                    raise ValueError("使用自定义 AI client 时不能热重载 ai 配置")
                self.client = OpenAICompatibleClient(new_config.ai)
            self.runtime.timezone = get_timezone(new_config.timezone)
            self.runtime.duplicate_window_hours = new_config.duplicate_window_hours
            self.config = new_config
            self.agent = self._build_agent(new_config)
            if self._gateway_started and hasattr(self.gateway, "reconfigure"):
                self.gateway.reconfigure(new_config.groups)  # type: ignore[attr-defined]
            self._config_signature = self._configuration_fingerprint()
        self._restore_pending_incoming()
        logger.info("Configuration hot-reloaded: groups=%d model=%s", len(new_config.groups), new_config.ai.model)
        return {"applied": True, "changed": True, "groups": len(new_config.groups)}

    def _configuration_fingerprint(self) -> tuple[Any, ...]:
        if not self.config_path:
            return ()
        config_path = Path(self.config_path)
        paths = [config_path]
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            for group in raw.get("groups") or []:
                prompt_file = group.get("system_prompt_file")
                if prompt_file:
                    paths.append((config_path.parent / str(prompt_file)).resolve())
        except (OSError, json.JSONDecodeError):
            pass
        signature: list[Any] = []
        for path in dict.fromkeys(paths):
            try:
                stat = path.stat()
                signature.extend((str(path), stat.st_mtime_ns, stat.st_size))
            except OSError:
                signature.extend((str(path), None, None))
        return tuple(signature)

    def on_message(self, group: Any, message: dict[str, Any]) -> None:
        if message.get("direction") == "self":
            logger.debug("Ignored self message in %s", group.name)
            return
        raw_key = message.get("id")
        if raw_key is None:
            serialized = json.dumps(message, ensure_ascii=False, sort_keys=True)
            raw_key = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        else:
            raw_key = f"{raw_key}:{message.get('time') or ''}"
        inbox_id, inserted = self.store.save_incoming(group.id, str(raw_key), message)
        if not inserted:
            logger.debug("Ignored duplicate incoming message group=%s key=%s", group.name, raw_key)
            return
        message = dict(message)
        message["_inbox_id"] = inbox_id
        logger.info("New message group=%s sender=%s", group.name, message.get("sender_name") or message.get("sender"))
        self._queue_incoming(group, message)

    def on_direct_message(self, target: str, message: dict[str, Any]) -> None:
        if message.get("direction") == "self":
            return
        content = str(message.get("content") or "").strip()
        clarification = self.store.find_clarification_for_reply(target, content)
        if clarification is None:
            logger.debug("Direct message from %s did not match a pending clarification", target)
            return
        result = self.store.answer_clarification(int(clarification["id"]), content, target)
        group = next((item for item in self.config.groups if item.id == result["group_id"]), None)
        if group is not None and result["inserted"]:
            self._queue_incoming(group, result["message"])
        logger.info("Clarification id=%s answered by %s and stored in memory", clarification["id"], target)

    def run_forever(self) -> None:
        self.threads = [
            threading.Thread(target=self._aggregate_loop, name="message-aggregator", daemon=True),
            threading.Thread(target=self._agent_loop, name="agent-worker", daemon=True),
            threading.Thread(target=self._send_loop, name="wechat-sender", daemon=True),
            threading.Thread(target=self._schedule_loop, name="schedule-worker", daemon=True),
            threading.Thread(target=self._inbox_recovery_loop, name="inbox-recovery", daemon=True),
        ]
        if self.config_path:
            self.threads.append(threading.Thread(target=self._config_watch_loop, name="config-watcher", daemon=True))
        for thread in self.threads:
            thread.start()
        self._restore_pending_incoming()
        try:
            self.gateway.start(self.config.groups, self.on_message, self.on_direct_message)  # type: ignore[attr-defined]
            self._gateway_started = True
            logger.info("Service started with %d configured groups", len(self.config.groups))
            while True:
                if self.stop_event.wait(1.0):
                    break
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        with self._shutdown_lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True
        logger.info("Stopping service")
        self.stop_event.set()
        self.aggregator.wake()
        try:
            self.gateway.stop()  # type: ignore[attr-defined]
        except Exception:
            logger.exception("Failed to stop WeChat listener")
        self._gateway_started = False
        self.agent_queue.put(None)
        self.outbound_wake.set()
        for thread in self.threads:
            thread.join(timeout=5)
        self.store.close()

    def _aggregate_loop(self) -> None:
        while not self.stop_event.is_set():
            batch = self.aggregator.pop_ready(self.stop_event)
            if batch is not None:
                logger.info("Queued batch group=%s messages=%d", batch.group.name, len(batch.messages))
                self.agent_queue.put((batch, 0))

    def _agent_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                item = self.agent_queue.get(timeout=1)
            except queue.Empty:
                continue
            if item is None:
                break
            batch, attempt = item
            inbox_ids = [int(message["_inbox_id"]) for message in batch.messages if message.get("_inbox_id") is not None]
            try:
                outcome = self.agent.run(batch)
                success = outcome in {"important", "ignored", "awaiting_clarification"}
                self.store.finish_incoming(inbox_ids, success, None if success else outcome)
                self._release_inbox_ids(inbox_ids)
            except Exception as exc:
                logger.exception("Agent run failed group=%s attempt=%d", batch.group.name, attempt + 1)
                if attempt < 2 and not self.stop_event.wait(2 ** attempt):
                    self.agent_queue.put((batch, attempt + 1))
                else:
                    self.store.record_run(batch.group.id, "error", str(exc)[:2000])
                    self.store.finish_incoming(inbox_ids, False, str(exc)[:2000])
                    self._release_inbox_ids(inbox_ids)
            finally:
                self.agent_queue.task_done()

    def _send_loop(self) -> None:
        while not self.stop_event.is_set():
            delivery = self.store.claim_delivery()
            if delivery is None:
                self.outbound_wake.wait(1.0)
                self.outbound_wake.clear()
                continue
            target = str(delivery["target"])
            success = False
            error = "send returned failure"
            try:
                success = self.gateway.send_text(target, str(delivery["text"]), self.config.send_verify)
            except Exception as exc:
                error = str(exc)[:2000]
                logger.exception("Send failed target=%s delivery=%s", target, delivery["id"])
            if success:
                logger.info("Sent delivery id=%s to %s", delivery["id"], target)
            status = self.store.finish_delivery(int(delivery["id"]), success, error)
            if not success:
                logger.warning("Delivery id=%s target=%s outbox_status=%s", delivery["id"], target, status)
                self.stop_event.wait(min(30, 2 ** min(5, int(delivery["attempts"]))))

    def _schedule_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                queued = self.store.queue_schedule_outbox()
                if queued:
                    logger.info("Queued %d due schedules into outbox", len(queued))
                    self.outbound_wake.set()
            except Exception:
                logger.exception("Schedule polling failed")
            self.stop_event.wait(self.config.schedule_poll_seconds)

    def _restore_pending_incoming(self) -> None:
        groups = {group.id: group for group in self.config.groups if group.enabled}
        restored = 0
        for item in self.store.pending_incoming():
            group = groups.get(item["group_id"])
            if group is None:
                logger.warning("Pending inbox item belongs to an unconfigured group: %s", item["group_id"])
                continue
            if self._queue_incoming(group, item["message"]):
                restored += 1
        if restored:
            logger.info("Restored %d pending inbox messages", restored)

    def _queue_incoming(self, group: Any, message: dict[str, Any]) -> bool:
        inbox_id = message.get("_inbox_id")
        if inbox_id is not None:
            with self._active_inbox_lock:
                if int(inbox_id) in self._active_inbox_ids:
                    return False
                self._active_inbox_ids.add(int(inbox_id))
        self.aggregator.submit(group, message)
        return True

    def _release_inbox_ids(self, inbox_ids: list[int]) -> None:
        with self._active_inbox_lock:
            self._active_inbox_ids.difference_update(inbox_ids)

    def _inbox_recovery_loop(self) -> None:
        while not self.stop_event.wait(5.0):
            try:
                self._restore_pending_incoming()
            except Exception:
                logger.exception("Periodic inbox recovery failed")

    def _config_watch_loop(self) -> None:
        while not self.stop_event.wait(1.0):
            fingerprint = self._configuration_fingerprint()
            if fingerprint == self._config_signature:
                continue
            try:
                self.reload_config()
            except Exception:
                logger.exception("Configuration hot-reload failed; keeping previous runtime configuration")
                self._config_signature = fingerprint


def configure_logging(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(path, encoding="utf-8")],
    )
