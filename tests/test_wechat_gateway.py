from __future__ import annotations

import unittest
import threading
import zstandard

from wechat_agent.models import GroupConfig
from wechat_agent.wechat_gateway import WeChatGateway, decode_wechat_content, send_response_accepted


class FakeListener:
    def __init__(self):
        self.listeners = []

    def add_listener(self, user, callback):
        self.listeners.append((user, callback))

    def remove_listener(self, user, callback):
        self.listeners.remove((user, callback))


class SendResponseTests(unittest.TestCase):
    def test_ui_sent_but_db_unconfirmed_is_accepted_without_retry(self):
        response = {"status": "失败", "message": "消息已操作发送，但数据库未确认", "data": None}
        self.assertEqual(send_response_accepted(response), (True, True))

    def test_failure_before_send_remains_retryable(self):
        response = {"status": "失败", "message": "发送失败：多次重试未完成", "data": None}
        self.assertEqual(send_response_accepted(response), (False, False))


class MessageDecodingTests(unittest.TestCase):
    def test_zstd_text_restores_sender_and_body(self):
        compressed = zstandard.ZstdCompressor().compress(
            "wxid_sender:\n@所有人 明天十点在 M103 开班会".encode("utf-8")
        )

        sender, content = decode_wechat_content(compressed, "文本")

        self.assertEqual(sender, "wxid_sender")
        self.assertEqual(content, "@所有人 明天十点在 M103 开班会")

    def test_zstd_card_is_summarized_instead_of_returning_placeholder(self):
        xml = """wxid_sender:
<?xml version="1.0"?><msg><appmsg><title>班会时间调整</title>
<des>改到明天十点</des><url>https://example.com/notice</url></appmsg></msg>"""
        compressed = zstandard.ZstdCompressor().compress(xml.encode("utf-8"))

        sender, content = decode_wechat_content(compressed, "文件/链接/卡片")

        self.assertEqual(sender, "wxid_sender")
        self.assertIn("班会时间调整", content)
        self.assertIn("改到明天十点", content)
        self.assertIn("https://example.com/notice", content)
        self.assertNotEqual(content, "[文件/链接/卡片]")


class SubscriptionTests(unittest.TestCase):
    def test_reconfigure_resolves_user_before_binding_group_callback(self):
        gateway = WeChatGateway.__new__(WeChatGateway)
        gateway.listener = FakeListener()
        gateway._subscriptions = []
        gateway._subscription_lock = threading.RLock()
        gateway._group_callback = lambda _group, _message: None
        gateway._direct_callback = None
        gateway.resolve_user = lambda value: f"resolved:{value}"
        gateway._advance_watermark = lambda _user: None
        gateway._normalize_db_message = lambda user, message: {"user": user, "message": message}
        group = GroupConfig(id="group-id", name="Test", forward_to=())

        gateway.reconfigure((group,))

        self.assertEqual(gateway.listener.listeners[0][0], "resolved:group-id")


if __name__ == "__main__":
    unittest.main()
