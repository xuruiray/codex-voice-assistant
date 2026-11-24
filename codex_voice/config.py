from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_DIR = Path.home() / ".config/codex-voice"
STATE_DIR = Path.home() / "Library/Application Support/CodexVoice"
DEFAULTS = {
    "codex_home": str(Path.home() / ".codex"),
    "state_dir": str(STATE_DIR),
    "dsh_home": str(Path.home() / ".dsh"),
    "dsh_command": "/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh",
    "dsh_profile": "codex-voice",
    "deepseek_provider": "deepseek-account",
    "deepseek_model": "deepseek-flash",
    "poll_seconds": 3,
    "merge_seconds": 8,
    "reminder_seconds": 300,
    "max_age_seconds": 600,
    "model_timeout_seconds": 60,
    "tts_timeout_seconds": 25,
    "max_attempts": 3,
    "call_name": "你好",
    "service_label": "io.github.codex-voice-assistant",
    "speaker": "zh_female_vv_uranus_bigtts",
    "tts_resource_id": "seed-tts-2.0",
    "tts_endpoint": "https://openspeech.bytedance.com/api/v3/tts/unidirectional/sse",
    "volume": 0.7,
    "quiet_hours": [],
    "enabled": True,
    "daemon_socket": str(
        Path.home() / ".codex/app-server-control/app-server-control.sock"
    ),
    "daemon_enabled": True,
    "include_subagents": False,
}


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def atomic_json(path: Path, data: object) -> None:
    private_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load(path: Path | None = None) -> dict:
    path = path or CONFIG_DIR / "config.json"
    cfg = {**DEFAULTS, **(json.loads(path.read_text()) if path.exists() else {})}
    for key in (
        "poll_seconds",
        "reminder_seconds",
        "max_age_seconds",
        "model_timeout_seconds",
        "tts_timeout_seconds",
    ):
        if not isinstance(cfg[key], (float, int)) or cfg[key] <= 0:
            raise ValueError(f"{key} must be positive")
    if not 0 <= cfg["volume"] <= 1 or not 0 <= cfg["merge_seconds"] <= 60:
        raise ValueError("volume must be 0..1 and merge_seconds 0..60")
    if cfg["deepseek_provider"] not in ("deepseek-account", "deepseek-official"):
        raise ValueError("Unsupported DeepSeek provider")
    if not 1 <= cfg["max_attempts"] <= 10:
        raise ValueError("max_attempts must be 1..10")
    for window in cfg["quiet_hours"]:
        import datetime

        if len(window) != 2:
            raise ValueError("quiet_hours entries must be [HH:MM, HH:MM]")
        for value in window:
            datetime.time.fromisoformat(value)
    cfg["config_path"] = str(path)
    cfg["secrets_path"] = str(path.parent / "secrets.json")
    private_dir(Path(cfg["state_dir"]))
    return cfg


def secrets(cfg: dict) -> dict:
    path = Path(cfg["secrets_path"])
    result = json.loads(path.read_text()) if path.exists() else {}
    for key, env in (
        ("volc_api_key", "VOLC_TTS_API_KEY"),
        ("deepseek_api_key", "DEEPSEEK_API_KEY"),
    ):
        if os.environ.get(env):
            result[key] = os.environ[env]
    return result
