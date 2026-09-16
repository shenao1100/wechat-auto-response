from __future__ import annotations

import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from wechat_agent.config import load_config
from wechat_agent.service import AgentService
from wechat_agent.wechat_gateway import SendMayHaveCompletedError


class FakeGateway:
    def __init__(self):
        self.reconfigured = []
        self.stopped = False

    def get_history(self, group_id, limit, offset=0):
        return []

    def send_text(self, target, text, verify):
        return True

    def reconfigure(self, groups):
        self.reconfigured.append(groups)

    def stop(self):
        self.stopped = True


class FakeClient:
    def complete(self, messages, tools):
        raise AssertionError("AI should not be called during config reload")


class ServiceHotReloadTests(unittest.TestCase):
    def test_prompt_group_rules_and_live_subscriptions_are_reloaded(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            prompts = root / "prompts"
            prompts.mkdir()
            prompt = prompts / "group.md"
            prompt.write_text("旧规则", encoding="utf-8")
            config_path = root / "config.json"
            raw = {
                "database_path": str(root / "app.db"),
                "log_path": str(root / "app.log"),
                "ai": {"base_url": "https://example.invalid/v1", "model": "fake"},
                "groups": [{
                    "id": "group-1",
                    "name": "测试群",
                    "forward_to": ["Alice"],
                    "system_prompt_file": "prompts/group.md",
                    "importance_threshold": 70,
                }],
            }
            config_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            gateway = FakeGateway()
            service = AgentService(
                load_config(config_path),
                gateway=gateway,
                client=FakeClient(),
                config_path=str(config_path),
            )
            service._gateway_started = True
            try:
                prompt.write_text("新规则", encoding="utf-8")
                raw["groups"][0]["importance_threshold"] = 85
                raw["groups"][0]["forward_to"] = ["Bob"]
                config_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

                result = service.reload_config()

                self.assertTrue(result["applied"])
                self.assertTrue(result["changed"])
                self.assertEqual(service.config.groups[0].system_prompt, "新规则")
                self.assertEqual(service.config.groups[0].importance_threshold, 85)
                self.assertEqual(service.config.groups[0].forward_to, ("Bob",))
                self.assertEqual(gateway.reconfigured[-1][0].forward_to, ("Bob",))

                trigger = service.trigger_history_review("group-1")
                self.assertTrue(trigger["queued"])
                pending = service.store.pending_incoming()
                self.assertEqual(pending[-1]["message"]["_internal_trigger"], "history_review")

                service.on_direct_message("Bob", {"id": 88, "time": 123, "content": "明天九点提醒我交材料"})
                direct = service.store.pending_incoming()[-1]
                self.assertEqual(direct["group_id"], "direct:Bob")
                self.assertTrue(direct["message"]["_direct_chat"])
                self.assertEqual(direct["message"]["_direct_target"], "Bob")
            finally:
                service.shutdown()

    def test_send_timeout_returns_an_uncertain_failure(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            config_path = root / "config.json"
            config_path.write_text(json.dumps({
                "database_path": str(root / "app.db"),
                "ai": {"base_url": "https://example.invalid/v1", "model": "fake"},
                "groups": [{"id": "g", "name": "群", "forward_to": ["Alice"]}],
            }, ensure_ascii=False), encoding="utf-8")
            gateway = FakeGateway()
            gateway.send_text = lambda target, text, verify: (time.sleep(2), True)[1]
            service = AgentService(
                replace(load_config(config_path), delivery_timeout_seconds=1),
                gateway=gateway,
                client=FakeClient(),
            )
            try:
                started = time.monotonic()
                success, error = service._send_with_timeout("Alice", "hello")
                elapsed = time.monotonic() - started
                self.assertFalse(success)
                self.assertIn("timed out", error)
                self.assertLess(elapsed, 1.8)
            finally:
                service.shutdown()

    def test_timeout_is_marked_failed_without_an_automatic_retry(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            config_path = root / "config.json"
            config_path.write_text(json.dumps({
                "database_path": str(root / "app.db"),
                "ai": {"base_url": "https://example.invalid/v1", "model": "fake"},
                "groups": [{"id": "g", "name": "group", "forward_to": ["Alice"]}],
            }), encoding="utf-8")
            gateway = FakeGateway()
            service = AgentService(load_config(config_path), gateway=gateway, client=FakeClient())
            try:
                service.store.enqueue_message(["Alice"], "hello")
                delivery = service.store.claim_delivery()
                error = "send timed out after 60s"
                status = service.store.finish_delivery(delivery["id"], False, error, max_attempts=1)
                self.assertEqual(status, "failed")
                self.assertIsNone(service.store.claim_delivery())
            finally:
                service.shutdown()

    def test_uncertain_send_is_marked_failed_without_retry(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            config_path = root / "config.json"
            config_path.write_text(json.dumps({
                "database_path": str(root / "app.db"),
                "ai": {"base_url": "https://example.invalid/v1", "model": "fake"},
                "groups": [{"id": "g", "name": "group", "forward_to": ["Alice"]}],
            }), encoding="utf-8")
            gateway = FakeGateway()
            gateway.send_text = lambda target, text, verify: (_ for _ in ()).throw(SendMayHaveCompletedError("unverified"))
            service = AgentService(load_config(config_path), gateway=gateway, client=FakeClient())
            try:
                outbox_id = service.store.enqueue_message(["Alice"], "hello")
                delivery = service.store.claim_delivery()
                success, error = service._send_with_timeout("Alice", "hello")
                self.assertFalse(success)
                self.assertTrue(error.startswith("send may have completed: "))
                self.assertEqual(service.store.finish_delivery(delivery["id"], success, error, max_attempts=1), "failed")
                self.assertEqual(service.store.claim_delivery(), None)
                self.assertEqual(outbox_id, 1)
            finally:
                service.shutdown()


if __name__ == "__main__":
    unittest.main()
