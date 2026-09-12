from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from wechat_agent.config_manager import ConfigManager
from wechat_agent.config import load_config


class ConfigManagerTests(unittest.TestCase):
    def test_napcat_token_is_stored_but_not_returned_by_settings(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "config.json"
            path.write_text(json.dumps({
                "ai": {"base_url": "https://example", "model": "fake"},
                "napcat": {"enabled": True, "websocket_url": "ws://127.0.0.1:3001", "access_token": "secret"},
                "groups": [{"id": "group", "forward_to": ["123"]}],
            }), encoding="utf-8")
            manager = ConfigManager(str(path))

            settings = manager.public_settings()["napcat"]
            self.assertEqual(settings["access_token"], "")
            self.assertTrue(settings["access_token_configured"])
            self.assertEqual(load_config(path).napcat.access_token, "secret")

            manager.replace_napcat({"enabled": True, "websocket_url": "ws://localhost:3001", "access_token": ""})
            saved = json.loads(path.read_text(encoding="utf-8"))["napcat"]
            self.assertEqual(saved["access_token"], "secret")
            self.assertNotIn("access_token_env", saved)

    def test_group_update_preserves_ai_configuration(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "prompts").mkdir()
            (root / "prompts" / "class.md").write_text("rules", encoding="utf-8")
            path = root / "config.json"
            path.write_text(json.dumps({"ai": {"base_url": "https://example", "api_key_env": "SECRET_ENV"}, "groups": []}), encoding="utf-8")
            manager = ConfigManager(str(path))
            manager.replace_groups([{
                "id": "1@chatroom", "name": "Class", "forward_to": ["Alice"],
                "system_prompt_file": "prompts/class.md", "enabled": True,
                "chain": {"enabled": True, "rules": [{
                    "name": "晚点名", "match_keywords": ["晚点名"],
                    "exclude_keywords": ["无需接龙"],
                    "entry_template": "{{profile.dormitory}} {{profile.name}}已返校",
                    "self_identifiers": ["{{profile.name}}"],
                }]},
            }])
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["ai"]["api_key_env"], "SECRET_ENV")
            self.assertEqual(raw["groups"][0]["id"], "1@chatroom")
            self.assertTrue(raw["groups"][0]["chain"]["enabled"])
            self.assertEqual(raw["groups"][0]["chain"]["rules"][0]["match_keywords"], ["晚点名"])

    def test_prompt_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "config.json"
            path.write_text('{"groups": []}', encoding="utf-8")
            manager = ConfigManager(str(path))
            with self.assertRaises(ValueError):
                manager.write_prompt("../secret.md", "bad")
