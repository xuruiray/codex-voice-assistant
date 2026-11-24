from __future__ import annotations

import concurrent.futures
import datetime as dt
import fcntl
import logging
import os
import signal
import subprocess
import time
from pathlib import Path

from .config import atomic_json, load
from .daemon import DaemonWatcher
from .composer import create_composer
from .rollout import RolloutWatcher
from .speech import VolcTTS, fallback_text
from .store import Store

log = logging.getLogger("codex_voice")


def quiet_now(cfg, when=None):
    now = (when or dt.datetime.now().astimezone()).time().replace(tzinfo=None)
    for start, end in cfg["quiet_hours"]:
        a, b = dt.time.fromisoformat(start), dt.time.fromisoformat(end)
        if (a <= now < b) if a < b else (now >= a or now < b):
            return True
    return False


def fallback_beep():
    # A local system sound still alerts if speech synthesis is unavailable.
    sound = Path("/System/Library/Sounds/Glass.aiff")
    if sound.exists():
        subprocess.run(
            ["/usr/bin/afplay", "-v", "0.3", str(sound)],
            timeout=5,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


class Runner:
    def __init__(self, cfg, store, *, harness=None, tts=None):
        self.cfg, self.store = cfg, store
        self.harness = harness or create_composer(cfg)
        self.provider = cfg["text_provider"]
        self.tts = tts or VolcTTS(cfg)
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.future = None
        self.batch = None
        self.stage = None
        self.audio = None

    def reconfigure(self, cfg):
        self.cfg = self.tts.cfg = cfg
        # A provider switch takes effect between notifications, never mid-request.
        if self.provider != cfg["text_provider"]:
            if self.future:
                return
            self.harness = create_composer(cfg)
            self.provider = cfg["text_provider"]
        self.harness.cfg = cfg

    def stop(self):
        cancel = getattr(self.harness, "cancel", None)
        if cancel:
            cancel()
        self.pool.shutdown(wait=True)
        if self.stage == "play" and self.future and self.future.done():
            self.finish(time.time())
        if self.audio:
            self.audio.unlink(missing_ok=True)

    def tick(self, now=None):
        now = now or time.time()
        if self.future and self.future.done():
            self.finish(now)
        if self.future:
            return
        self.reconfigure(self.cfg)
        manual_only = not self.cfg["enabled"] or quiet_now(self.cfg)
        if not manual_only:
            self.store.reminders(now, self.cfg["reminder_seconds"])
        batch = self.store.due(now, self.cfg["max_age_seconds"], only_tests=manual_only)
        if not batch:
            return
        self.batch = batch
        ids = [e["id"] for e in batch]
        self.store.transition(
            ids, "preparing", attempts=max(e["attempts"] for e in batch) + 1
        )
        self.stage = "compose"
        self.future = self.pool.submit(self.compose, batch)

    def compose(self, batch):
        try:
            return self.harness.compose(batch), None
        except Exception as exc:
            # A mandatory reminder survives a model outage; generic text makes
            # no new factual claims. The delivery record identifies fallback.
            return fallback_text(batch, self.cfg["call_name"]), type(exc).__name__

    def finish(self, now):
        future, self.future = self.future, None
        ids = [e["id"] for e in self.batch]
        try:
            result = future.result()
            if self.stage == "play":
                self.store.transition(ids, "played", played_at=now)
                log.info("Played %s notification(s)", len(ids))
                self.cleanup()
                return
            valid = [e for e in self.batch if self.store.valid(e) is True]
            # A combined sentence could still mention a resolved item. Re-compose
            # only the remaining events instead of playing stale mixed content.
            if len(valid) != len(self.batch):
                gone = [e["id"] for e in self.batch if self.store.valid(e) is False]
                if gone:
                    self.store.transition(gone, "cancelled")
                if valid:
                    self.store.transition([e["id"] for e in valid], "pending", due=now)
                unknown = [e["id"] for e in self.batch if self.store.valid(e) is None]
                if unknown:
                    self.store.transition(unknown, "pending", due=now + 30)
                self.cleanup()
                return
            if (
                self.stage != "play"
                and (not self.cfg["enabled"] or quiet_now(self.cfg))
                and not all(e["kind"] == "test" for e in self.batch)
            ):
                self.store.transition(ids, "pending", due=now + 30)
                self.cleanup()
                return
            if self.stage == "compose":
                text, fallback = result
                self.store.transition(
                    ids,
                    "synthesizing",
                    speech=text,
                    error=f"Model fallback: {fallback}" if fallback else None,
                )
                self.audio = Path(self.cfg["state_dir"]) / (ids[0] + ".mp3")
                self.stage = "synthesize"
                self.future = self.pool.submit(self.tts.synthesize, text, self.audio)
            elif self.stage == "synthesize":
                self.store.transition(ids, "playing")
                self.stage = "play"
                self.future = self.pool.submit(self.tts.play, self.audio)
        except Exception as exc:
            attempts = max(
                self.store.db.execute(
                    "SELECT attempts FROM events WHERE id=?", (i,)
                ).fetchone()[0]
                for i in ids
            )
            # Sound may already have started; a player failure is ambiguous.
            status = (
                "uncertain"
                if self.stage == "play"
                else ("failed" if attempts >= self.cfg["max_attempts"] else "pending")
            )
            self.store.transition(
                ids,
                status,
                due=now + min(120, 10 * 2**attempts),
                error=f"{self.stage}: {type(exc).__name__}",
            )
            log.warning(
                "Notification %s failed (%s); state=%s",
                self.stage,
                type(exc).__name__,
                status,
            )
            if status == "failed":
                try:
                    fallback_beep()
                except Exception:
                    pass
            self.cleanup()

    def cleanup(self):
        if self.audio:
            self.audio.unlink(missing_ok=True)
        self.audio = self.stage = self.batch = self.future = None


def run(cfg, observe_only=False):
    state = Path(cfg["state_dir"])
    lock = (state / "service.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError("Codex voice assistant is already running") from None
    os.chmod(state / "service.lock", 0o600)
    store = Store(state / "state.sqlite")
    store.recover()
    watcher, daemon = RolloutWatcher(store, cfg), DaemonWatcher(store, cfg)
    runner = Runner(cfg, store)
    from .webui import UI

    ui = UI(cfg).start()
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopping:
            tick = time.monotonic()
            try:
                cfg = load(Path(cfg["config_path"]))
                watcher.cfg = daemon.cfg = cfg
                runner.reconfigure(cfg)
                watcher.poll()
                daemon.poll()
                if not observe_only:
                    runner.tick()
                atomic_json(
                    state / "health.json",
                    {
                        "pid": os.getpid(),
                        "updated_at": time.time(),
                        "enabled": cfg["enabled"],
                        "observe_only": observe_only,
                        "rollout_files": len(watcher.paths),
                        "daemon_loaded_threads": daemon.loaded_count,
                        "daemon_error": daemon.error,
                        "delivery_stage": runner.stage,
                        "text_provider": runner.provider,
                        "desktop_approval_coverage": "unavailable: desktop does not expose pending approval flags to an independent observer",
                    },
                )
            except Exception as exc:
                log.warning("Poll failed (%s); no status inferred", type(exc).__name__)
            delay = max(0, cfg["poll_seconds"] - (time.monotonic() - tick))
            deadline = time.monotonic() + delay
            while not stopping and time.monotonic() < deadline:
                time.sleep(min(0.2, max(0, deadline - time.monotonic())))
    finally:
        ui.close()
        runner.stop()
        daemon.close()
        store.close()
        lock.close()
