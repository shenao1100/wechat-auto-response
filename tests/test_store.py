from __future__ import annotations

import tempfile
import unittest
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from wechat_agent.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.tempdir.name) / "app.db"))

    def tearDown(self):
        self.store.close()
        self.tempdir.cleanup()

    def test_memory_and_schedule_lifecycle(self):
        self.store.remember("g1", "deadline", "Friday")
        self.assertEqual(self.store.get_memories("another-group")[0]["value"], "Friday")
        self.store.remember("g2", "deadline", "Saturday")
        self.assertEqual(self.store.get_memories("g1")[0]["value"], "Saturday")

        run_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        schedule_id = self.store.add_schedule("g1", "Test", "Content", run_at, ["filehelper"])
        self.assertEqual(len(self.store.queue_schedule_outbox()), 1)
        delivery = self.store.claim_delivery()
        self.assertEqual(delivery["target"], "filehelper")
        self.assertEqual(self.store.finish_delivery(delivery["id"], True), "sent")
        schedules = self.store.list_schedules("g1", include_done=True)
        self.assertEqual(schedules[0]["id"], schedule_id)
        self.assertEqual(schedules[0]["status"], "sent")

    def test_inbox_survives_and_can_be_retried(self):
        inbox_id, inserted = self.store.save_incoming("g1", "m1", {"content": "hello"})
        self.assertTrue(inserted)
        same_id, inserted_again = self.store.save_incoming("g1", "m1", {"content": "hello"})
        self.assertEqual(same_id, inbox_id)
        self.assertFalse(inserted_again)
        self.assertEqual(self.store.pending_incoming()[0]["message"]["content"], "hello")

        self.store.finish_incoming([inbox_id], False, "AI unavailable")
        self.assertEqual(len(self.store.failed_items()["inbox"]), 1)
        self.assertEqual(self.store.retry_failed_incoming(), 1)
        self.assertEqual(len(self.store.pending_incoming()), 1)

    def test_each_outbox_target_has_independent_delivery_state(self):
        inserted, outbox_id = self.store.record_event_and_enqueue(
            "g1", "fingerprint", "Title", "Summary", "notice", 90, [], ["Alice", "Bob"], "Message"
        )
        self.assertTrue(inserted)
        first = self.store.claim_delivery()
        self.assertEqual(first["target"], "Alice")
        self.assertEqual(self.store.finish_delivery(first["id"], True), "pending")

        second = self.store.claim_delivery()
        self.assertEqual(second["target"], "Bob")
        self.assertEqual(self.store.finish_delivery(second["id"], False, "offline", max_attempts=1), "failed")
        failed = self.store.failed_items()["deliveries"]
        self.assertEqual([(item["outbox_id"], item["target"]) for item in failed], [(outbox_id, "Bob")])
        self.assertEqual(self.store.retry_failed_deliveries(), 1)
        retried = self.store.claim_delivery()
        self.assertEqual(retried["target"], "Bob")

    def test_clarification_is_idempotent_and_answer_becomes_memory_and_inbox(self):
        context = [{"content": "是否参加可选活动？"}]
        clarification_id = self.store.create_clarification(
            "g1", "Class", "这类自愿活动需要提醒你吗？", "preference.optional_events", context, "Alice"
        )
        duplicate_id = self.store.create_clarification(
            "g1", "Class", "这类自愿活动需要提醒你吗？", "preference.optional_events", context, "Alice"
        )
        self.assertEqual(clarification_id, duplicate_id)
        self.assertEqual(len(self.store.list_clarifications("pending")), 1)

        matched = self.store.find_clarification_for_reply("Alice", f"#{clarification_id} 需要提醒")
        self.assertEqual(matched["id"], clarification_id)
        result = self.store.answer_clarification(clarification_id, f"#{clarification_id} 需要提醒", "Alice")
        self.assertTrue(result["inserted"])
        self.assertEqual(result["message"]["content"], "需要提醒")
        memory = self.store.get_memories("another-group")[0]
        self.assertEqual(memory["key"], "preference.optional_events")
        self.assertIn("需要提醒", memory["value"])
        self.assertEqual(len(self.store.pending_incoming()), 1)

    def test_legacy_group_memories_are_merged_and_backed_up(self):
        path = Path(self.tempdir.name) / "legacy.db"
        connection = sqlite3.connect(path)
        connection.execute(
            """CREATE TABLE memories (
               group_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
               updated_at TEXT NOT NULL, expires_at TEXT, PRIMARY KEY(group_id, key))"""
        )
        connection.execute(
            "INSERT INTO memories VALUES ('g1', 'preference', 'old', '2026-01-01T00:00:00+00:00', NULL)"
        )
        connection.execute(
            "INSERT INTO memories VALUES ('g2', 'preference', 'new', '2026-02-01T00:00:00+00:00', NULL)"
        )
        connection.commit()
        connection.close()

        migrated = Store(str(path))
        try:
            self.assertEqual(migrated.get_memories("any-group")[0]["value"], "new")
            backup_count = migrated._connection().execute("SELECT COUNT(*) FROM memories_group_backup").fetchone()[0]
            self.assertEqual(backup_count, 2)
        finally:
            migrated.close()


if __name__ == "__main__":
    unittest.main()
