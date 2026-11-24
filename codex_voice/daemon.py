from __future__ import annotations

import json
import time
from pathlib import Path

from websockets.sync.client import unix_connect

from .store import Store


class DaemonWatcher:
    """Observe only threads actually loaded in this daemon; never resume them."""

    def __init__(self, store: Store, cfg: dict):
        self.store, self.cfg = store, cfg
        self.ws = None
        self.sequence = 0
        self.loaded_count = None
        self.error = None
        self.retry_at = 0

    def close(self):
        if self.ws:
            self.ws.close()
        self.ws = None

    def rpc(self, method, params):
        self.sequence += 1
        request_id = self.sequence
        self.ws.send(json.dumps({"id": request_id, "method": method, "params": params}))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            record = json.loads(
                self.ws.recv(timeout=max(0.01, deadline - time.monotonic()))
            )
            if record.get("id") != request_id:
                # Deliberately don't answer server-side approvals. This is an observer.
                continue
            if "error" in record:
                raise RuntimeError(
                    f"Codex {method} returned error {record['error'].get('code')}"
                )
            return record["result"]
        raise TimeoutError("Codex status request timed out")

    def poll(self, now=None):
        now = now or time.time()
        if not self.cfg["daemon_enabled"] or now < self.retry_at:
            return
        try:
            if not self.ws:
                self.ws = unix_connect(
                    self.cfg["daemon_socket"],
                    open_timeout=3,
                    close_timeout=1,
                    compression=None,
                    max_size=8 * 1024 * 1024,
                )
                self.rpc(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "codex_voice_observer",
                            "version": "0.1.0",
                        },
                        "capabilities": {"experimentalApi": True},
                    },
                )
                self.ws.send('{"method":"initialized"}')
            ids, cursor = [], None
            while True:
                page = self.rpc("thread/loaded/list", {"limit": 100, "cursor": cursor})
                ids.extend(page["data"])
                cursor = page.get("nextCursor")
                if not cursor:
                    break
            self.loaded_count = len(ids)
            for thread_id in ids:
                thread = self.rpc(
                    "thread/read", {"threadId": thread_id, "includeTurns": False}
                )["thread"]
                if thread.get("parentThreadId") and not self.cfg["include_subagents"]:
                    continue
                # Read the latest turn rather than hydrating the entire history.
                turns = self.rpc(
                    "thread/turns/list",
                    {"threadId": thread_id, "limit": 1, "sortDirection": "desc"},
                )["data"]
                latest = turns[0] if turns else {}
                self.observe(thread, latest, now)
            self.error = None
        except Exception as exc:
            self.error = f"{type(exc).__name__}: live daemon unavailable"
            self.retry_at = now + 30
            self.close()
            with self.store.db:
                for row in self.store.db.execute(
                    "SELECT id,body FROM threads"
                ).fetchall():
                    thread = json.loads(row["body"])
                    if thread.get("daemon_available"):
                        thread["daemon_available"] = False
                        self.store.save_thread(thread)

    def observe(self, live, latest, now):
        with self.store.db:
            t = self.store.thread(live["id"])
            old_turn = t.get("daemon_turn")
            old_status = t.get("daemon_status")
            status = live["status"]
            if status["type"] == "notLoaded":
                return
            t["title"] = (
                live.get("name")
                or self.title(live["id"])
                or t.get("title", "Codex 任务")
            )
            t["source"] = "daemon"
            turn_id = latest.get("id") or t.get("turn_id") or "unknown"
            t["turn_id"] = turn_id
            turn_status = latest.get("status")
            if turn_status:
                t["status"] = turn_status
            flags = (
                set(status.get("activeFlags", []))
                if status["type"] == "active"
                else set()
            )
            pending = t.setdefault("pending", {})
            for key in list(pending):
                if key.startswith("daemon:") and key.split(":", 2)[1] not in flags:
                    del pending[key]
            for flag, kind in (
                ("waitingOnApproval", "waiting_on_approval"),
                ("waitingOnUserInput", "waiting_on_user"),
            ):
                if flag in flags:
                    existing = next(
                        (
                            k
                            for k in pending
                            if k.startswith(f"daemon:{flag}:{turn_id}:")
                        ),
                        None,
                    )
                    generation = t.setdefault("daemon_wait_generations", {})
                    if existing:
                        key = existing
                    else:
                        generation[flag] = generation.get(flag, 0) + 1
                        key = f"daemon:{flag}:{turn_id}:{generation[flag]}"
                    pending[key] = {
                        "kind": kind,
                        "text": "Codex 正在等待操作批准。"
                        if kind == "waiting_on_approval"
                        else "Codex 正在等待你的回答。",
                    }
                    self.store.enqueue(
                        t,
                        kind,
                        key,
                        now,
                        evidence=f"实时 ThreadActiveFlag={flag}",
                        summary=pending[key]["text"],
                    )
            # Suppress history on initial contact; recognize only observed transitions.
            if (
                old_turn
                and turn_status in ("completed", "failed")
                and (old_turn != turn_id or old_status == "inProgress")
            ):
                self.store.enqueue(
                    t,
                    turn_status,
                    turn_id,
                    now,
                    self.cfg["merge_seconds"],
                    evidence=f"实时 turn.status={turn_status}",
                    summary=t.get("summary", ""),
                )
            t.update(
                daemon_turn=turn_id,
                daemon_status=turn_status,
                daemon_seen_at=now,
                daemon_available=True,
            )
            self.store.save_thread(t)

    def title(self, thread_id):
        import sqlite3

        for base in (
            Path(self.cfg["codex_home"]),
            Path(self.cfg["codex_home"]) / "sqlite",
        ):
            try:
                with sqlite3.connect(
                    f"file:{base / 'state_5.sqlite'}?mode=ro", uri=True
                ) as db:
                    row = db.execute(
                        "SELECT title FROM threads WHERE id=?", (thread_id,)
                    ).fetchone()
                    if row:
                        return row[0]
            except sqlite3.Error:
                pass
        return None
