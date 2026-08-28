from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from wechat_agent.agent import AgentRunner
from wechat_agent.agent_tools import ToolRuntime
from wechat_agent.models import GroupConfig, MessageBatch, OutboundMessage
from wechat_agent.store import Store


class FakeGateway:
    def __init__(self):
        self.history_calls = []

    def get_history(self, group_id, limit, offset=0):
        self.history_calls.append((group_id, limit, offset))
        return [{"id": 1, "sender": "张三", "content": "会议改到明天十点"}]

    def send_text(self, target, text, verify):
        return True


class FakeClient:
    def __init__(self):
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "history-1",
                    "type": "function",
                    "function": {"name": "get_chat_history", "arguments": '{"limit": 80}'},
                }],
            }
        if self.calls == 2:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "forward-1",
                    "type": "function",
                    "function": {
                        "name": "forward_important",
                        "arguments": json.dumps({
                            "title": "会议改期",
                            "summary": "项目会议改到明天上午十点。",
                            "category": "schedule_change",
                            "importance_score": 90,
                            "reason": "明确的会议时间变更",
                            "action_required": True,
                            "suggested_action": "确认能否参会",
                            "event_time": "2026-08-29T10:00:00+08:00"
                        }, ensure_ascii=False),
                    },
                }],
            }
        return {
            "role": "assistant",
            "content": 'FINAL_DECISION: {"important": true, "forwarded": true, "reason": "已转发会议变更"}',
        }


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.tempdir.name) / "app.db"))
        self.gateway = FakeGateway()
        self.outbound: list[OutboundMessage] = []
        self.group = GroupConfig(
            id="group-id",
            name="项目群",
            forward_to=("文件传输助手",),
            history_limit=30,
            importance_threshold=70,
        )

    def tearDown(self):
        self.store.close()
        self.tempdir.cleanup()

    def test_forward_is_not_terminal_and_final_decision_ends_loop(self):
        client = FakeClient()
        runtime = ToolRuntime(self.store, self.gateway, self.outbound.append, "Asia/Shanghai", 24)
        agent = AgentRunner(client, runtime, self.gateway, self.store, "fake", 10, "Asia/Shanghai", 24)

        outcome = agent.run(MessageBatch(self.group, [{"content": "会议时间变了"}]))

        self.assertEqual(outcome, "important")
        self.assertEqual(client.calls, 3)
        self.assertEqual(len(self.outbound), 1)
        self.assertIn("会议改期", self.outbound[0].text)
        self.assertIn(("group-id", 80, 0), self.gateway.history_calls)
        delivery = self.store.claim_delivery()
        self.assertIsNotNone(delivery)
        self.assertEqual(delivery["target"], "文件传输助手")

    def test_invalid_final_response_does_not_end_loop(self):
        self.assertIsNone(AgentRunner._parse_final_decision("done"))
        self.assertIsNone(AgentRunner._parse_final_decision('FINAL_DECISION: {"important": true}'))
        parsed = AgentRunner._parse_final_decision(
            'FINAL_DECISION: {"important": false, "forwarded": false, "reason": "普通闲聊"}'
        )
        self.assertEqual(parsed["important"], False)
        self.assertEqual(parsed["awaiting_clarification"], False)

        awaiting = AgentRunner._parse_final_decision(
            'FINAL_DECISION: {"important": false, "forwarded": false, "awaiting_clarification": true, "reason": "等待用户偏好"}'
        )
        self.assertEqual(awaiting["awaiting_clarification"], True)


if __name__ == "__main__":
    unittest.main()
