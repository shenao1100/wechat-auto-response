from __future__ import annotations

import unittest
import threading
import zstandard

from wechat_agent.models import GroupConfig
from wechat_agent.wechat_gateway import PyWeChatSender, WeChatGateway, decode_wechat_content, normalize_message


class FakeListener:
    def __init__(self):
        self.listeners = []

    def add_listener(self, user, callback):
        self.listeners.append((user, callback))

    def remove_listener(self, user, callback):
        self.listeners.remove((user, callback))


class FakeContactDB:
    def search_contact(self, value):
        if value == "123@chatroom":
            return [{"username": value, "nick_name": "测试群", "remark": "群备注"}]
        return [
            {"username": "wxid_direct", "nick_name": value, "remark": ""},
            {"username": "123@chatroom", "nick_name": value, "remark": ""},
        ]

    def get_nickname(self, value):
        return value


class FakeMessagesAPI:
    def __init__(self):
        self.calls = []

    def send_messages_to_friend(self, **kwargs):
        self.calls.append(kwargs)


class FailingMessagesAPI:
    def send_messages_to_friend(self, **_kwargs):
        raise RuntimeError("微信窗口不可用")


class PyWeChatSenderTests(unittest.TestCase):
    def test_send_uses_pyweixin_messages_api_without_closing_wechat(self):
        api = FakeMessagesAPI()
        sender = PyWeChatSender(api)

        self.assertTrue(sender.send_text("文件传输助手", "测试消息"))
        self.assertEqual(api.calls, [{
            "friend": "文件传输助手",
            "messages": ["测试消息"],
            "search_pages": 0,
            "clear": True,
            "close_weixin": False,
        }])

    def test_send_error_is_propagated_for_outbox_retry(self):
        sender = PyWeChatSender(FailingMessagesAPI())

        with self.assertRaisesRegex(RuntimeError, "微信窗口不可用"):
            sender.send_text("文件传输助手", "测试消息")


class MessageDecodingTests(unittest.TestCase):
    def test_sort_sequence_is_used_as_history_id_fallback(self):
        message = normalize_message({"sort_seq": 987654, "content": "notice"})

        self.assertEqual(message["id"], 987654)

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
    def test_ambiguous_name_prefers_single_direct_contact_over_same_named_group(self):
        gateway = WeChatGateway.__new__(WeChatGateway)
        gateway.db = FakeContactDB()
        gateway._resolved_users = {}

        self.assertEqual(gateway.resolve_user("NTG"), "wxid_direct")

    def test_origin_source_distinguishes_private_outgoing_and_incoming_messages(self):
        gateway = WeChatGateway.__new__(WeChatGateway)
        gateway.db = FakeContactDB()
        gateway._sender_names = {}

        outgoing = gateway._normalize_db_message(
            "wxid_direct", {"local_id": 1, "content": "bot reply", "sender_id": 4, "origin_source": 1}
        )
        incoming = gateway._normalize_db_message(
            "wxid_direct", {"local_id": 2, "content": "user message", "sender_id": 6, "origin_source": 2}
        )

        self.assertEqual(outgoing["direction"], "self")
        self.assertEqual(incoming["direction"], "incoming")

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

    def test_send_resolves_internal_group_id_to_visible_name(self):
        gateway = WeChatGateway.__new__(WeChatGateway)
        gateway.db = FakeContactDB()
        gateway.sender = PyWeChatSender(FakeMessagesAPI())

        self.assertTrue(gateway.send_text("123@chatroom", "接龙内容", verify=True))
        self.assertEqual(gateway.sender._messages_api.calls[0]["friend"], "群备注")


if __name__ == "__main__":
    unittest.main()
