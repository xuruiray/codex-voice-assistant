import datetime as dt
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_voice.config import DEFAULTS, atomic_json
from codex_voice.daemon import DaemonWatcher
from codex_voice.rollout import RolloutWatcher
from codex_voice.service import Runner, quiet_now
from codex_voice.speech import TTSFailure, VolcTTS
from codex_voice.store import Store


class Environment(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = time.time()
        self.cfg = {
            **DEFAULTS,
            "codex_home": str(self.root / "codex"),
            "state_dir": str(self.root),
            "secrets_path": str(self.root / "secrets.json"),
            "merge_seconds": 0,
        }
        self.store = Store(self.root / "state.sqlite")
        self.watcher = RolloutWatcher(self.store, self.cfg)
        self.file = (
            self.root
            / "codex/sessions"
            / dt.datetime.fromtimestamp(self.now).strftime("%Y/%m/%d")
            / "rollout-test.jsonl"
        )
        self.file.parent.mkdir(parents=True)
        self.append("session_meta", {"id": "thread-1", "source": "cli"})
        self.append("event_msg", {"type": "task_started", "turn_id": "turn-1"})
        self.watcher.poll(self.now)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def append(self, kind, payload, stamp=None):
        record = {
            "timestamp": dt.datetime.fromtimestamp(
                stamp or self.now + 1, dt.timezone.utc
            ).isoformat(),
            "type": kind,
            "payload": payload,
        }
        with self.file.open("a") as f:
            f.write(json.dumps(record) + "\n")

    def question(self, async_=True, key="call_question"):
        self.append(
            "response_item",
            {
                "type": "function_call",
                "name": "request_user_input_async" if async_ else "request_user_input",
                "call_id": key,
                "arguments": json.dumps(
                    {"questions": [{"title": "你想选择哪个方案？"}]}
                ),
            },
        )

    def complete(self):
        self.append(
            "event_msg",
            {
                "type": "task_complete",
                "turn_id": "turn-1",
                "last_agent_message": "本轮测试已经结束，有一项需要人工查看。",
            },
        )
        self.watcher.poll(self.now + 2)


class RolloutTests(Environment):
    def test_no_completion_from_inactivity(self):
        self.watcher.poll(self.now + 500)
        self.assertEqual(self.store.recent(), [])

    def test_terminal_record_is_deduplicated_across_restart(self):
        self.complete()
        self.complete()
        self.assertEqual(len(self.store.due(self.now + 3, 600)), 1)
        self.store.close()
        self.store = Store(self.root / "state.sqlite")
        RolloutWatcher(self.store, self.cfg).poll(self.now + 4)
        self.assertEqual(len(self.store.recent()), 1)

    def test_partial_line_is_not_lost(self):
        body = json.dumps(
            {
                "timestamp": dt.datetime.fromtimestamp(
                    self.now + 1, dt.timezone.utc
                ).isoformat(),
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "turn-1"},
            }
        )
        with self.file.open("a") as f:
            f.write(body[:25])
        self.watcher.poll(self.now + 2)
        self.assertEqual(self.store.recent(), [])
        with self.file.open("a") as f:
            f.write(body[25:] + "\n")
        self.watcher.poll(self.now + 3)
        self.assertEqual(len(self.store.recent()), 1)

    def test_async_acceptance_is_not_an_answer(self):
        self.question()
        self.append(
            "response_item",
            {
                "type": "function_call_output",
                "call_id": "call_question",
                "output": '{"accepted":true}',
            },
        )
        self.watcher.poll(self.now + 2)
        self.assertEqual(
            self.store.due(self.now + 3, 600)[0]["kind"], "waiting_on_user"
        )
        self.append(
            "response_item",
            {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "text": '<send_user_message_question_reply>[{"questionItemId":"[\\"request_user_input_async\\",\\"call_question\\",0]","answer":"方案一"}]</send_user_message_question_reply>'
                    }
                ],
            },
        )
        self.watcher.poll(self.now + 4)
        self.assertEqual(self.store.due(self.now + 5, 600), [])
        self.assertEqual(self.store.recent()[0]["status"], "cancelled")

    def test_sync_answer_cancels_question(self):
        self.question(False)
        self.append(
            "response_item",
            {
                "type": "function_call_output",
                "call_id": "call_question",
                "output": '{"answers":{}}',
            },
        )
        self.watcher.poll(self.now + 2)
        self.assertEqual(self.store.due(self.now + 3, 600), [])

    def test_new_turn_cancels_old_completion(self):
        self.complete()
        self.append("event_msg", {"type": "task_started", "turn_id": "turn-2"})
        self.watcher.poll(self.now + 3)
        self.assertEqual(self.store.due(self.now + 4, 600), [])

    def test_initial_history_is_silent(self):
        self.complete()
        self.store.close()
        (self.root / "state.sqlite").unlink()
        self.store = Store(self.root / "state.sqlite")
        RolloutWatcher(self.store, self.cfg).poll(self.now + 3)
        self.assertEqual(self.store.recent(), [])

    def test_historical_file_discovered_later_is_silent(self):
        self.file = self.file.with_name("rollout-imported.jsonl")
        self.append("session_meta", {"id": "old-thread"}, stamp=self.now - 86400)
        self.append(
            "event_msg",
            {"type": "task_complete", "turn_id": "old-turn"},
            stamp=self.now - 86400,
        )
        self.watcher.poll(self.now + 16)
        self.assertEqual(self.store.recent(), [])

    def test_old_file_cannot_overwrite_a_newer_turn_result(self):
        self.complete()
        self.file = self.file.with_name("rollout-z-old-session.jsonl")
        self.append("session_meta", {"id": "thread-1"}, stamp=self.now - 100)
        self.append(
            "event_msg",
            {"type": "task_started", "turn_id": "turn-old"},
            stamp=self.now - 100,
        )
        self.watcher.poll(self.now + 16)
        self.assertEqual(self.store.thread("thread-1")["status"], "completed")
        self.assertEqual(len(self.store.due(self.now + 17, 600)), 1)

    def test_old_completed_notice_expires(self):
        self.complete()
        self.assertEqual(self.store.due(self.now + 1000, 600), [])
        self.assertEqual(self.store.recent()[0]["status"], "expired")

    def test_one_reminder_survives_repeated_polls(self):
        self.question()
        self.watcher.poll(self.now + 2)
        row = self.store.due(self.now + 3, 600)[0]
        self.store.transition([row["id"]], "played", played_at=self.now + 3)
        for delta in (310, 320, 800):
            self.store.reminders(self.now + delta, 300)
        self.assertEqual(len(self.store.recent()), 2)
        reminder = self.store.due(self.now + 900, 600)
        self.assertEqual(len(reminder), 1)

    def test_restart_does_not_replay_possibly_audible_notification(self):
        self.complete()
        row = self.store.recent()[0]
        self.store.transition([row["id"]], "playing")
        self.store.recover()
        self.assertEqual(self.store.due(self.now + 3, 600), [])
        self.assertEqual(self.store.recent()[0]["status"], "uncertain")


