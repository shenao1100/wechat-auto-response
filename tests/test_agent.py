from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from wechat_agent.agent import AgentRunner
from wechat_agent.agent_tools import ToolRuntime, parse_chain_message
from wechat_agent.models import ChainRuleConfig, GroupConfig, MessageBatch, OutboundMessage
from wechat_agent.store import Store


class FakeGateway:
    def __init__(self):
        self.history_calls = []

    def get_history(self, group_id, limit, offset=0):
        self.history_calls.append((group_id, limit, offset))
        return [{"id": 1, "sender": "张三", "content": "会议改到明天十点"}]

    def send_text(self, target, text, verify):
        return True


class ChainGateway(FakeGateway):
    def get_history(self, group_id, limit, offset=0):
        self.history_calls.append((group_id, limit, offset))
        return [{
            "id": 2,
            "content": "[文件/链接/卡片] #接龙\n今日晚点名\n\n1. 502 2/4\n2. 401 4/4",
            "direction": "incoming",
        }]


class FakeClient:
    def __init__(self):
        self.calls = 0
        self.initial_payload = None

    def complete(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            self.initial_payload = json.loads(messages[-1]["content"])
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
        if self.calls == 3:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "read-1",
                    "type": "function",
                    "function": {
                        "name": "mark_history_read",
                        "arguments": '{"message_ids": [1]}',
                    },
                }],
            }
        return {
            "role": "assistant",
            "content": 'FINAL_DECISION: {"important": true, "forwarded": true, "reason": "已转发会议变更"}',
        }


class CapturingIgnoreClient:
    def __init__(self):
        self.messages = None

    def complete(self, messages, tools):
        self.messages = messages
        self.tool_names = [tool["function"]["name"] for tool in tools]
        return {
            "role": "assistant",
            "content": 'FINAL_DECISION: {"important": false, "forwarded": false, "reason": "历史中没有待处理事件"}',
        }


class PrematureMarkClient:
    def __init__(self):
        self.calls = 0
        self.messages = None

    def complete(self, messages, tools):
        self.calls += 1
        self.messages = messages
        if self.calls == 1:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "read-too-early",
                    "type": "function",
                    "function": {"name": "mark_history_read", "arguments": '{"message_ids": [1]}'},
                }],
            }
        return {
            "role": "assistant",
            "content": 'FINAL_DECISION: {"important": false, "forwarded": false, "reason": "not important"}',
        }


class DirectScheduleClient:
    def __init__(self):
        self.calls = 0
        self.tool_names = []

    def complete(self, messages, tools):
        self.calls += 1
        self.tool_names = [tool["function"]["name"] for tool in tools]
        if self.calls == 1:
            return {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "schedule-direct", "type": "function",
                    "function": {"name": "schedule_reminder", "arguments": json.dumps({
                        "title": "Submit report", "content": "Submit the monthly report",
                        "run_at": "2099-09-01T09:00:00+08:00",
                    })},
                }],
            }
        if self.calls == 2:
            return {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "reply-direct", "type": "function",
                    "function": {"name": "reply_to_sender", "arguments": json.dumps({
                        "text": "已创建提醒：2099年9月1日9:00提交月报。",
                    }, ensure_ascii=False)},
                }],
            }
        return {
            "role": "assistant",
            "content": 'DIRECT_DECISION: {"replied": true, "reason": "日程已创建并回复"}',
        }


class ChainClient:
    def __init__(self):
        self.calls = 0
        self.tool_names = []

    def complete(self, messages, tools):
        self.calls += 1
        self.tool_names = [tool["function"]["name"] for tool in tools]
        if self.calls == 1:
            return {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "join-chain", "type": "function",
                    "function": {"name": "continue_group_chain", "arguments": json.dumps({
                        "source_message_id": 2,
                    }, ensure_ascii=False)},
                }],
            }
        return {
            "role": "assistant",
            "content": 'CHAIN_DECISION: {"joined": true, "awaiting_clarification": false, "reason": "已追加"}',
        }


