from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import time
from pathlib import Path

from .store import Store


def message_text(payload):
    return "\n".join(
        x.get("text", "") for x in payload.get("content", []) if isinstance(x, dict)
    )


class RolloutWatcher:
    """Incrementally poll persisted Codex events, never infer completion from silence.

    Desktop permissions aren't guaranteed to be persisted. Only explicit input
    calls and terminal events are recognized here; live daemon flags supplement
    permissions on threads actually loaded by that daemon.
    """

    def __init__(self, store: Store, cfg: dict):
        self.store, self.cfg = store, cfg
        self.home = Path(cfg["codex_home"])
        self.last_discover = 0
        self.paths = set()
        self.titles = {}

    def discover(self, now):
        if self.paths and now - self.last_discover < 15:
            return
        self.last_discover = now
        for base in (self.home, self.home / "sqlite"):
            db = base / "state_5.sqlite"
            if not db.exists():
                continue
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1) as c:
                for thread_id, path, title in c.execute(
                    "SELECT id,rollout_path,title FROM threads WHERE archived=0 AND updated_at>? ORDER BY updated_at DESC LIMIT 500",
                    (int(now - 7 * 86400),),
                ):
                    self.titles[thread_id] = title
                    if path:
                        self.paths.add(Path(path))
        # The directory scan catches files before the index is updated.
        for ago in range(3):
            day = dt.datetime.fromtimestamp(now - ago * 86400)
            self.paths.update(
                (self.home / "sessions" / day.strftime("%Y/%m/%d")).glob("*.jsonl")
            )
        self.paths.update(
            Path(r[0]) for r in self.store.db.execute("SELECT path FROM files")
        )

    def poll(self, now=None):
        now = time.time() if now is None else now
        self.discover(now)
        initial = not self.store.meta("baseline_done", False)
        if self.store.meta("watch_since") is None:
            with self.store.db:
                self.store.set_meta("watch_since", now)
        count = 0
        for path in sorted(self.paths):
            try:
                count += self.read(path, now, initial)
            except FileNotFoundError:
                continue
        if initial:
            with self.store.db:
                self.store.set_meta("baseline_done", True)
        return count

    def read(self, path, now, baseline):
        stat = path.stat()
        row = self.store.db.execute(
            "SELECT inode,offset FROM files WHERE path=?", (str(path),)
        ).fetchone()
        offset = (
            row["offset"]
            if row and row["inode"] == stat.st_ino and row["offset"] <= stat.st_size
            else 0
        )
        if offset == stat.st_size:
            return 0
        # Decode complete lines only; a partial write stays unconsumed.
        events = 0
        with path.open("rb") as f, self.store.db:
            f.seek(offset)
            current = self.store.thread("unknown")
            # The first metadata record identifies the file even after a restart.
            first = f.readline() if offset == 0 else None
            if first is None:
                f.seek(0)
                first = f.readline()
                f.seek(offset)
            if first.endswith(b"\n"):
                try:
                    meta = json.loads(first)
                    if meta.get("type") == "session_meta":
                        p = meta["payload"]
                        current = self.store.thread(p.get("id") or p.get("session_id"))
                        current["title"] = self.titles.get(
                            current["id"], current.get("title", "Codex 任务")
                        )
                        source = p.get("source", "")
                        current["subagent"] = (
                            isinstance(source, dict)
                            and bool({"subAgent", "subagent"} & source.keys())
                        ) or p.get("thread_source") == "subagent"
                        current["source"] = "rollout"
                except (ValueError, TypeError, KeyError):
                    return 0
            else:
                return 0
            if offset == 0:
                offset = f.tell()
                if baseline and stat.st_size > 512 * 1024:
                    f.seek(stat.st_size - 512 * 1024)
                    f.readline()
                    offset = f.tell()
            if current["id"] == "unknown":
                return 0
            while True:
                start = f.tell()
                line = f.readline()
                if not line or not line.endswith(b"\n"):
                    offset = start
                    break
                try:
                    record = json.loads(line)
                except ValueError:
                    # Complete malformed records are skipped without losing later events.
                    offset = f.tell()
                    continue
                recorded = record.get("timestamp")
                try:
                    stamp = (
                        dt.datetime.fromisoformat(
                            recorded.replace("Z", "+00:00")
                        ).timestamp()
                        if recorded
                        else now
                    )
                except (ValueError, AttributeError):
                    stamp = now
                if recorded and stamp < current.get("last_record_at", 0):
                    offset = f.tell()
                    events += 1
                    continue
                self.apply(
                    current,
                    record,
                    now,
                    emit=not baseline and stamp >= self.store.meta("watch_since", now),
                )
                if recorded:
                    current["last_record_at"] = stamp
                offset = f.tell()
                events += 1
            self.store.save_thread(current)
            self.store.db.execute(
                "INSERT OR REPLACE INTO files VALUES (?,?,?)",
                (str(path), stat.st_ino, offset),
            )
            # On first installation, only recently active explicit questions may alert.
            if baseline and stat.st_mtime > now - 600:
                for key, pending in current.get("pending", {}).items():
                    self.notify(current, pending["kind"], key, now, pending["text"])
        return events

    def notify(self, t, kind, key, now, summary=""):
        if t.get("subagent") and not self.cfg["include_subagents"]:
            return
        self.store.enqueue(
            t,
            kind,
            key,
            now,
            self.cfg["merge_seconds"] if kind in ("completed", "failed") else 0,
            evidence={
                "completed": "Codex 记录了本轮 task_complete；不代表整个项目成功。",
                "failed": "Codex 明确记录本轮失败。",
                "waiting_on_user": "Codex 发出了结构化用户问题，尚未记录对应回答。",
                "waiting_on_approval": "Codex 明确记录了待审批请求。",
            }[kind],
            summary=summary,
        )

    def apply(self, t, record, now, emit=True):
        payload = record.get("payload", {})
        kind = record.get("type")
        event = payload.get("type")
        if kind == "event_msg":
            if event in ("task_started", "turn_started"):
                t.update(
                    turn_id=payload.get("turn_id") or t.get("turn_id"),
                    status="inProgress",
                )
            elif event in ("task_complete", "turn_complete", "task_failed"):
                t["turn_id"] = payload.get("turn_id") or t.get("turn_id")
                failed = event == "task_failed" or payload.get("status") == "failed"
                t["status"] = "failed" if failed else "completed"
                t["summary"] = payload.get("last_agent_message") or t.get("summary", "")
                if emit:
                    self.notify(
                        t, t["status"], t.get("turn_id", "unknown"), now, t["summary"]
                    )
            elif event in ("turn_aborted", "task_interrupted"):
                t["status"] = "interrupted"
                t["pending"] = {}
            return
        if kind != "response_item":
            return
        if event == "message":
            text = message_text(payload)
            if (
                payload.get("role") == "assistant"
                and payload.get("phase") == "final_answer"
            ):
                t["summary"] = text
            if payload.get("role") == "user":
                if "<send_user_message_question_reply>" in text:
                    for call_id in re.findall(r"call_[A-Za-z0-9]+", text):
                        t["pending"].pop(call_id, None)
                elif "<environment_context>" not in text[:100]:
                    # New ordinary user input may replace a previously unanswered request.
                    t["pending"] = {}
            return
        if event in ("function_call", "custom_tool_call"):
            name = payload.get("name", "").split(".")[-1]
            if name not in ("request_user_input", "request_user_input_async"):
                return
            raw = payload.get("arguments", payload.get("input", "{}"))
            try:
                args = json.loads(raw) if isinstance(raw, str) else raw
            except ValueError:
                return
            questions = args.get("questions", [])
            text = "\n".join(
                q.get("question") or q.get("title", "")
                for q in questions
                if isinstance(q, dict)
            )
            key = payload.get("call_id") or payload.get("id")
            if key and text:
                t["pending"][key] = {
                    "kind": "waiting_on_user",
                    "text": text,
                    "async": name.endswith("_async"),
                }
                if emit:
                    self.notify(t, "waiting_on_user", key, now, text)
        elif event in ("function_call_output", "custom_tool_call_output"):
            key = payload.get("call_id")
            pending = t["pending"].get(key)
            if pending and not pending.get("async"):
                t["pending"].pop(key, None)
            elif pending:
                output = payload.get("output", "")
                # Tool failure is not an outstanding user question.
                if isinstance(output, str):
                    try:
                        result = json.loads(output)
                        if result.get("accepted") is False or result.get("error"):
                            t["pending"].pop(key, None)
                    except (ValueError, AttributeError):
                        pass
