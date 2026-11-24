from __future__ import annotations

import argparse
import getpass
import json
import logging
import logging.handlers
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

from .config import DEFAULTS, atomic_json, load, secrets
from .daemon import DaemonWatcher
from .harness import Harness, prepare_profile
from .service import run
from .speech import VolcTTS
from .store import Store


def print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def configure(cfg, args):
    path = Path(cfg["config_path"])
    settings = {k: cfg[k] for k in DEFAULTS}
    if args.api_key:
        credential_file = Path(cfg["secrets_path"])
        data = (
            json.loads(credential_file.read_text()) if credential_file.exists() else {}
        )
        value = getpass.getpass("火山 TTS API Key（输入不会显示）: ").strip()
        if not value:
            raise ValueError("API Key cannot be blank")
        data["volc_api_key"] = value
        atomic_json(Path(cfg["secrets_path"]), data)
    if args.deepseek_key:
        credential_file = Path(cfg["secrets_path"])
        data = (
            json.loads(credential_file.read_text()) if credential_file.exists() else {}
        )
        value = getpass.getpass("DeepSeek API Key（输入不会显示）: ").strip()
        if not value:
            raise ValueError("API Key cannot be blank")
        data["deepseek_api_key"] = value
        atomic_json(Path(cfg["secrets_path"]), data)
        settings["deepseek_provider"] = "deepseek-official"
    if args.account:
        settings["deepseek_provider"] = "deepseek-account"
    if args.speaker:
        settings["speaker"] = args.speaker
    if args.resource:
        settings["tts_resource_id"] = args.resource
    atomic_json(path, settings)
    prepare_profile(load(path))
    print("配置已保存。后台服务会在下一轮读取。")


def doctor(cfg):
    store = Store(Path(cfg["state_dir"]) / "state.sqlite")
    daemon = DaemonWatcher(store, cfg)
    daemon.poll()
    result = {
        "dsh_installed": Path(cfg["dsh_command"]).exists(),
        "dsh_provider": cfg["deepseek_provider"],
        "dsh_profile": cfg["dsh_profile"],
        "volc_key_configured": bool(secrets(cfg).get("volc_api_key")),
        "deepseek_api_key_configured": bool(secrets(cfg).get("deepseek_api_key")),
        "desktop_credential_store_exists": (
            Path(cfg["dsh_home"]) / ".credentials.yaml"
        ).exists(),
        "daemon_connected": daemon.error is None and daemon.ws is not None,
        "daemon_loaded_threads": daemon.loaded_count,
        "daemon_error": daemon.error,
        "desktop_completed_and_structured_questions": "persisted rollout events",
        "desktop_approval_flags": "not available to independent observer; not inferred",
        "config": cfg["config_path"],
        "secrets_mode": oct(Path(cfg["secrets_path"]).stat().st_mode & 0o777)
        if Path(cfg["secrets_path"]).exists()
        else None,
    }
    daemon.close()
    store.close()
    print_json(result)


def test_notification(cfg, no_play=False, tts_only=False):
    event = {
        "id": "manual-test",
        "body": json.dumps(
            {
                "event_type": "completed",
                "task_title": "语音助理安装测试",
                "evidence": "这是用户发起的测试通知，不是工作任务的实际状态。",
                "latest_summary": "请告知用户语音链路测试完成。",
            },
            ensure_ascii=False,
        ),
    }
    text = (
        f"{cfg['call_name']}，Codex 语音助理正在测试，现在可以听到提醒了。"
        if tts_only
        else Harness(cfg).compose([event])
    )
    path = Path(cfg["state_dir"]) / "test-notification.mp3"
    try:
        tts = VolcTTS(cfg)
        tts.synthesize(text, path)
        if not no_play:
            tts.play(path)
        print_json(
            {
                "model": "skipped" if tts_only else "DeepSeek Harness",
                "tts": "ok",
                "playback": "skipped" if no_play else "afplay exited successfully",
                "text": text,
                "audio_bytes": path.stat().st_size,
            }
        )
    finally:
        path.unlink(missing_ok=True)


