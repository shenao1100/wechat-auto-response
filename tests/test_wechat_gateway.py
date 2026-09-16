from __future__ import annotations

import threading
import unittest

import zstandard

from wechat_agent.models import GroupConfig
from wechat_agent.wechat_gateway import (
    ReplicaWeChatSender,
    SendMayHaveCompletedError,
    WeChatGateway,
    decode_wechat_content,
    normalize_message,
)


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
            return [{"username": value, "nick_name": "Test Group", "remark": "Group Remark"}]
        return [
            {"username": "wxid_direct", "nick_name": value, "remark": ""},
            {"username": "123@chatroom", "nick_name": value, "remark": ""},
        ]

    def get_nickname(self, value):
        return value


class FakeReplicaGUI:
    def __init__(self):
        self.calls = []

    def send_msg(self, text, who=None, verify=False):
        self.calls.append({"text": text, "who": who, "verify": verify})
        return {"status": "success", "message": "sent"}


class FailingReplicaGUI:
    def send_msg(self, _text, who=None, verify=False):
        raise RuntimeError("WeChat window unavailable")


class ReplicaWeChatSenderTests(unittest.TestCase):
    def test_send_uses_replica_gui_with_database_verification(self):
        gui = FakeReplicaGUI()
        sender = ReplicaWeChatSender(lambda: gui)

        self.assertTrue(sender.send_text("File Transfer", "test message"))
        self.assertEqual(gui.calls, [{"text": "test message", "who": "File Transfer", "verify": True}])

    def test_send_error_is_propagated_for_outbox_retry(self):
        sender = ReplicaWeChatSender(lambda: FailingReplicaGUI())

        with self.assertRaisesRegex(RuntimeError, "window unavailable"):
            sender.send_text("File Transfer", "test message")

    def test_failed_replica_response_is_propagated_for_outbox_retry(self):
        class FailedResponseGUI:
            def send_msg(self, _text, who=None, verify=False):
                return {"status": "failure", "message": "not verified"}

        with self.assertRaisesRegex(RuntimeError, "not verified"):
            ReplicaWeChatSender(FailedResponseGUI).send_text("File Transfer", "test message")

    def test_unverified_post_send_response_is_not_safe_to_retry(self):
        class UnverifiedResponseGUI:
            def send_msg(self, text, who=None, verify=False):
                return {"status": "failure", "message": "unverified", "data": {"content": text}}

        with self.assertRaises(SendMayHaveCompletedError):
            ReplicaWeChatSender(UnverifiedResponseGUI).send_text("File Transfer", "test message")


class MessageDecodingTests(unittest.TestCase):
    def test_sort_sequence_is_used_as_history_id_fallback(self):
        message = normalize_message({"sort_seq": 987654, "content": "notice"})

        self.assertEqual(message["id"], 987654)

    def test_zstd_text_restores_sender_and_body(self):
        compressed = zstandard.ZstdCompressor().compress(b"wxid_sender:\nmeeting at 10")

        sender, content = decode_wechat_content(compressed, "text")

        self.assertEqual(sender, "wxid_sender")
        self.assertEqual(content, "meeting at 10")

    def test_zstd_card_is_summarized_instead_of_returning_placeholder(self):
        xml = """wxid_sender:
<?xml version="1.0"?><msg><appmsg><title>Meeting update</title>
<des>Moved to 10 AM</des><url>https://example.com/notice</url></appmsg></msg>"""
        compressed = zstandard.ZstdCompressor().compress(xml.encode("utf-8"))

        sender, content = decode_wechat_content(compressed, "card")

        self.assertEqual(sender, "wxid_sender")
        self.assertIn("Meeting update", content)
        self.assertIn("Moved to 10 AM", content)
        self.assertIn("https://example.com/notice", content)


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
        gui = FakeReplicaGUI()
        gateway.sender = ReplicaWeChatSender(lambda: gui)

        self.assertTrue(gateway.send_text("123@chatroom", "relay content", verify=True))
        self.assertEqual(gui.calls[0]["who"], "Group Remark")


if __name__ == "__main__":
    unittest.main()
