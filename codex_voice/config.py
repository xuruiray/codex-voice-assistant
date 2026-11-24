from __future__ import annotations

import json
import os
import fcntl
import math
import re
import tempfile
import shutil
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
    "text_provider": "codex",
    "codex_command": shutil.which("codex") or "codex",
    "codex_login_home": "",
    "codex_model": "",
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
    "ui_port": 8793,
}


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def atomic_json(path: Path, data: object) -> None:
    private_dir(path.parent)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def load(path: Path | None = None) -> dict:
    path = path or CONFIG_DIR / "config.json"
    cfg = {**DEFAULTS, **(json.loads(path.read_text()) if path.exists() else {})}
    validate(cfg)
    cfg["config_path"] = str(path)
    cfg["secrets_path"] = str(path.parent / "secrets.json")
    private_dir(Path(cfg["state_dir"]))
    return cfg


def validate(cfg):
    bounds = {
        "poll_seconds": (0.1, 3600),
        "reminder_seconds": (1, 86400),
        "max_age_seconds": (1, 86400),
        "model_timeout_seconds": (1, 600),
        "tts_timeout_seconds": (1, 300),
        "volume": (0, 1),
        "merge_seconds": (0, 60),
        "max_attempts": (1, 10),
        "ui_port": (0, 65535),
    }
    for key, (minimum, maximum) in bounds.items():
        value = cfg[key]
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or not minimum <= value <= maximum
        ):
            raise ValueError(f"{key}: 请输入 {minimum} 到 {maximum} 之间的数字")
    for key in ("enabled", "daemon_enabled", "include_subagents"):
        if not isinstance(cfg[key], bool):
            raise ValueError(f"{key}: 需要开关值")
    if any(
        not isinstance(cfg[k], int) or isinstance(cfg[k], bool)
        for k in ("max_attempts", "ui_port")
    ):
        raise ValueError("重试次数和端口必须为整数")
    if (
        not isinstance(cfg["call_name"], str)
        or not 1 <= len(cfg["call_name"].strip()) <= 30
        or re.search(r"[<>\x00-\x1f]", cfg["call_name"])
    ):
        raise ValueError("称呼需要 1 到 30 个字符，不含换行或标记")
    for key in ("speaker", "tts_resource_id", "deepseek_model", "service_label"):
        if not isinstance(cfg[key], str) or not re.fullmatch(
            r"[\w./-]{1,120}", cfg[key]
        ):
            raise ValueError(f"{key}: 格式不正确")
    if "/" in cfg["service_label"]:
        raise ValueError("service_label: 不能包含路径")
    if cfg["deepseek_provider"] not in ("deepseek-account", "deepseek-official"):
        raise ValueError("Unsupported DeepSeek provider")
    if cfg["text_provider"] not in ("codex", "deepseek"):
        raise ValueError("Unsupported text provider")
    if not isinstance(cfg["codex_model"], str) or (
        cfg["codex_model"] and not re.fullmatch(r"[\w.-]{1,120}", cfg["codex_model"])
    ):
        raise ValueError("codex_model: 格式不正确")
    if not isinstance(cfg["codex_command"], str) or not cfg["codex_command"].strip():
        raise ValueError("codex_command: 需要本机 Codex 命令")
    if not isinstance(cfg["codex_login_home"], str):
        raise ValueError("codex_login_home: 需要本机目录")
    if not isinstance(cfg["quiet_hours"], list):
        raise ValueError("免打扰时段需要列表")
    for window in cfg["quiet_hours"]:
        import datetime

        if not isinstance(window, list) or len(window) != 2 or window[0] == window[1]:
            raise ValueError("quiet_hours entries must be [HH:MM, HH:MM]")
        for value in window:
            datetime.time.fromisoformat(value)


def update_settings(path: Path, changes: dict) -> dict:
    """One cross-process commit point for CLI, menu bar and web settings."""
    if not isinstance(changes, dict) or set(changes) - DEFAULTS.keys():
        raise ValueError("包含不支持的配置项")
    private_dir(path.parent)
    with (path.parent / ".config.lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing = json.loads(path.read_text()) if path.exists() else {}
        candidate = {**DEFAULTS, **existing, **changes}
        validate(candidate)
        atomic_json(path, candidate)
    return load(path)


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