class FakeHarness:
    def compose(self, events):
        return "你好，Codex 测试任务本轮已经结束，请查看结果。"


class FakeSpeech:
    def __init__(self):
        self.played = []
        self.on_synthesis = None

    def synthesize(self, text, path):
        path.write_bytes(b"test-audio")
        if self.on_synthesis:
            self.on_synthesis()
        return path

    def play(self, path):
        self.played.append(path.read_bytes())


class DeliveryTests(Environment):
    def drain(self, runner):
        for _ in range(200):
            runner.tick(self.now + 10)
            if runner.future is None and not self.store.due(self.now + 10, 600):
                return
            time.sleep(0.002)
        self.fail("notification did not settle")

    def test_completed_event_runs_through_persistent_queue_and_player(self):
        self.complete()
        speech = FakeSpeech()
        runner = Runner(self.cfg, self.store, harness=FakeHarness(), tts=speech)
        try:
            self.drain(runner)
            self.assertEqual(speech.played, [b"test-audio"])
            self.assertEqual(self.store.recent()[0]["status"], "played")
            self.assertEqual(list(self.root.glob("*.mp3")), [])
        finally:
            runner.stop()

    def test_answer_during_synthesis_prevents_playback(self):
        self.question()
        self.watcher.poll(self.now + 2)
        speech = FakeSpeech()
        ready = threading.Event()
        release = threading.Event()
        speech.on_synthesis = lambda: (ready.set(), release.wait(2))
        runner = Runner(self.cfg, self.store, harness=FakeHarness(), tts=speech)
        try:
            runner.tick(self.now + 3)
            for _ in range(100):
                runner.tick(self.now + 4)
                if ready.is_set():
                    break
                time.sleep(0.002)
            self.assertTrue(ready.is_set())
            self.append(
                "response_item",
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "text": "<send_user_message_question_reply>call_question</send_user_message_question_reply>"
                        }
                    ],
                },
            )
            self.watcher.poll(self.now + 5)
            release.set()
            self.drain(runner)
            self.assertEqual(speech.played, [])
            self.assertEqual(self.store.recent()[0]["status"], "cancelled")
        finally:
            release.set()
            runner.stop()

    def test_model_failure_uses_conservative_fallback(self):
        class Broken:
            def compose(self, events):
                raise RuntimeError("provider down")

        self.complete()
        speech = FakeSpeech()
        runner = Runner(self.cfg, self.store, harness=Broken(), tts=speech)
        try:
            self.drain(runner)
            row = self.store.recent()[0]
            self.assertEqual(row["status"], "played")
            self.assertIn("Model fallback", row["error"])
            self.assertNotIn("成功", row["speech"])
        finally:
            runner.stop()

    def test_speech_failure_retries_without_claiming_delivery(self):
        class Broken(FakeSpeech):
            def synthesize(self, text, path):
                raise TTSFailure("synthetic outage")

        self.complete()
        runner = Runner(self.cfg, self.store, harness=FakeHarness(), tts=Broken())
        try:
            self.drain(runner)
            row = self.store.recent()[0]
            self.assertEqual(row["status"], "pending")
            self.assertEqual(row["attempts"], 1)
        finally:
            runner.stop()

    def test_pause_keeps_new_events_without_playing(self):
        self.complete()
        self.cfg["enabled"] = False
        speech = FakeSpeech()
        runner = Runner(self.cfg, self.store, harness=FakeHarness(), tts=speech)
        try:
            runner.tick(self.now + 3)
            self.assertIsNone(runner.future)
            self.assertEqual(speech.played, [])
            self.assertEqual(self.store.recent()[0]["status"], "pending")
        finally:
            runner.stop()


