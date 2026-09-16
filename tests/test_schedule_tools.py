from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wechat_agent.agent_tools import ToolRuntime, tool_definitions
from wechat_agent.models import GroupConfig, MessageBatch
from wechat_agent.store import Store


class ScheduleToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.tempdir.name) / "app.db"))
        self.runtime = ToolRuntime(self.store, None, lambda _message: None, "Asia/Shanghai", 24)
        self.batch = MessageBatch(
            GroupConfig(id="direct:owner", name="Private", forward_to=("Alice",)),
            [{"content": "remind me"}],
        )

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def test_create_list_update_and_cancel_schedule(self) -> None:
        created = self.runtime.execute(
            "schedule_reminder",
            {
                "title": "Submit report",
                "content": "Submit the monthly report",
                "run_at": "2099-09-01T09:00:00+08:00",
            },
            self.batch,
        ).content
        schedule_id = created["schedule_id"]
        self.assertTrue(created["ok"])
        self.assertEqual(created["targets"], ["Alice"])
        self.assertEqual(created["run_at_local"], "2099-09-01T09:00+08:00")
        self.assertTrue(created["can_update"])

        updated = self.runtime.execute(
            "update_schedule",
            {
                "schedule_id": schedule_id,
                "title": "Submit revised report",
                "run_at": "2099-09-02T10:30:00+08:00",
            },
            self.batch,
        ).content
        self.assertTrue(updated["ok"])
        self.assertTrue(updated["updated"])
        self.assertEqual(updated["title"], "Submit revised report")
        self.assertEqual(updated["content"], "Submit the monthly report")
        self.assertEqual(updated["run_at_local"], "2099-09-02T10:30+08:00")

        listed = self.runtime.execute("list_schedules", {"status": "pending"}, self.batch).content
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["schedules"][0]["schedule_id"], schedule_id)

        cancelled = self.runtime.execute(
            "cancel_schedule", {"schedule_id": schedule_id}, self.batch
        ).content
        self.assertTrue(cancelled["ok"])
        self.assertTrue(cancelled["cancelled"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertFalse(cancelled["can_cancel"])
        self.assertEqual(
            self.runtime.execute("list_schedules", {}, self.batch).content["count"],
            0,
        )

    def test_duplicate_and_invalid_schedules_are_rejected(self) -> None:
        arguments = {
            "title": "Meeting",
            "content": "Join meeting",
            "run_at": "2099-10-01T12:00:00+08:00",
        }
        first = self.runtime.execute("schedule_reminder", arguments, self.batch).content
        duplicate = self.runtime.execute("schedule_reminder", arguments, self.batch).content
        self.assertTrue(duplicate["ok"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["schedule_id"], first["schedule_id"])

        empty = self.runtime.execute(
            "schedule_reminder",
            {"title": " ", "content": "x", "run_at": "2099-10-01T12:00:00+08:00"},
            self.batch,
        ).content
        self.assertFalse(empty["ok"])

        no_target_batch = MessageBatch(GroupConfig(id="g", name="No target", forward_to=()), [])
        no_target = self.runtime.execute("schedule_reminder", arguments, no_target_batch).content
        self.assertFalse(no_target["ok"])

    def test_update_schedule_is_exposed_to_direct_chat(self) -> None:
        names = {item["function"]["name"] for item in tool_definitions()}
        self.assertIn("update_schedule", names)


if __name__ == "__main__":
    unittest.main()
