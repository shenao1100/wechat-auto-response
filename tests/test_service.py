from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from wechat_agent.config import load_config
from wechat_agent.service import AgentService


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
                self.assertEqual(direct["group_id"], "Bob")
                self.assertTrue(direct["message"]["_direct_chat"])
                self.assertEqual(direct["message"]["_direct_target"], "Bob")
            finally:
                service.shutdown()


if __name__ == "__main__":
    unittest.main()