class TTSTests(Environment):
    def response(self, body):
        class Response(io.BytesIO):
            headers = {"Content-Type": "text/event-stream; charset=utf-8"}

        return Response(body)

    def test_real_sse_decoder_writes_only_complete_audio(self):
        import base64

        atomic_json(Path(self.cfg["secrets_path"]), {"volc_api_key": "test-secret"})
        body = (
            b'data: {"code":0,"data":"'
            + base64.b64encode(b"audio")
            + b'"}\n\ndata: {"code":20000000}\n\n'
        )
        target = self.root / "output.mp3"
        with patch("urllib.request.urlopen", return_value=self.response(body)) as http:
            VolcTTS(self.cfg).synthesize("你好，测试任务需要你查看。", target)
        self.assertEqual(target.read_bytes(), b"audio")
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(http.call_args[0][0].get_header("X-api-key"), "test-secret")

    def test_truncated_stream_never_becomes_playable_file(self):
        atomic_json(Path(self.cfg["secrets_path"]), {"volc_api_key": "test-secret"})
        with patch(
            "urllib.request.urlopen",
            return_value=self.response(b'data: {"code":0,"data":"YXVkaW8="}\n\n'),
        ):
            with self.assertRaises(TTSFailure):
                VolcTTS(self.cfg).synthesize(
                    "你好，测试任务需要你查看。", self.root / "bad.mp3"
                )
        self.assertFalse((self.root / "bad.mp3").exists())

    def test_service_error_does_not_leak_credential(self):
        atomic_json(Path(self.cfg["secrets_path"]), {"volc_api_key": "test-secret"})
        with patch(
            "urllib.request.urlopen",
            return_value=self.response(
                b'data: {"code":45000001,"message":"test-secret"}\n\n'
            ),
        ):
            with self.assertRaises(TTSFailure) as error:
                VolcTTS(self.cfg).synthesize(
                    "你好，测试任务需要你查看。", self.root / "bad.mp3"
                )
        self.assertNotIn("test-secret", str(error.exception))


