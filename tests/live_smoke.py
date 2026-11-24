"""Opt-in real model/TTS smoke test through the production service entry point.

Uses a disposable Codex home, not the user's task files. The test speaks once.
Run: .venv/bin/python tests/live_smoke.py
"""
import datetime as dt
import json
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from codex_voice.config import atomic_json, load, secrets


def main():
    real = load()
    with tempfile.TemporaryDirectory(prefix="codex-voice-smoke-") as directory:
        root = Path(directory)
        cfg = {
            **real,
            "state_dir": str(root / "state"),
            "codex_home": str(root / "codex"),
            "codex_login_home": real["codex_login_home"] or real["codex_home"],
            "daemon_enabled": False,
            "poll_seconds": 0.25,
            "merge_seconds": 0,
        }
        cfg_path = root / "config.json"
        atomic_json(cfg_path, cfg)
        atomic_json(root / "secrets.json", secrets(real))
        now = dt.datetime.now(dt.timezone.utc)
        rollout = (
            root
            / "codex/sessions"
            / now.astimezone().strftime("%Y/%m/%d")
            / "rollout-smoke.jsonl"
        )
        rollout.parent.mkdir(parents=True)

        def append(type_, payload):
            with rollout.open("a") as f:
                f.write(
                    json.dumps(
                        {
                            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
                            "type": type_,
                            "payload": payload,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

        append("session_meta", {"id": "smoke-test", "source": "cli"})
        append("event_msg", {"type": "task_started", "turn_id": "smoke-turn"})
        proc = subprocess.Popen(
            [sys.executable, "-m", "codex_voice", "--config", str(cfg_path), "run"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 10
            while not (root / "state/health.json").exists():
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Smoke service failed to start")
                time.sleep(0.1)
            append(
                "event_msg",
                {
                    "type": "task_complete",
                    "turn_id": "smoke-turn",
                    "last_agent_message": "这是语音助理的隔离模拟任务，轮询、模型组织语言和语音播放全链路正在测试，不是实际工作结果。",
                },
            )
            deadline = time.monotonic() + 100
            while time.monotonic() < deadline:
                with sqlite3.connect(root / "state/state.sqlite") as db:
                    row = db.execute(
                        "SELECT status,speech,error,attempts FROM events"
                    ).fetchone()
                if row and row[0] in ("played", "failed", "uncertain"):
                    print(
                        json.dumps(
                            {
                                "delivery": row[0],
                                "speech": row[1],
                                "error": row[2],
                                "attempts": row[3],
                                "task_files_modified": False,
                            },
                            ensure_ascii=False,
                        )
                    )
                    if row[0] != "played" or row[2] is not None:
                        raise RuntimeError(
                            "Live smoke did not complete through the selected model and TTS"
                        )
                    # A restart must not repeat an already played notification.
                    proc.send_signal(signal.SIGTERM)
                    proc.wait(10)
                    proc = subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "codex_voice",
                            "--config",
                            str(cfg_path),
                            "run",
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    time.sleep(1)
                    with sqlite3.connect(root / "state/state.sqlite") as db:
                        rows = db.execute(
                            "SELECT status,attempts FROM events"
                        ).fetchall()
                    if rows != [("played", 1)]:
                        raise RuntimeError("Restart changed a completed notification")
                    print("Restart readback: one played event, one attempt, no replay.")
                    return
                if proc.poll() is not None:
                    raise RuntimeError("Smoke service exited unexpectedly")
                time.sleep(0.25)
            raise TimeoutError("Live smoke timed out")
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


if __name__ == "__main__":
    main()
