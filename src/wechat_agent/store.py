from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

SHARED_MEMORY_SCOPE = "__shared__"
SHARED_MEMORY_MIGRATION = "shared_memories_v1"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime | None = None) -> str:
    value = value or utc_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connection(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            self._local.connection = connection
        return connection

    def close(self) -> None:
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            connection.close()
            self._local.connection = None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()

    def _init_schema(self) -> None:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS memories (
                    group_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT,
                    PRIMARY KEY (group_id, key)
                );
                CREATE TABLE IF NOT EXISTS schema_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memories_group_backup (
                    group_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT,
                    PRIMARY KEY (group_id, key)
                );
                CREATE TABLE IF NOT EXISTS forwarded_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    category TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    source_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(group_id, fingerprint)
                );
                CREATE TABLE IF NOT EXISTS history_read_marks (
                    group_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    marked_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, message_id)
                );
                CREATE TABLE IF NOT EXISTS chain_participations (
                    group_id TEXT NOT NULL,
                    source_message_id TEXT NOT NULL,
                    entry TEXT NOT NULL,
                    outbox_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, source_message_id)
                );
                CREATE TABLE IF NOT EXISTS schedules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    run_at TEXT NOT NULL,
                    targets_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    sent_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_schedules_due
                    ON schedules(status, run_at);
                CREATE TABLE IF NOT EXISTS agent_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    message_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    received_at TEXT NOT NULL,
                    finished_at TEXT,
                    last_error TEXT,
                    UNIQUE(group_id, message_key)
                );
                CREATE INDEX IF NOT EXISTS idx_inbox_status
                    ON inbox(status, group_id, id);
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    targets_json TEXT NOT NULL,
                    text TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    schedule_id INTEGER,
                    created_at TEXT NOT NULL,
                    sent_at TEXT,
                    last_error TEXT,
                    FOREIGN KEY(schedule_id) REFERENCES schedules(id)
                );
                CREATE INDEX IF NOT EXISTS idx_outbox_status
                    ON outbox(status, id);
                CREATE TABLE IF NOT EXISTS outbox_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    outbox_id INTEGER NOT NULL,
                    target TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    sent_at TEXT,
                    last_error TEXT,
                    UNIQUE(outbox_id, target),
                    FOREIGN KEY(outbox_id) REFERENCES outbox(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_outbox_deliveries_status
                    ON outbox_deliveries(status, id);
                CREATE TABLE IF NOT EXISTS clarifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    question TEXT NOT NULL,
                    memory_key TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    target TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    answered_at TEXT,
                    answered_by TEXT,
                    answer TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_clarifications_status
                    ON clarifications(status, target, id);
                """
            )
            orphaned = connection.execute(
                """SELECT o.id, o.targets_json, o.status, o.sent_at, o.last_error FROM outbox o
                   WHERE NOT EXISTS (
                     SELECT 1 FROM outbox_deliveries d WHERE d.outbox_id=o.id
                   )"""
            ).fetchall()
            for row in orphaned:
                targets = json.loads(row["targets_json"])
                status = row["status"] if row["status"] in {"sent", "failed"} else "pending"
                connection.executemany(
                    """INSERT OR IGNORE INTO outbox_deliveries
                       (outbox_id, target, status, sent_at, last_error) VALUES (?, ?, ?, ?, ?)""",
                    [
                        (
                            int(row["id"]),
                            target,
                            status,
                            row["sent_at"] if status == "sent" else None,
                            row["last_error"] if status == "failed" else None,
                        )
                        for target in targets
                    ],
                )
            self._migrate_shared_memories(connection)
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _migrate_shared_memories(connection: sqlite3.Connection) -> None:
        migrated = connection.execute(
            "SELECT 1 FROM schema_metadata WHERE key=?",
            (SHARED_MEMORY_MIGRATION,),
        ).fetchone()
        if migrated is not None:
            return
        rows = connection.execute(
            """SELECT group_id, key, value, updated_at, expires_at FROM memories
               ORDER BY updated_at DESC, CASE WHEN group_id=? THEN 0 ELSE 1 END""",
            (SHARED_MEMORY_SCOPE,),
        ).fetchall()
        connection.executemany(
            """INSERT OR IGNORE INTO memories_group_backup
               (group_id, key, value, updated_at, expires_at) VALUES (?, ?, ?, ?, ?)""",
            [(row["group_id"], row["key"], row["value"], row["updated_at"], row["expires_at"]) for row in rows],
        )
        latest_by_key: dict[str, sqlite3.Row] = {}
        for row in rows:
            latest_by_key.setdefault(str(row["key"]), row)
        connection.execute("DELETE FROM memories")
        connection.executemany(
            """INSERT INTO memories(group_id, key, value, updated_at, expires_at)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (SHARED_MEMORY_SCOPE, row["key"], row["value"], row["updated_at"], row["expires_at"])
                for row in latest_by_key.values()
            ],
        )
        connection.execute(
            "INSERT INTO schema_metadata(key, value, updated_at) VALUES (?, ?, ?)",
            (SHARED_MEMORY_MIGRATION, "completed", iso_utc()),
        )

    def get_memories(self, _group_id: str = SHARED_MEMORY_SCOPE) -> list[dict[str, Any]]:
        now = iso_utc()
        rows = self._connection().execute(
            """SELECT key, value, updated_at, expires_at FROM memories
               WHERE group_id=? AND (expires_at IS NULL OR expires_at > ?)
               ORDER BY updated_at DESC""",
            (SHARED_MEMORY_SCOPE, now),
        ).fetchall()
        return [dict(row) for row in rows]

    def remember(self, _group_id: str, key: str, value: str, expires_at: str | None = None) -> None:
        self._connection().execute(
            """INSERT INTO memories(group_id, key, value, updated_at, expires_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(group_id, key) DO UPDATE SET
                 value=excluded.value, updated_at=excluded.updated_at, expires_at=excluded.expires_at""",
            (SHARED_MEMORY_SCOPE, key, value, iso_utc(), expires_at),
        )

    def recent_events(self, group_id: str, hours: int, limit: int = 20) -> list[dict[str, Any]]:
        since = iso_utc(utc_now() - timedelta(hours=hours))
        rows = self._connection().execute(
            """SELECT title, summary, category, score, created_at FROM forwarded_events
               WHERE group_id=? AND created_at>=? ORDER BY id DESC LIMIT ?""",
            (group_id, since, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_history_read(self, group_id: str, message_ids: list[Any]) -> tuple[int, int]:
        normalized = list(dict.fromkeys(str(value).strip() for value in message_ids if str(value).strip()))
        if not normalized:
            raise ValueError("message_ids must contain at least one history message id")
        if len(normalized) > 200:
            raise ValueError("message_ids cannot contain more than 200 ids")
        with self.transaction() as connection:
            before = connection.total_changes
            connection.executemany(
                "INSERT OR IGNORE INTO history_read_marks(group_id, message_id, marked_at) VALUES (?, ?, ?)",
                [(group_id, message_id, iso_utc()) for message_id in normalized],
            )
            inserted = connection.total_changes - before
        return len(normalized), inserted

    def annotate_history(self, group_id: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ids = list(dict.fromkeys(str(item["id"]) for item in messages if item.get("id") is not None))
        marked: set[str] = set()
        if ids:
            placeholders = ",".join("?" for _ in ids)
            rows = self._connection().execute(
                f"SELECT message_id FROM history_read_marks WHERE group_id=? AND message_id IN ({placeholders})",
                [group_id, *ids],
            ).fetchall()
            marked = {str(row["message_id"]) for row in rows}
        return [
            {**item, "is_read": item.get("id") is not None and str(item["id"]) in marked}
            for item in messages
        ]

    def has_recent_event(self, group_id: str, semantic_fingerprint: str, hours: int) -> bool:
        since = iso_utc(utc_now() - timedelta(hours=hours))
        row = self._connection().execute(
            """SELECT 1 FROM forwarded_events
               WHERE group_id=? AND fingerprint LIKE ? AND created_at>=? LIMIT 1""",
            (group_id, f"{semantic_fingerprint}:%", since),
        ).fetchone()
        return row is not None

    def record_event(
        self,
        group_id: str,
        fingerprint: str,
        title: str,
        summary: str,
        category: str,
        score: int,
        source: list[dict[str, Any]],
    ) -> bool:
        try:
            self._connection().execute(
                """INSERT INTO forwarded_events
                   (group_id, fingerprint, title, summary, category, score, source_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (group_id, fingerprint, title, summary, category, score, json.dumps(source, ensure_ascii=False), iso_utc()),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def record_event_and_enqueue(
        self,
        group_id: str,
        fingerprint: str,
        title: str,
        summary: str,
        category: str,
        score: int,
        source: list[dict[str, Any]],
        targets: list[str],
        text: str,
    ) -> tuple[bool, int | None]:
        try:
            with self.transaction() as connection:
                connection.execute(
                    """INSERT INTO forwarded_events
                       (group_id, fingerprint, title, summary, category, score, source_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        group_id,
                        fingerprint,
                        title,
                        summary,
                        category,
                        score,
                        json.dumps(source, ensure_ascii=False),
                        iso_utc(),
                    ),
                )
                cursor = connection.execute(
                    """INSERT INTO outbox(targets_json, text, created_at)
                       VALUES (?, ?, ?)""",
                    (json.dumps(targets, ensure_ascii=False), text, iso_utc()),
                )
                outbox_id = int(cursor.lastrowid)
                connection.executemany(
                    "INSERT INTO outbox_deliveries(outbox_id, target) VALUES (?, ?)",
                    [(outbox_id, target) for target in targets],
                )
            return True, outbox_id
        except sqlite3.IntegrityError:
            return False, None

    def enqueue_message(self, targets: list[str], text: str) -> int:
        clean_targets = list(dict.fromkeys(str(target).strip() for target in targets if str(target).strip()))
        clean_text = str(text).strip()
        if not clean_targets:
            raise ValueError("targets must not be empty")
        if not clean_text:
            raise ValueError("text must not be empty")
        with self.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO outbox(targets_json, text, created_at) VALUES (?, ?, ?)",
                (json.dumps(clean_targets, ensure_ascii=False), clean_text, iso_utc()),
            )
            outbox_id = int(cursor.lastrowid)
            connection.executemany(
                "INSERT INTO outbox_deliveries(outbox_id, target) VALUES (?, ?)",
                [(outbox_id, target) for target in clean_targets],
            )
        return outbox_id

    def enqueue_chain_participation(
        self,
        group_id: str,
        source_message_id: Any,
        entry: str,
        text: str,
    ) -> tuple[bool, int]:
        source_key = str(source_message_id).strip()
        if not source_key:
            raise ValueError("source_message_id must not be empty")
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT outbox_id FROM chain_participations WHERE group_id=? AND source_message_id=?",
                (group_id, source_key),
            ).fetchone()
            if existing is not None:
                return False, int(existing["outbox_id"])
            cursor = connection.execute(
                "INSERT INTO outbox(targets_json, text, created_at) VALUES (?, ?, ?)",
                (json.dumps([group_id], ensure_ascii=False), text, iso_utc()),
            )
            outbox_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO outbox_deliveries(outbox_id, target) VALUES (?, ?)",
                (outbox_id, group_id),
            )
            connection.execute(
                """INSERT INTO chain_participations
                   (group_id, source_message_id, entry, outbox_id, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (group_id, source_key, entry, outbox_id, iso_utc()),
            )
        return True, outbox_id

    def has_chain_participation(self, group_id: str, source_message_id: Any) -> bool:
        source_key = str(source_message_id or "").strip()
        if not source_key:
            return False
        row = self._connection().execute(
            "SELECT 1 FROM chain_participations WHERE group_id=? AND source_message_id=? LIMIT 1",
            (group_id, source_key),
        ).fetchone()
        return row is not None

    def save_incoming(self, group_id: str, message_key: str, payload: dict[str, Any]) -> tuple[int, bool]:
        cursor = self._connection().execute(
            """INSERT OR IGNORE INTO inbox(group_id, message_key, payload_json, received_at)
               VALUES (?, ?, ?, ?)""",
            (group_id, message_key, json.dumps(payload, ensure_ascii=False), iso_utc()),
        )
        inserted = cursor.rowcount == 1
        row = self._connection().execute(
            "SELECT id FROM inbox WHERE group_id=? AND message_key=?",
            (group_id, message_key),
        ).fetchone()
        return int(row["id"]), inserted

    def pending_incoming(self, limit: int = 1000) -> list[dict[str, Any]]:
        rows = self._connection().execute(
            """SELECT id, group_id, payload_json, received_at FROM inbox
               WHERE status='pending' ORDER BY id ASC LIMIT ?""",
            (limit,),
        ).fetchall()
        result = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            payload["_inbox_id"] = int(row["id"])
            result.append({"group_id": row["group_id"], "message": payload, "received_at": row["received_at"]})
        return result

    def finish_incoming(self, inbox_ids: list[int], success: bool, error: str | None = None) -> None:
        if not inbox_ids:
            return
        placeholders = ",".join("?" for _ in inbox_ids)
        status = "done" if success else "failed"
        self._connection().execute(
            f"UPDATE inbox SET status=?, finished_at=?, last_error=? WHERE id IN ({placeholders})",
            [status, iso_utc(), error] + inbox_ids,
        )

    def retry_failed_incoming(self) -> int:
        cursor = self._connection().execute(
            "UPDATE inbox SET status='pending', finished_at=NULL, last_error=NULL WHERE status='failed'"
        )
        return cursor.rowcount

    def retry_failed_deliveries(self) -> int:
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT DISTINCT outbox_id FROM outbox_deliveries WHERE status='failed'"
            ).fetchall()
            outbox_ids = [int(row["outbox_id"]) for row in rows]
            deliveries = connection.execute(
                """UPDATE outbox_deliveries
                   SET status='pending', attempts=0, last_error=NULL
                   WHERE status='failed'"""
            ).rowcount
            if outbox_ids:
                placeholders = ",".join("?" for _ in outbox_ids)
                connection.execute(
                    f"UPDATE outbox SET status='pending', last_error=NULL WHERE id IN ({placeholders})",
                    outbox_ids,
                )
                connection.execute(
                    f"""UPDATE schedules SET status='sending'
                        WHERE id IN (
                          SELECT schedule_id FROM outbox
                          WHERE id IN ({placeholders}) AND schedule_id IS NOT NULL
                        )""",
                    outbox_ids,
                )
        return deliveries

    def create_clarification(
        self,
        group_id: str,
        group_name: str,
        question: str,
        memory_key: str,
        context: list[dict[str, Any]],
        target: str,
    ) -> int:
        with self.transaction() as connection:
            existing = connection.execute(
                """SELECT id FROM clarifications
                   WHERE group_id=? AND question=? AND memory_key=? AND target=? AND status='pending'
                   ORDER BY id DESC LIMIT 1""",
                (group_id, question, memory_key, target),
            ).fetchone()
            if existing is not None:
                return int(existing["id"])
            cursor = connection.execute(
                """INSERT INTO clarifications
                   (group_id, question, memory_key, context_json, target, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    group_id,
                    question,
                    memory_key,
                    json.dumps(context, ensure_ascii=False),
                    target,
                    iso_utc(),
                ),
            )
            clarification_id = int(cursor.lastrowid)
            text = (
                f"【需要确认 #{clarification_id}】\n"
                f"来源群：{group_name}\n\n"
                f"{question}\n\n"
                f"请回复：#{clarification_id} 你的答案"
            )
            outbox_cursor = connection.execute(
                """INSERT INTO outbox(targets_json, text, created_at)
                   VALUES (?, ?, ?)""",
                (json.dumps([target], ensure_ascii=False), text, iso_utc()),
            )
            outbox_id = int(outbox_cursor.lastrowid)
            connection.execute(
                "INSERT INTO outbox_deliveries(outbox_id, target) VALUES (?, ?)",
                (outbox_id, target),
            )
        return clarification_id

    def list_clarifications(
        self,
        status: str | None = None,
        group_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        sql = """SELECT id, group_id, question, memory_key, target, status,
                        created_at, answered_at, answered_by, answer
                 FROM clarifications WHERE 1=1"""
        params: list[Any] = []
        if status:
            sql += " AND status=?"
            params.append(status)
        if group_id:
            sql += " AND group_id=?"
            params.append(group_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(1000, limit)))
        return [dict(row) for row in self._connection().execute(sql, params).fetchall()]

    def find_clarification_for_reply(self, target: str, text: str) -> dict[str, Any] | None:
        import re

        match = re.search(r"#\s*(\d+)", text)
        connection = self._connection()
        if match:
            row = connection.execute(
                """SELECT * FROM clarifications
                   WHERE id=? AND target=? AND status='pending'""",
                (int(match.group(1)), target),
            ).fetchone()
            return dict(row) if row else None
        rows = connection.execute(
            """SELECT * FROM clarifications
               WHERE target=? AND status='pending' ORDER BY id DESC LIMIT 2""",
            (target,),
        ).fetchall()
        return dict(rows[0]) if len(rows) == 1 else None

    def answer_clarification(
        self,
        clarification_id: int,
        answer: str,
        answered_by: str,
    ) -> dict[str, Any]:
        import re

        cleaned = re.sub(r"^\s*#\s*\d+\s*[:：-]?\s*", "", answer).strip()
        if not cleaned:
            raise ValueError("Clarification answer must not be empty")
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM clarifications WHERE id=? AND status='pending'",
                (clarification_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Clarification is missing or already answered")
            now = iso_utc()
            connection.execute(
                """UPDATE clarifications SET status='answered', answered_at=?,
                   answered_by=?, answer=? WHERE id=?""",
                (now, answered_by, cleaned, clarification_id),
            )
            memory_value = f"问题：{row['question']}\n答案：{cleaned}"
            connection.execute(
                """INSERT INTO memories(group_id, key, value, updated_at, expires_at)
                   VALUES (?, ?, ?, ?, NULL)
                   ON CONFLICT(group_id, key) DO UPDATE SET
                     value=excluded.value, updated_at=excluded.updated_at, expires_at=NULL""",
                (SHARED_MEMORY_SCOPE, row["memory_key"], memory_value, now),
            )
            payload = {
                "id": f"clarification-{clarification_id}",
                "time": now,
                "sender": answered_by,
                "sender_name": answered_by,
                "type": "clarification_answer",
                "content": cleaned,
                "question": row["question"],
                "original_messages": json.loads(row["context_json"]),
                "clarification_id": clarification_id,
            }
            message_key = f"clarification:{clarification_id}"
            cursor = connection.execute(
                """INSERT OR IGNORE INTO inbox(group_id, message_key, payload_json, received_at)
                   VALUES (?, ?, ?, ?)""",
                (row["group_id"], message_key, json.dumps(payload, ensure_ascii=False), now),
            )
            inbox_row = connection.execute(
                "SELECT id FROM inbox WHERE group_id=? AND message_key=?",
                (row["group_id"], message_key),
            ).fetchone()
        payload["_inbox_id"] = int(inbox_row["id"])
        return {"group_id": row["group_id"], "message": payload, "inserted": cursor.rowcount == 1}

    def delete_memory(self, _group_id: str, key: str) -> bool:
        cursor = self._connection().execute(
            "DELETE FROM memories WHERE group_id=? AND key=?",
            (SHARED_MEMORY_SCOPE, key),
        )
        return cursor.rowcount == 1

    def recover_outbox(self) -> int:
        with self.transaction() as connection:
            deliveries = connection.execute(
                "UPDATE outbox_deliveries SET status='pending' WHERE status='sending'"
            ).rowcount
            outboxes = connection.execute(
                "UPDATE outbox SET status='pending' WHERE status='sending'"
            ).rowcount
        return deliveries + outboxes

    def claim_delivery(self) -> dict[str, Any] | None:
        with self.transaction() as connection:
            row = connection.execute(
                """SELECT d.id, d.outbox_id, d.target, d.attempts, o.text
                   FROM outbox_deliveries d JOIN outbox o ON o.id=d.outbox_id
                   WHERE d.status='pending' AND o.status IN ('pending', 'sending')
                   ORDER BY d.id ASC LIMIT 1"""
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE outbox_deliveries SET status='sending', attempts=attempts+1 WHERE id=?",
                (int(row["id"]),),
            )
            connection.execute(
                "UPDATE outbox SET status='sending', attempts=attempts+1 WHERE id=?",
                (int(row["outbox_id"]),),
            )
        return dict(row)

    def finish_delivery(
        self,
        delivery_id: int,
        success: bool,
        error: str | None = None,
        max_attempts: int = 5,
    ) -> str:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT outbox_id, attempts FROM outbox_deliveries WHERE id=?",
                (delivery_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Outbox delivery not found: {delivery_id}")
            delivery_status = "sent" if success else ("failed" if int(row["attempts"]) >= max_attempts else "pending")
            connection.execute(
                """UPDATE outbox_deliveries
                   SET status=?, sent_at=?, last_error=? WHERE id=?""",
                (
                    delivery_status,
                    iso_utc() if success else None,
                    None if success else (error or "send failed")[:2000],
                    delivery_id,
                ),
            )
            outbox_id = int(row["outbox_id"])
            states = {
                item["status"]: int(item["count"])
                for item in connection.execute(
                    """SELECT status, COUNT(*) AS count FROM outbox_deliveries
                       WHERE outbox_id=? GROUP BY status""",
                    (outbox_id,),
                ).fetchall()
            }
            if states.get("pending") or states.get("sending"):
                outbox_status = "pending"
            elif states.get("failed"):
                outbox_status = "failed"
            else:
                outbox_status = "sent"
            connection.execute(
                "UPDATE outbox SET status=?, sent_at=?, last_error=? WHERE id=?",
                (
                    outbox_status,
                    iso_utc() if outbox_status == "sent" else None,
                    None if outbox_status == "sent" else (error or "one or more targets failed")[:2000],
                    outbox_id,
                ),
            )
            schedule = connection.execute(
                "SELECT schedule_id FROM outbox WHERE id=?",
                (outbox_id,),
            ).fetchone()
            schedule_id = schedule["schedule_id"] if schedule else None
            if schedule_id is not None and outbox_status in {"sent", "failed"}:
                connection.execute(
                    "UPDATE schedules SET status=?, sent_at=? WHERE id=?",
                    (
                        outbox_status,
                        iso_utc() if outbox_status == "sent" else None,
                        int(schedule_id),
                    ),
                )
        return outbox_status

    def queue_schedule_outbox(self, limit: int = 20) -> list[int]:
        outbox_ids: list[int] = []
        with self.transaction() as connection:
            rows = connection.execute(
                """SELECT * FROM schedules WHERE status='pending' AND run_at<=?
                   ORDER BY run_at ASC LIMIT ?""",
                (iso_utc(), limit),
            ).fetchall()
            for row in rows:
                text = f"【日程提醒】\n{row['title']}\n\n{row['content']}"
                cursor = connection.execute(
                    """INSERT INTO outbox(targets_json, text, schedule_id, created_at)
                       VALUES (?, ?, ?, ?)""",
                    (row["targets_json"], text, int(row["id"]), iso_utc()),
                )
                outbox_id = int(cursor.lastrowid)
                outbox_ids.append(outbox_id)
                targets = json.loads(row["targets_json"])
                connection.executemany(
                    "INSERT INTO outbox_deliveries(outbox_id, target) VALUES (?, ?)",
                    [(outbox_id, target) for target in targets],
                )
                connection.execute("UPDATE schedules SET status='sending' WHERE id=?", (int(row["id"]),))
        return outbox_ids

    def queue_status(self) -> dict[str, Any]:
        connection = self._connection()
        counts: dict[str, Any] = {}
        for table in ("inbox", "outbox", "schedules"):
            rows = connection.execute(
                f"SELECT status, COUNT(*) AS count FROM {table} GROUP BY status"
            ).fetchall()
            counts[table] = {row["status"]: int(row["count"]) for row in rows}
        delivery_rows = connection.execute(
            "SELECT status, COUNT(*) AS count FROM outbox_deliveries GROUP BY status"
        ).fetchall()
        counts["deliveries"] = {row["status"]: int(row["count"]) for row in delivery_rows}
        clarification_rows = connection.execute(
            "SELECT status, COUNT(*) AS count FROM clarifications GROUP BY status"
        ).fetchall()
        counts["clarifications"] = {row["status"]: int(row["count"]) for row in clarification_rows}
        counts["memories"] = int(
            connection.execute(
                """SELECT COUNT(*) FROM memories
                   WHERE group_id=? AND (expires_at IS NULL OR expires_at > ?)""",
                (SHARED_MEMORY_SCOPE, iso_utc()),
            ).fetchone()[0]
        )
        counts["agent_runs"] = int(connection.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0])
        return counts

    def recent_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._connection().execute(
            "SELECT * FROM agent_runs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def failed_items(self, limit: int = 100) -> dict[str, list[dict[str, Any]]]:
        connection = self._connection()
        inbox = connection.execute(
            """SELECT id, group_id, message_key, payload_json, status,
                      received_at, finished_at, last_error FROM inbox
               WHERE status='failed' ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        deliveries = connection.execute(
            """SELECT d.id, d.outbox_id, d.target, d.status, d.attempts,
                      d.sent_at, d.last_error, o.text, o.created_at
               FROM outbox_deliveries d JOIN outbox o ON o.id=d.outbox_id
               WHERE d.status='failed' ORDER BY d.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return {
            "inbox": [dict(row) for row in inbox],
            "deliveries": [dict(row) for row in deliveries],
        }

    def add_schedule(
        self,
        group_id: str,
        title: str,
        content: str,
        run_at: str,
        targets: list[str],
    ) -> int:
        cursor = self._connection().execute(
            """INSERT INTO schedules(group_id, title, content, run_at, targets_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (group_id, title, content, run_at, json.dumps(targets, ensure_ascii=False), iso_utc()),
        )
        return int(cursor.lastrowid)

    def find_schedule(self, _group_id: str, title: str, content: str, run_at: str) -> int | None:
        row = self._connection().execute(
            """SELECT id FROM schedules
               WHERE title=? AND content=? AND run_at=? AND status!='cancelled'
               ORDER BY id DESC LIMIT 1""",
            (title, content, run_at),
        ).fetchone()
        return int(row["id"]) if row else None

    def list_schedules(self, _group_id: str | None = None, include_done: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM schedules WHERE 1=1"
        params: list[Any] = []
        if not include_done:
            sql += " AND status IN ('pending', 'sending')"
        sql += " ORDER BY run_at ASC LIMIT 100"
        rows = self._connection().execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def cancel_schedule(self, _group_id: str | None, schedule_id: int) -> bool:
        cursor = self._connection().execute(
            "UPDATE schedules SET status='cancelled' WHERE id=? AND status='pending'",
            (schedule_id,),
        )
        return cursor.rowcount == 1

    def recover_sending_schedules(self) -> int:
        cursor = self._connection().execute(
            """UPDATE schedules SET status='pending'
               WHERE status='sending' AND NOT EXISTS (
                 SELECT 1 FROM outbox
                 WHERE outbox.schedule_id=schedules.id AND outbox.status IN ('pending', 'sending')
               )"""
        )
        return cursor.rowcount

    def record_run(self, group_id: str, outcome: str, detail: str) -> None:
        self._connection().execute(
            "INSERT INTO agent_runs(group_id, outcome, detail, created_at) VALUES (?, ?, ?, ?)",
            (group_id, outcome, detail, iso_utc()),
        )
