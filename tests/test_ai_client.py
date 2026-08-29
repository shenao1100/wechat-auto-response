from __future__ import annotations

import json
import os
import ssl
import unittest
from unittest.mock import patch

from wechat_agent.ai_client import OpenAICompatibleClient
from wechat_agent.models import AIConfig


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps({"choices": [{"message": {"role": "assistant", "content": "ok"}}]}).encode()


class AIClientSSLTests(unittest.TestCase):
    def test_ssl_verification_can_be_disabled_explicitly(self):
        config = AIConfig(
            base_url="https://example.invalid/v1",
            model="fake",
            verify_ssl=False,
        )
        with patch.dict(os.environ, {"AI_API_KEY": "test-key"}):
            client = OpenAICompatibleClient(config)
        with patch("wechat_agent.ai_client.urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
            result = client.complete([], [])

        context = urlopen.call_args.kwargs["context"]
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertFalse(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_NONE)
        self.assertEqual(result["content"], "ok")

    def test_ssl_verification_remains_enabled_by_default(self):
        config = AIConfig(base_url="https://example.invalid/v1", model="fake")
        with patch.dict(os.environ, {"AI_API_KEY": "test-key"}):
            client = OpenAICompatibleClient(config)
        with patch("wechat_agent.ai_client.urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
            client.complete([], [])

        self.assertIsNone(urlopen.call_args.kwargs["context"])

    def test_full_request_payload_logging_excludes_api_key(self):
        config = AIConfig(
            base_url="https://example.invalid/v1",
            model="fake",
            log_requests=True,
        )
        messages = [{"role": "system", "content": "完整系统提示"}]
        tools = [{"type": "function", "function": {"name": "demo"}}]
        with patch.dict(os.environ, {"AI_API_KEY": "secret-that-must-not-be-logged"}):
            client = OpenAICompatibleClient(config)
        with patch("wechat_agent.ai_client.urllib.request.urlopen", return_value=FakeResponse()):
            with self.assertLogs("wechat_agent.ai_client", level="INFO") as captured:
                client.complete(messages, tools)

        output = "\n".join(captured.output)
        self.assertIn("完整系统提示", output)
        self.assertIn('"name": "demo"', output)
        self.assertNotIn("secret-that-must-not-be-logged", output)


if __name__ == "__main__":
    unittest.main()
