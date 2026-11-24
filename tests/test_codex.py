import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_voice.codex import CodexComposer, CodexFailure
from codex_voice.composer import create_composer
from codex_voice.config import DEFAULTS, validate
from codex_voice.harness import Harness
from codex_voice.service import Runner
from codex_voice.store import Store


class CodexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.command = self.root / "codex"
        self.cfg = {
            **DEFAULTS,
            "state_dir": str(self.root),
            "codex_home": str(self.root / "home"),
            "codex_command": str(self.command),
            "call_name": "哥哥",
        }
        Path(self.cfg["codex_home"]).mkdir()
        (Path(self.cfg["codex_home"]) / "config.toml").write_text(
            'model = "local-model"\n'
        )
        self.event = {
            "id": "one",
            "body": json.dumps(
                {
                    "event_type": "completed",
                    "task_title": "检查项目",
                    "evidence": "明确结束记录",
                    "latest_summary": "完成，密钥 api_key=private-example，路径 /Users/example/private。",
                }
            ),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def fake(self, body):
        self.command.write_text(
            f"#!{sys.executable}\nimport sys,json,os,time\nfrom pathlib import Path\n"
            + body
        )
        self.command.chmod(0o700)

    def test_real_subprocess_receives_redacted_facts_and_no_credentials(self):
        capture = self.root / "capture.json"
        self.fake(
            f"""
args=sys.argv[1:]
prompt=json.loads(sys.stdin.read())
Path({str(capture)!r}).write_text(json.dumps({{'args':args,'prompt':prompt,'env':dict(os.environ)}}))
output=Path(args[args.index('--output-last-message')+1])
output.write_text(json.dumps({{'text':'哥哥，项目本轮已结束，请查看检查结果。'}}))
print(json.dumps({{'type':'item.completed','item':{{'type':'agent_message'}}}}))
"""
        )
        with patch.dict(
            os.environ,
            {
                "VOLC_TTS_API_KEY": "tts-private",
                "OPENAI_API_KEY": "openai-private",
                "DEEPSEEK_API_KEY": "ds-private",
            },
        ):
            text = CodexComposer(self.cfg).compose([self.event])
        self.assertEqual(text, "哥哥，项目本轮已结束，请查看检查结果。")
        seen = json.loads(capture.read_text())
        self.assertNotIn("private-example", json.dumps(seen["prompt"]))
        self.assertNotIn("/Users/example", json.dumps(seen["prompt"]))
        for key in ("VOLC_TTS_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
            self.assertNotIn(key, seen["env"])
        self.assertIn("--ephemeral", seen["args"])
        self.assertIn("--ignore-user-config", seen["args"])
        self.assertIn('forced_login_method="chatgpt"', seen["args"])
        self.assertIn("local-model", seen["args"])
        self.assertEqual(list((self.root / "codex-composer").iterdir()), [])

    def test_rejects_tools_invalid_schema_and_sensitive_speech(self):
        for data, item in [
            ({"text": "哥哥，任务已结束。"}, "command_execution"),
            ({"text": "哥哥，任务已结束。", "extra": "x"}, "agent_message"),
            ({"text": "哥哥，请查看 /Users/example/private"}, "agent_message"),
        ]:
            with self.subTest(data=data, item=item):
                self.fake(
                    f"""
args=sys.argv
Path(args[args.index('--output-last-message')+1]).write_text({json.dumps(data)!r})
print(json.dumps({{'item':{{'type':{item!r}}}}}))
"""
                )
                with self.assertRaises(CodexFailure):
                    CodexComposer(self.cfg).compose([self.event])

    def test_failure_does_not_leak_provider_output_or_use_stdout_as_speech(self):
        self.fake(
            "sys.stderr.write('Bearer private-secret'); print('哥哥，任务完成。'); sys.exit(1)\n"
        )
        with self.assertRaises(CodexFailure) as context:
            CodexComposer(self.cfg).compose([self.event])
        self.assertNotIn("private-secret", str(context.exception))

    def test_timeout_and_cancellation_reap_owned_process(self):
        self.fake("time.sleep(30)\n")
        composer = CodexComposer({**self.cfg, "model_timeout_seconds": 0.1})
        with self.assertRaisesRegex(CodexFailure, "超时"):
            composer.compose([self.event])
        self.assertIsNone(composer.process)
        composer = CodexComposer(self.cfg)
        errors = []

        def run():
            try:
                composer.compose([self.event])
            except CodexFailure as error:
                errors.append(error)

        worker = threading.Thread(target=run)
        worker.start()
        deadline = time.monotonic() + 3
        while composer.process is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNotNone(composer.process)
        proc = composer.process
        composer.cancel()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertIsNotNone(proc.poll())
        self.assertEqual(len(errors), 1)

    def test_factory_and_config_validation(self):
        self.assertIsInstance(create_composer(self.cfg), CodexComposer)
        self.assertIsInstance(
            create_composer({**self.cfg, "text_provider": "deepseek"}), Harness
        )
        for changes in (
            {"text_provider": "unknown"},
            {"codex_model": "bad model; cmd"},
            {"codex_command": ""},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate({**self.cfg, **changes})

    def test_provider_hot_reload_waits_for_inflight_work(self):
        store = Store(self.root / "state.sqlite")
        runner = Runner(self.cfg, store)
        original = runner.harness
        try:
            runner.future = object()
            next_cfg = {**self.cfg, "text_provider": "deepseek"}
            runner.reconfigure(next_cfg)
            self.assertIs(runner.harness, original)
            runner.future = None
            runner.reconfigure(next_cfg)
            self.assertIsInstance(runner.harness, Harness)
            self.assertEqual(runner.provider, "deepseek")
        finally:
            runner.future = None
            runner.stop()
            store.close()
