import concurrent.futures
import http.client
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from codex_voice.config import DEFAULTS, atomic_json, load, update_settings
from codex_voice.store import Store
from codex_voice.webui import UI


class WebUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "config.json"
        atomic_json(
            self.path, {**DEFAULTS, "state_dir": str(self.root / "state"), "ui_port": 0}
        )
        self.cfg = load(self.path)
        atomic_json(
            self.root / "secrets.json", {"volc_api_key": "fake-tts-key-for-ui-tests"}
        )
        self.store = Store(self.root / "state/state.sqlite")
        atomic_json(
            self.root / "state/health.json",
            {"pid": os.getpid(), "updated_at": time.time(), "rollout_files": 3},
        )
        self.ui = UI(self.cfg).start()

    def tearDown(self):
        self.ui.close()
        self.store.close()
        self.temp.cleanup()

    def request(self, path, body=None, authorized=True, headers=None):
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.ui.server.server_port, timeout=3
        )
        hdr = {
            "Content-Type": "application/json",
            **({"Authorization": "Bearer " + self.ui.token} if authorized else {}),
            **(headers or {}),
        }
        conn.request(
            "GET" if body is None else "POST",
            path,
            body=None if body is None else json.dumps(body),
            headers=hdr,
        )
        result = conn.getresponse()
        data = result.read()
        status = result.status
        response_headers = dict(result.getheaders())
        conn.close()
        return status, data, response_headers

    def test_authentication_and_origin_are_required(self):
        self.assertEqual(self.request("/api/config", authorized=False)[0], 401)
        self.assertEqual(
            self.request("/api/config", headers={"Origin": "https://example.com"})[0],
            403,
        )
        self.assertEqual(
            self.request("/api/config", headers={"Host": "example.com"})[0], 403
        )
        self.assertEqual(
            self.request("/api/config", headers={"Sec-Fetch-Site": "cross-site"})[0],
            403,
        )

    def test_public_shell_has_no_settings_or_credentials(self):
        status, body, headers = self.request("/", authorized=False)
        self.assertEqual(status, 200)
        self.assertIn(b"Codex Voice", body)
        self.assertNotIn(self.ui.token.encode(), body)
        self.assertNotIn(b"fake-tts-key", body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_assets_are_local_and_cannot_escape_the_asset_directory(self):
        status, body, headers = self.request(
            "/assets/icons/waveform.svg", authorized=False
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/svg+xml")
        self.assertEqual(
            self.request("/assets/../../../config.py", authorized=False)[0], 404
        )
        self.assertEqual(
            self.request("/assets/../../secrets.json", authorized=False)[0], 404
        )

    def test_saving_preferences_preserves_paths_and_secrets(self):
        old_secret = (self.root / "secrets.json").read_bytes()
        status, body, _ = self.request(
            "/api/config",
            {
                "call_name": "朋友",
                "volume": 0.35,
                "speaker": "zh_female_xiaohe_uranus_bigtts",
                "text_provider": "codex",
                "codex_model": "configured-model",
            },
        )
        self.assertEqual(status, 200)
        actual = load(self.path)
        self.assertEqual(actual["call_name"], "朋友")
        self.assertEqual(actual["state_dir"], self.cfg["state_dir"])
        self.assertEqual(actual["speaker"], "zh_female_xiaohe_uranus_bigtts")
        self.assertEqual(actual["text_provider"], "codex")
        self.assertEqual(actual["codex_model"], "configured-model")
        self.assertEqual(old_secret, (self.root / "secrets.json").read_bytes())
        self.assertNotIn(b"fake-tts-key", body)

    def test_invalid_values_do_not_change_configuration(self):
        original = self.path.read_bytes()
        for change in (
            {"volume": 10},
            {"poll_seconds": True},
            {"poll_seconds": float("nan")},
            {"quiet_hours": [["bad", "08:00"]]},
            {"quiet_hours": [["08:00", "08:00"]]},
            {"dsh_command": "/tmp/untrusted"},
            {"call_name": ""},
            {"max_attempts": 1.2},
            {"text_provider": "unknown"},
            {"codex_model": "bad model; cmd"},
        ):
            with self.subTest(change=change):
                self.assertEqual(self.request("/api/config", change)[0], 400)
                self.assertEqual(self.path.read_bytes(), original)

    def test_keys_can_be_replaced_but_never_read_back(self):
        value = "fake-replacement-key-for-tests"
        self.assertEqual(
            self.request("/api/credentials", {"volc_api_key": value})[0], 200
        )
        self.assertEqual(
            json.loads((self.root / "secrets.json").read_text())["volc_api_key"], value
        )
        _, body, _ = self.request("/api/config")
        self.assertNotIn(value.encode(), body)
        self.assertTrue(json.loads(body)["credentials"]["volc"])
        self.assertEqual((self.root / "secrets.json").stat().st_mode & 0o777, 0o600)

    def test_preview_requests_share_the_real_queue_and_are_deduplicated(self):
        first = json.loads(self.request("/api/test", {})[1])["event_id"]
        second = json.loads(self.request("/api/test", {})[1])["event_id"]
        self.assertEqual(first, second)
        rows = self.store.due(time.time(), 600)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "test")
        self.assertTrue(self.store.valid(rows[0]))

    def test_parallel_preview_requests_do_not_duplicate(self):
        def request():
            db = Store(self.root / "state/state.sqlite")
            try:
                return db.queue_test()
            finally:
                db.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(lambda _: request(), range(4)))
        self.assertEqual(len(set(ids)), 1)

    def test_status_and_history_use_real_delivery_records(self):
        event_id = self.store.queue_test()
        self.store.transition(
            [event_id], "played", played_at=time.time(), speech="朋友，这是试听提醒。"
        )
        _, body, _ = self.request("/api/status")
        status = json.loads(body)
        self.assertTrue(status["running"])
        self.assertEqual(
            status["stats"]["today_played"], 0
        )  # previews aren't work alerts
        _, body, _ = self.request("/api/events")
        events = json.loads(body)["events"]
        self.assertEqual(events[0]["status"], "played")
        self.assertEqual(events[0]["speech"], "朋友，这是试听提醒。")

    def test_concurrent_configuration_updates_do_not_lose_changes(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = [
                pool.submit(update_settings, self.path, {"volume": 0.25}),
                pool.submit(update_settings, self.path, {"poll_seconds": 7}),
            ]
            for result in results:
                result.result()
        cfg = load(self.path)
        self.assertEqual(cfg["volume"], 0.25)
        self.assertEqual(cfg["poll_seconds"], 7)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
