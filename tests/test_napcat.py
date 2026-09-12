from __future__ import annotations

import unittest
from unittest.mock import patch

from wechat_agent.models import NapCatConfig
from wechat_agent.napcat import NapCatClient, _message_text


class NapCatTests(unittest.TestCase):
    def test_connect_uses_token_from_config(self):
        created = []

        with patch("websocket.create_connection", side_effect=lambda *args, **kwargs: created.append((args, kwargs)) or object()):
            client = NapCatClient(
                NapCatConfig(enabled=True, access_token="local-token"),
                lambda _target, _message: None,
            )
            client._connect()

        self.assertEqual(created[0][1]["header"], ["Authorization: Bearer local-token"])

    def test_authentication_failure_is_reported(self):
        client = NapCatClient(NapCatConfig(enabled=True), lambda _target, _message: None)

        with self.assertRaisesRegex(RuntimeError, "token invalid"):
            client._handle({"status": "failed", "retcode": 1403, "message": "token invalid"}, set())

    def test_listener_disables_socket_read_timeout_after_connect(self):
        client = NapCatClient(NapCatConfig(enabled=True), lambda _target, _message: None)

        class FakeSocket:
            timeout = "unchanged"

            def settimeout(self, value):
                self.timeout = value

            def recv(self):
                client._stop.set()
                return '{"post_type":"meta_event","meta_event_type":"heartbeat"}'

            def close(self):
                pass

        socket = FakeSocket()
        client._connect = lambda: socket
        client._run(set())

        self.assertIsNone(socket.timeout)

    def test_message_segments_are_converted_to_text(self):
        self.assertEqual(
            _message_text([
                {"type": "text", "data": {"text": "提醒我"}},
                {"type": "image", "data": {"file": "x"}},
                {"type": "text", "data": {"text": " 明天"}},
            ]),
            "提醒我[image] 明天",
        )

    def test_private_event_only_reaches_configured_target(self):
        received = []
        client = NapCatClient(NapCatConfig(enabled=True), lambda target, message: received.append((target, message)))
        client._handle({
            "post_type": "message",
            "message_type": "private",
            "user_id": 123,
            "message_id": 9,
            "time": 10,
            "message": [{"type": "text", "data": {"text": "hello"}}],
            "sender": {"nickname": "Alice", "user_id": 123},
        }, {"123"})
        client._handle({"post_type": "message", "message_type": "private", "user_id": 456, "message": "ignored"}, {"123"})

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][0], "123")
        self.assertEqual(received[0][1]["content"], "hello")


if __name__ == "__main__":
    unittest.main()
