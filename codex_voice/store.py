from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

WAIT_KINDS = {"waiting_on_user", "waiting_on_approval"}


class Store:
    """Own cursors, facts and durable notification delivery in one SQLite DB.

    A playing record is never automatically replayed after process loss: the
    speaker may already have made sound. Prepared/synthesizing records can retry.
    """

    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, timeout=5)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, inode INTEGER, offset INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS threads(id TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(
                id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                kind TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL,
                due REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, speech TEXT, error TEXT,
                played_at REAL, repeat_of TEXT);
            CREATE INDEX IF NOT EXISTS event_queue ON events(status,due);
        """
        )
        self.db.commit()

    def close(self):
        self.db.close()

    def recover(self):
        with self.db:
            self.db.execute(
                "UPDATE events SET status='pending' WHERE status IN ('preparing','synthesizing')"
            )
            self.db.execute(
                "UPDATE events SET status='uncertain',error='Process ended during playback; automatic replay suppressed' WHERE status='playing'"
            )

    def meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        self.db.execute(
            "INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value))
        )

    def thread(self, thread_id):
        row = self.db.execute(
            "SELECT body FROM threads WHERE id=?", (thread_id,)
        ).fetchone()
        return (
            json.loads(row[0])
            if row
            else {"id": thread_id, "pending": {}, "status": "unknown"}
        )

    def save_thread(self, thread):
        self.db.execute(
            "INSERT OR REPLACE INTO threads VALUES (?,?)",
            (thread["id"], json.dumps(thread, ensure_ascii=False)),
        )

    def enqueue(
        self, thread, kind, key, now, delay=0, evidence="", summary="", repeat_of=None
    ):
        turn = thread.get("turn_id") or "unknown"
        event_id = hashlib.sha256(
            f"{thread['id']}:{turn}:{kind}:{key}".encode()
        ).hexdigest()[:32]
        body = {
            "event_id": event_id,
            "event_type": kind,
            "thread_id": thread["id"],
            "turn_id": turn,
            "request_id": key if kind in WAIT_KINDS else None,
            "task_title": thread.get("title") or "Codex 任务",
            "evidence": evidence,
            "latest_summary": summary,
            "source": thread.get("source", "rollout"),
        }
        self.db.execute(
            "INSERT OR IGNORE INTO events(id,thread_id,turn_id,kind,body,created,due,repeat_of) VALUES (?,?,?,?,?,?,?,?)",
            (
                event_id,
                thread["id"],
                turn,
                kind,
                json.dumps(body, ensure_ascii=False),
                now,
                now + delay,
                repeat_of,
            ),
        )
        return event_id

    def valid(self, event):
        body = json.loads(event["body"])
        t = self.thread(event["thread_id"])
        if event["kind"] in WAIT_KINDS:
            if (body.get("request_id") or "").startswith("daemon:") and (
                not t.get("daemon_available")
                or time.time() - t.get("daemon_seen_at", 0) > 30
            ):
                return None  # Unknown while the authoritative runtime is unavailable.
            return body.get("request_id") in t.get("pending", {})
        # A subsequent user turn makes an earlier completion notification stale.
        return t.get("turn_id") == event["turn_id"] and t.get("status") == event["kind"]

    def due(self, now, max_age, limit=5):
        with self.db:
            self.db.execute(
                "UPDATE events SET status='expired' WHERE status='pending' AND created<? AND kind NOT IN ('waiting_on_user','waiting_on_approval')",
                (now - max_age,),
            )
            rows = self.db.execute(
                "SELECT * FROM events WHERE status='pending' AND due<=? ORDER BY CASE WHEN kind IN ('waiting_on_user','waiting_on_approval') THEN 0 ELSE 1 END,created LIMIT 100",
                (now,),
            ).fetchall()
            valid = []
            for row in rows:
                validity = self.valid(row)
                if validity is False:
                    self.db.execute(
                        "UPDATE events SET status='cancelled' WHERE id=?", (row["id"],)
                    )
                elif validity is True and len(valid) < limit:
                    valid.append(dict(row))
            return valid

    def transition(self, ids, status, **updates):
        allowed = {"attempts", "speech", "error", "due", "played_at"}
        if not set(updates) <= allowed:
            raise ValueError("Unknown delivery field")
        fields = {"status": status, **updates}
        with self.db:
            for event_id in ids:
                self.db.execute(
                    "UPDATE events SET "
                    + ",".join(k + "=?" for k in fields)
                    + " WHERE id=?",
                    (*fields.values(), event_id),
                )

    def reminders(self, now, interval):
        with self.db:
            rows = self.db.execute(
                "SELECT * FROM events WHERE kind IN ('waiting_on_user','waiting_on_approval') AND status='played' AND repeat_of IS NULL AND played_at<=?",
                (now - interval,),
            ).fetchall()
            for row in rows:
                if self.valid(row):
                    body = json.loads(row["body"])
                    reminder_id = hashlib.sha256(
                        (row["id"] + ":reminder").encode()
                    ).hexdigest()[:32]
                    body.update(
                        event_id=reminder_id,
                        evidence="同一事项仍待处理，这是唯一一次补充提醒。",
                    )
                    self.db.execute(
                        "INSERT OR IGNORE INTO events(id,thread_id,turn_id,kind,body,created,due,repeat_of) VALUES (?,?,?,?,?,?,?,?)",
                        (
                            reminder_id,
                            row["thread_id"],
                            row["turn_id"],
                            row["kind"],
                            json.dumps(body, ensure_ascii=False),
                            now,
                            now,
                            row["id"],
                        ),
                    )

    def recent(self, limit=20):
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT id,thread_id,kind,status,created,attempts,speech,error FROM events ORDER BY created DESC LIMIT ?",
                (limit,),
            )
        ]
