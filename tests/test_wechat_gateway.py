from __future__ import annotations

import unittest

from wechat_agent.wechat_gateway import send_response_accepted


class SendResponseTests(unittest.TestCase):
    def test_ui_sent_but_db_unconfirmed_is_accepted_without_retry(self):
        response = {"status": "失败", "message": "消息已操作发送，但数据库未确认", "data": None}
        self.assertEqual(send_response_accepted(response), (True, True))

    def test_failure_before_send_remains_retryable(self):
        response = {"status": "失败", "message": "发送失败：多次重试未完成", "data": None}
        self.assertEqual(send_response_accepted(response), (False, False))


if __name__ == "__main__":
    unittest.main()