class DaemonIntegrationTests(Environment):
    def test_live_websocket_flags_and_completion(self):
        from websockets.sync.server import unix_serve

        socket_path = str(self.root / "codex.sock")
        current = {
            "status": {"type": "active", "activeFlags": []},
            "turn": "inProgress",
        }
        methods = []

        def handler(ws):
            for text in ws:
                r = json.loads(text)
                method = r.get("method")
                methods.append(method)
                if current.get("disconnect"):
                    ws.close()
                    return
                if "id" not in r:
                    continue
                if method == "initialize":
                    result = {}
                elif method == "thread/loaded/list":
                    result = {"data": ["live-thread"], "nextCursor": None}
                elif method == "thread/read":
                    result = {
                        "thread": {
                            "id": "live-thread",
                            "name": "实时任务",
                            "status": current["status"],
                        }
                    }
                elif method == "thread/turns/list":
                    result = {
                        "data": [{"id": "live-turn", "status": current["turn"]}],
                        "nextCursor": None,
                    }
                else:
                    raise AssertionError(method)
                ws.send(json.dumps({"id": r["id"], "result": result}))

        with unix_serve(handler, path=socket_path) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            watcher = DaemonWatcher(
                self.store, {**self.cfg, "daemon_socket": socket_path}
            )
            try:
                watcher.poll(self.now)
                self.assertIsNone(watcher.error)
                current["status"] = {
                    "type": "active",
                    "activeFlags": ["waitingOnApproval"],
                }
                watcher.poll(self.now + 3)
                rows = self.store.due(self.now + 4, 600)
                self.assertEqual([r["kind"] for r in rows], ["waiting_on_approval"])
                current["status"] = {"type": "active", "activeFlags": []}
                watcher.poll(self.now + 4)
                current["status"] = {
                    "type": "active",
                    "activeFlags": ["waitingOnApproval"],
                }
                watcher.poll(self.now + 5)
                self.assertEqual(
                    len(
                        [
                            r
                            for r in self.store.recent()
                            if r["kind"] == "waiting_on_approval"
                        ]
                    ),
                    2,
                )
                with patch("codex_voice.store.time.time", return_value=self.now + 100):
                    self.assertEqual(self.store.due(self.now + 100, 600), [])
                    self.assertTrue(
                        any(r["status"] == "pending" for r in self.store.recent())
                    )
                current["status"] = {"type": "idle"}
                current["turn"] = "completed"
                watcher.poll(self.now + 6)
                self.assertEqual(
                    [r["kind"] for r in self.store.due(self.now + 7, 600)],
                    ["completed"],
                )
                self.assertNotIn("thread/resume", methods)
                self.assertNotIn("turn/start", methods)
                current["status"] = {
                    "type": "active",
                    "activeFlags": ["waitingOnApproval"],
                }
                current["turn"] = "inProgress"
                watcher.poll(self.now + 8)
                self.assertEqual(
                    [r["kind"] for r in self.store.due(self.now + 8, 600)],
                    ["waiting_on_approval"],
                )
                current["disconnect"] = True
                watcher.poll(self.now + 9)
                self.assertIsNotNone(watcher.error)
                self.assertEqual(self.store.due(self.now + 9, 600), [])
            finally:
                watcher.close()
                server.shutdown()
                thread.join(2)


class QuietHoursTests(unittest.TestCase):
    def test_overnight_window(self):
        cfg = {"quiet_hours": [["23:00", "08:00"]]}
        self.assertTrue(quiet_now(cfg, dt.datetime(2026, 11, 24, 23, 30)))
        self.assertTrue(quiet_now(cfg, dt.datetime(2026, 11, 24, 7, 30)))
        self.assertFalse(quiet_now(cfg, dt.datetime(2026, 11, 24, 8, 0)))


if __name__ == "__main__":
    unittest.main()