class ChainClarificationClient:
    def __init__(self):
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "ask-chain-entry", "type": "function",
                    "function": {"name": "ask_forward_target", "arguments": json.dumps({
                        "question": "这次宿舍晚点名接龙要追加什么内容？请只回复不含序号的一行。",
                        "memory_key": "solitaire.dormitory_checkin_entry",
                    }, ensure_ascii=False)},
                }],
            }
        return {
            "role": "assistant",
            "content": 'CHAIN_DECISION: {"joined": false, "awaiting_clarification": true, "reason": "缺少本人接龙内容"}',
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

    @staticmethod
    def chain_group(template="505 3/4 张三居家", identifiers=("张三",), keywords=("晚点名",)):
        return GroupConfig(
            id="dorm@chatroom",
            name="宿舍通知群",
            forward_to=("Alice",),
            chain_enabled=True,
            chain_rules=(ChainRuleConfig(
                name="宿舍晚点名",
                match_keywords=keywords,
                entry_template=template,
                self_identifiers=identifiers,
            ),),
        )

    def test_forward_is_not_terminal_and_final_decision_ends_loop(self):
        client = FakeClient()
        runtime = ToolRuntime(self.store, self.gateway, self.outbound.append, "Asia/Shanghai", 24)
        agent = AgentRunner(client, runtime, self.gateway, self.store, "fake", 10, "Asia/Shanghai", 24)

        outcome = agent.run(MessageBatch(self.group, [{"content": "会议时间变了"}]))

        self.assertEqual(outcome, "important")
        self.assertEqual(client.calls, 4)
        self.assertEqual(len(self.outbound), 1)
        self.assertIn("会议改期", self.outbound[0].text)
        self.assertIn(("group-id", 80, 0), self.gateway.history_calls)
        self.assertFalse(client.initial_payload["recent_history"][0]["is_read"])
        self.assertTrue(self.store.annotate_history("group-id", self.gateway.get_history("group-id", 1))[0]["is_read"])
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

    def test_manual_history_review_is_a_trusted_agent_task(self):
        client = CapturingIgnoreClient()
        runtime = ToolRuntime(self.store, self.gateway, self.outbound.append, "Asia/Shanghai", 24)
        agent = AgentRunner(client, runtime, self.gateway, self.store, "fake", 3, "Asia/Shanghai", 24)
        message = {
            "type": "manual_history_review",
            "content": "管理员触发",
            "_internal_trigger": "history_review",
        }

        outcome = agent.run(MessageBatch(self.group, [message]))

        self.assertEqual(outcome, "ignored")
        self.assertIn("可信人工历史回顾", client.messages[0]["content"])
        self.assertIn("人工回顾 recent_history", client.messages[1]["content"])

    def test_history_cannot_be_marked_read_before_forwarding(self):
        client = PrematureMarkClient()
        runtime = ToolRuntime(self.store, self.gateway, self.outbound.append, "Asia/Shanghai", 24)
        agent = AgentRunner(client, runtime, self.gateway, self.store, "fake", 3, "Asia/Shanghai", 24)

        outcome = agent.run(MessageBatch(self.group, [{"id": 1, "content": "ordinary"}]))

        self.assertEqual(outcome, "ignored")
        self.assertFalse(self.store.annotate_history("group-id", [{"id": 1}])[0]["is_read"])
        tool_message = next(item for item in client.messages if item.get("role") == "tool")
        self.assertIn("requires a successful forward_important", tool_message["content"])

    def test_forward_target_direct_chat_can_schedule_and_reply(self):
        client = DirectScheduleClient()
        runtime = ToolRuntime(self.store, self.gateway, self.outbound.append, "Asia/Shanghai", 24)
        agent = AgentRunner(client, runtime, self.gateway, self.store, "fake", 6, "Asia/Shanghai", 24)
        direct_group = GroupConfig(id="Alice", name="私聊 · Alice", forward_to=("Alice",), aggregation_seconds=1)
        message = {
            "id": 22, "content": "2099年9月1日上午9点提醒我交月报",
            "_direct_chat": True, "_direct_target": "Alice",
        }

        outcome = agent.run(MessageBatch(direct_group, [message]))

        self.assertEqual(outcome, "direct_chat")
        self.assertEqual(client.calls, 3)
        self.assertIn("reply_to_sender", client.tool_names)
        self.assertNotIn("forward_important", client.tool_names)
        schedules = self.store.list_schedules("Alice")
        self.assertEqual(schedules[0]["title"], "Submit report")
        delivery = self.store.claim_delivery()
        self.assertEqual(delivery["target"], "Alice")
        self.assertIn("已创建提醒", delivery["text"])

    def test_native_chain_text_is_parsed_and_automatically_continued(self):
        parsed = parse_chain_message("[文件/链接/卡片] #接龙\n今日晚点名\n\n1. 502 2/4\n2. 401 4/4")
        self.assertEqual(parsed["title"], "今日晚点名")
        self.assertEqual(parsed["entries"][-1], {"number": 2, "content": "401 4/4"})

        gateway = ChainGateway()
        client = ChainClient()
        runtime = ToolRuntime(self.store, gateway, self.outbound.append, "Asia/Shanghai", 24)
        agent = AgentRunner(client, runtime, gateway, self.store, "fake", 5, "Asia/Shanghai", 24)
        self.store.remember("ignored", "profile.dormitory", "505 3/4")
        self.store.remember("ignored", "profile.name", "张三")
        group = self.chain_group(
            template="{{profile.dormitory}} {{profile.name}}居家",
            identifiers=("{{profile.name}}",),
        )
        message = {"id": 2, "content": "[文件/链接/卡片] #接龙\n今日晚点名\n\n1. 502 2/4\n2. 401 4/4"}

        outcome = agent.run(MessageBatch(group, [message]))

        self.assertEqual(outcome, "chain_joined")
        self.assertIn("continue_group_chain", client.tool_names)
        self.assertNotIn("forward_important", client.tool_names)
        delivery = self.store.claim_delivery()
        self.assertEqual(delivery["target"], "dorm@chatroom")
        self.assertTrue(delivery["text"].endswith("3. 505 3/4 张三居家"))
        self.assertTrue(self.store.annotate_history("dorm@chatroom", [{"id": 2}])[0]["is_read"])

    def test_chain_with_missing_identity_asks_forward_target_without_joining(self):
        gateway = ChainGateway()
        client = ChainClarificationClient()
        runtime = ToolRuntime(self.store, gateway, self.outbound.append, "Asia/Shanghai", 24)
        agent = AgentRunner(client, runtime, gateway, self.store, "fake", 5, "Asia/Shanghai", 24)
        group = self.chain_group(
            template="{{solitaire.dormitory_checkin_entry}}",
            identifiers=(),
        )
        message = {"id": 2, "content": "[文件/链接/卡片] #接龙\n今日晚点名\n\n1. 502 2/4"}

        outcome = agent.run(MessageBatch(group, [message]))

        self.assertEqual(outcome, "awaiting_clarification")
        self.assertEqual(client.calls, 2)
        pending = self.store.list_clarifications("pending")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["memory_key"], "solitaire.dormitory_checkin_entry")
        delivery = self.store.claim_delivery()
        self.assertEqual(delivery["target"], "Alice")
        self.assertIn("需要确认", delivery["text"])
        self.assertNotIn("dorm@chatroom", [item.targets[0] for item in self.outbound])

    def test_chain_is_skipped_when_self_identifier_is_already_present(self):
        gateway = ChainGateway()
        gateway.get_history = lambda group_id, limit, offset=0: [{
            "id": 2,
            "content": "#接龙\n今日晚点名\n\n1. 502 2/4\n2. 505 3/4 张三已返校",
            "direction": "incoming",
        }]
        client = ChainClient()
        runtime = ToolRuntime(self.store, gateway, self.outbound.append, "Asia/Shanghai", 24)
        agent = AgentRunner(client, runtime, gateway, self.store, "fake", 5, "Asia/Shanghai", 24)

        outcome = agent.run(MessageBatch(self.chain_group(), [{
            "id": 2, "content": "#接龙\n今日晚点名\n\n1. 502 2/4\n2. 505 3/4 张三已返校",
        }]))

        self.assertEqual(outcome, "chain_already_joined")
        self.assertIsNone(self.store.claim_delivery())
        self.assertEqual(self.outbound, [])

    def test_unconfigured_or_unmatched_chain_cannot_use_chain_tool(self):
        client = CapturingIgnoreClient()
        runtime = ToolRuntime(self.store, self.gateway, self.outbound.append, "Asia/Shanghai", 24)
        agent = AgentRunner(client, runtime, self.gateway, self.store, "fake", 3, "Asia/Shanghai", 24)
        message = {"id": 2, "content": "#接龙\n周末聚餐报名\n\n1. 李四"}

        outcome = agent.run(MessageBatch(self.chain_group(keywords=("晚点名",)), [message]))

        self.assertEqual(outcome, "ignored")
        self.assertNotIn("continue_group_chain", client.tool_names)


if __name__ == "__main__":
    unittest.main()