def install_service(cfg):
    label = cfg["service_label"]
    prepare_profile(cfg)
    if not Path(cfg["config_path"]).exists():
        atomic_json(Path(cfg["config_path"]), {k: cfg[k] for k in DEFAULTS})
    state = Path(cfg["state_dir"])
    agents = Path.home() / "Library/LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    target = agents / (label + ".plist")
    data = {
        "Label": label,
        "ProgramArguments": [
            sys.executable,
            "-m",
            "codex_voice",
            "--config",
            cfg["config_path"],
            "run",
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 15,
        "ProcessType": "Background",
        "WorkingDirectory": str(state),
        "EnvironmentVariables": {
            "PYTHONUNBUFFERED": "1",
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
        },
        "StandardOutPath": str(state / "launchd.log"),
        "StandardErrorPath": str(state / "launchd.log"),
    }
    with (state / "launchd.log").open("a"):
        pass
    (state / "launchd.log").chmod(0o600)
    if target.exists():
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # bootout can return before launchd has removed the old registration.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            old = subprocess.run(
                ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if old.returncode:
                break
            time.sleep(0.1)
    target.write_bytes(plistlib.dumps(data))
    target.chmod(0o600)
    deadline = time.monotonic() + 5
    while True:
        started = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(target)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if started.returncode == 0:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("launchd could not register the user service")
        time.sleep(0.2)
    print("已安装并启动本机后台服务。")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Codex 本机语音助理")
    parser.add_argument("--config", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="检查本机接入与能力范围")
    p = sub.add_parser("configure", help="本机安全配置凭据")
    p.add_argument("--api-key", action="store_true")
    p.add_argument("--deepseek-key", action="store_true")
    p.add_argument("--account", action="store_true")
    p.add_argument("--speaker")
    p.add_argument("--resource")
    p = sub.add_parser("run", help="前台运行监听程序")
    p.add_argument("--observe-only", action="store_true")
    p = sub.add_parser("test", help="真实 DS → 火山 TTS → 本机播放测试")
    p.add_argument("--no-play", action="store_true")
    p.add_argument("--tts-only", action="store_true")
    sub.add_parser("status", help="查看服务健康状态")
    p = sub.add_parser("events", help="查看近期通知")
    p.add_argument("--limit", type=int, default=20)
    sub.add_parser("pause", help="暂停语音，继续记录状态")
    sub.add_parser("resume", help="恢复语音")
    sub.add_parser("install-service", help="安装并启动用户级 launchd 服务")
    sub.add_parser("uninstall-service", help="停止后台服务，保留配置和记录")
    args = parser.parse_args()
    try:
        cfg = load(args.config)
        if args.command == "doctor":
            doctor(cfg)
        elif args.command == "configure":
            configure(cfg, args)
        elif args.command == "test":
            test_notification(cfg, args.no_play, args.tts_only)
        elif args.command == "run":
            handler = logging.handlers.RotatingFileHandler(
                Path(cfg["state_dir"]) / "service.log", maxBytes=512000, backupCount=2
            )
            os.chmod(Path(cfg["state_dir"]) / "service.log", 0o600)
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s %(levelname)s %(message)s",
                handlers=[handler],
            )
            run(cfg, args.observe_only)
        elif args.command == "install-service":
            install_service(cfg)
        elif args.command == "uninstall-service":
            subprocess.run(
                ["launchctl", "bootout", f"gui/{os.getuid()}/{cfg['service_label']}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            (
                Path.home() / "Library/LaunchAgents" / (cfg["service_label"] + ".plist")
            ).unlink(missing_ok=True)
            print("后台服务已移除，配置和记录已保留。")
        elif args.command in ("pause", "resume"):
            settings = {k: cfg[k] for k in DEFAULTS}
            settings["enabled"] = args.command == "resume"
            atomic_json(Path(cfg["config_path"]), settings)
            print("语音已暂停。" if args.command == "pause" else "语音已恢复。")
        elif args.command == "status":
            path = Path(cfg["state_dir"]) / "health.json"
            health = (
                json.loads(path.read_text())
                if path.exists()
                else {"status": "not_started"}
            )
            health["fresh"] = time.time() - health.get("updated_at", 0) < 30
            try:
                pid = health.get("pid")
                if not isinstance(pid, int) or pid <= 0:
                    raise ProcessLookupError
                os.kill(pid, 0)
                alive = True
            except (ProcessLookupError, PermissionError):
                alive = False
            health["running"] = alive and health["fresh"]
            health["configured_enabled"] = cfg["enabled"]
            print_json(health)
        elif args.command == "events":
            store = Store(Path(cfg["state_dir"]) / "state.sqlite")
            print_json(store.recent(min(100, max(1, args.limit))))
            store.close()
    except Exception as exc:
        # Provider bodies and configuration values must never leak through the CLI.
        from .harness import HarnessFailure
        from .speech import TTSFailure

        if isinstance(exc, (HarnessFailure, TTSFailure)):
            print(str(exc), file=sys.stderr)
        else:
            print(
                f"操作失败：{type(exc).__name__}。请检查配置或运行 doctor。",
                file=sys.stderr,
            )
        return 1
    return 0
