from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from wechat_agent.main import main


class CLITests(unittest.TestCase):
    def test_status_does_not_require_ai_key_or_wechat_login(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            config = {
                "database_path": str(root / "app.db"),
                "log_path": str(root / "app.log"),
                "ai": {"base_url": "https://example.invalid/v1", "model": "fake"},
                "groups": [{"id": "g1", "forward_to": ["filehelper"]}],
            }
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            output = io.StringIO()
            with patch.object(sys, "argv", ["wechat-agent", "--config", str(config_path), "--status"]):
                with contextlib.redirect_stdout(output):
                    code = main()
            self.assertEqual(code, 0)
            status = json.loads(output.getvalue())
            self.assertIn("inbox", status)
            self.assertIn("deliveries", status)

    def test_default_command_starts_agent_and_embedded_webui(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            config_path = root / "config.json"
            config_path.write_text(json.dumps({
                "database_path": str(root / "app.db"),
                "log_path": str(root / "app.log"),
                "ai": {"base_url": "https://example.invalid/v1", "model": "fake"},
                "groups": [{"id": "g1", "name": "G1", "forward_to": ["filehelper"]}],
            }), encoding="utf-8")
            service = MagicMock()
            service.gateway = MagicMock()
            web = MagicMock()
            with patch.object(sys, "argv", ["wechat-agent", "--config", str(config_path)]):
                with patch("wechat_agent.main.AgentService", return_value=service):
                    with patch("wechat_agent.web_server.EmbeddedWebServer", return_value=web):
                        with patch("wechat_agent.main.configure_logging"):
                            with patch("wechat_agent.main.signal.signal"):
                                code = main()
            self.assertEqual(code, 0)
            web.start.assert_called_once_with()
            service.run_forever.assert_called_once_with()
            web.stop.assert_called_once_with()

    def test_legacy_web_flag_still_starts_combined_service(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            config_path = root / "config.json"
            config_path.write_text(json.dumps({
                "database_path": str(root / "app.db"),
                "log_path": str(root / "app.log"),
                "ai": {"base_url": "https://example.invalid/v1", "model": "fake"},
                "groups": [{"id": "g1", "name": "G1", "forward_to": ["filehelper"]}],
            }), encoding="utf-8")
            service = MagicMock()
            service.gateway = MagicMock()
            web = MagicMock()
            with patch.object(sys, "argv", ["wechat-agent", "--config", str(config_path), "--web"]):
                with patch("wechat_agent.main.AgentService", return_value=service):
                    with patch("wechat_agent.web_server.EmbeddedWebServer", return_value=web):
                        with patch("wechat_agent.main.configure_logging"):
                            with patch("wechat_agent.main.signal.signal"):
                                code = main()
            self.assertEqual(code, 0)
            web.start.assert_called_once_with()
            service.run_forever.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
