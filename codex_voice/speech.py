from __future__ import annotations

import base64
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .config import secrets
from .text import validate_speech


class TTSFailure(RuntimeError):
    pass


class VolcTTS:
    def __init__(self, cfg):
        self.cfg = cfg

    def synthesize(self, text: str, target: Path):
        text = validate_speech(text)
        credential = secrets(self.cfg).get("volc_api_key")
        if not credential:
            raise TTSFailure("火山 TTS 凭据尚未配置")
        endpoint = self.cfg["tts_endpoint"]
        url = urllib.parse.urlsplit(endpoint)
        if (
            url.scheme != "https"
            or url.hostname != "openspeech.bytedance.com"
            or url.username
            or url.password
        ):
            raise TTSFailure("TTS endpoint must be the official HTTPS speech endpoint")
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(
                {
                    "user": {"uid": "codex-voice-local"},
                    "req_params": {
                        "text": text,
                        "speaker": self.cfg["speaker"],
                        "audio_params": {"format": "mp3", "sample_rate": 24000},
                    },
                },
                ensure_ascii=False,
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Api-Key": credential,
                "X-Api-Resource-Id": self.cfg["tts_resource_id"],
                "X-Api-Request-Id": str(uuid.uuid4()),
            },
        )
        parts, completed = [], False
        deadline = time.monotonic() + self.cfg["tts_timeout_seconds"]
        try:
            with urllib.request.urlopen(
                request, timeout=self.cfg["tts_timeout_seconds"]
            ) as response:
                if "text/event-stream" not in response.headers.get("Content-Type", ""):
                    raise TTSFailure("TTS returned a non-SSE response")
                size = 0
                for line in response:
                    if time.monotonic() > deadline:
                        raise TTSFailure("TTS response deadline exceeded")
                    if len(line) > 4 * 1024 * 1024:
                        raise TTSFailure("TTS response record too large")
                    if not line.startswith(b"data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == b"[DONE]":
                        completed = True
                        break
                    data = json.loads(raw)
                    code = data.get("code", 0)
                    if code not in (0, 20000000):
                        raise TTSFailure(
                            f"TTS service rejected synthesis (code {code})"
                        )
                    encoded = data.get("data")
                    if encoded:
                        if not isinstance(encoded, str):
                            raise TTSFailure("TTS audio data has an unexpected type")
                        chunk = base64.b64decode(encoded, validate=True)
                        parts.append(chunk)
                        size += len(chunk)
                        if size > 8 * 1024 * 1024:
                            raise TTSFailure(
                                "TTS audio exceeds the short-notification limit"
                            )
                    if code == 20000000:
                        completed = True
                        break
            if not completed or not parts:
                raise TTSFailure("TTS stream ended without a complete audio result")
        except urllib.error.HTTPError as exc:
            # Never retain response bodies, URLs with credentials or request headers.
            raise TTSFailure(
                f"TTS HTTP {exc.code}; check API key, resource and speaker permissions"
            ) from None
        except (ValueError, OSError) as exc:
            raise TTSFailure(
                f"TTS transport/decoding failed ({type(exc).__name__})"
            ) from None
        fd = os.open(
            target.with_suffix(".tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        with os.fdopen(fd, "wb") as f:
            for chunk in parts:
                f.write(chunk)
            f.flush()
            os.fsync(f.fileno())
        os.replace(target.with_suffix(".tmp"), target)
        return target

    def play(self, path):
        subprocess.run(
            ["/usr/bin/afplay", "-v", str(self.cfg["volume"]), str(path)],
            check=True,
            timeout=40,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def fallback_text(events, call_name):
    if len(events) > 1:
        return f"{call_name}，有{len(events)}个 Codex 任务有新结果或需要你处理，请回来查看。"
    body = json.loads(events[0]["body"])
    from .text import redact

    title = redact(body["task_title"], 25)
    action = {
        "test": "语音提醒已准备好，这是试听消息",
        "waiting_on_user": "需要你回答一个问题",
        "waiting_on_approval": "有操作需要你批准",
        "failed": "本轮执行失败，请查看原因",
        "completed": "本轮执行结束，请查看结果",
    }[body["event_type"]]
    return f"{call_name}，{title}，{action}。"
