"""Authenticated, loopback-only settings surface for the existing service."""
from __future__ import annotations

import datetime as dt
import hmac
import json
import secrets as random_secrets
import sqlite3
import threading
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .config import atomic_json, load, secrets, update_settings
from .store import Store
from .text import redact

EDITABLE = frozenset(
    {
        "call_name",
        "speaker",
        "tts_resource_id",
        "volume",
        "enabled",
        "poll_seconds",
        "merge_seconds",
        "reminder_seconds",
        "max_age_seconds",
        "max_attempts",
        "quiet_hours",
        "deepseek_provider",
        "deepseek_model",
        "text_provider",
        "codex_model",
        "include_subagents",
    }
)


class UI:
    def __init__(self, cfg):
        self.config_path = Path(cfg["config_path"])
        self.state = Path(cfg["state_dir"])
        self.token = random_secrets.token_urlsafe(32)
        self.static = Path(__file__).with_name("static")
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "CodexVoice"

            def log_message(self, *_):
                pass  # Neither access URLs nor authorization data belong in logs.

            def respond(
                self, status, data, content_type="application/json; charset=utf-8"
            ):
                body = (
                    data
                    if isinstance(data, bytes)
                    else json.dumps(data, ensure_ascii=False).encode()
                )
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
                )
                self.end_headers()
                self.wfile.write(body)

            def allowed(self, auth=True):
                if self.headers.get("Host") != owner.authority:
                    self.respond(403, {"error": "请求来源不正确"})
                    return False
                origin = self.headers.get("Origin")
                if origin and origin != owner.origin:
                    self.respond(403, {"error": "请求来源不正确"})
                    return False
                if self.headers.get("Sec-Fetch-Site") == "cross-site":
                    self.respond(403, {"error": "请从菜单栏打开设置"})
                    return False
                expected = "Bearer " + owner.token
                if auth and not hmac.compare_digest(
                    self.headers.get("Authorization", ""), expected
                ):
                    self.respond(401, {"error": "连接已更新，请从菜单栏重新打开设置"})
                    return False
                return True

            def do_GET(self):
                path = urlsplit(self.path).path
                if not self.allowed(auth=path.startswith("/api/")):
                    return
                try:
                    if path == "/api/config":
                        self.respond(200, owner.settings())
                    elif path == "/api/status":
                        self.respond(200, owner.status())
                    elif path == "/api/events":
                        self.respond(200, {"events": owner.events()})
                    elif path in ("/", "/app.js", "/style.css"):
                        name = {
                            "/": "index.html",
                            "/app.js": "app.js",
                            "/style.css": "style.css",
                        }[path]
                        mime = {
                            "index.html": "text/html",
                            "app.js": "text/javascript",
                            "style.css": "text/css",
                        }[name]
                        self.respond(
                            200,
                            (owner.static / name).read_bytes(),
                            mime + "; charset=utf-8",
                        )
                    elif path.startswith("/assets/"):
                        asset = (owner.static / path.lstrip("/")).resolve()
                        if (
                            not asset.is_relative_to(
                                (owner.static / "assets").resolve()
                            )
                            or not asset.is_file()
                            or asset.suffix not in (".png", ".svg", ".woff2")
                        ):
                            self.respond(404, {"error": "资源不存在"})
                        else:
                            mime = {
                                ".png": "image/png",
                                ".svg": "image/svg+xml",
                                ".woff2": "font/woff2",
                            }[asset.suffix]
                            self.respond(200, asset.read_bytes(), mime)
                    else:
                        self.respond(404, {"error": "页面不存在"})
                except (OSError, sqlite3.Error, ValueError):
                    self.respond(503, {"error": "本机服务暂时不可用，请稍后重试"})

            def do_POST(self):
                if not self.allowed():
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if (
                        not 0 < length <= 16384
                        or self.headers.get("Content-Type", "").split(";")[0]
                        != "application/json"
                    ):
                        raise ValueError("请求格式不正确")
                    data = json.loads(self.rfile.read(length))
                    if not isinstance(data, dict):
                        raise ValueError("请求格式不正确")
                    path = urlsplit(self.path).path
                    if path == "/api/config":
                        if set(data) - EDITABLE:
                            raise ValueError("包含不可修改的配置项")
                        update_settings(owner.config_path, data)
                        self.respond(200, owner.settings())
                    elif path == "/api/test":
                        cfg = load(owner.config_path)
                        if not secrets(cfg).get("volc_api_key"):
                            raise ValueError("请先配置火山语音凭据")
                        store = Store(owner.state / "state.sqlite")
                        try:
                            self.respond(
                                202,
                                {
                                    "event_id": store.queue_test(),
                                    "message": "已加入试听队列",
                                },
                            )
                        finally:
                            store.close()
                    elif path == "/api/credentials":
                        if not data or set(data) - {"volc_api_key", "deepseek_api_key"}:
                            raise ValueError("不支持的凭据类型")
                        if any(
                            not isinstance(v, str) or not 8 <= len(v.strip()) <= 4096
                            for v in data.values()
                        ):
                            raise ValueError("请输入有效的 API Key")
                        cfg = load(owner.config_path)
                        target = Path(cfg["secrets_path"])
                        # Credential writes serialize separately from ordinary preferences.
                        import fcntl

                        with (target.parent / ".credentials.lock").open("a") as lock:
                            Path(lock.name).chmod(0o600)
                            fcntl.flock(lock, fcntl.LOCK_EX)
                            old = (
                                json.loads(target.read_text())
                                if target.exists()
                                else {}
                            )
                            atomic_json(
                                target,
                                {**old, **{k: v.strip() for k, v in data.items()}},
                            )
                        self.respond(200, {"saved": True})
                    else:
                        self.respond(404, {"error": "操作不存在"})
                except (ValueError, TypeError) as exc:
                    self.respond(
                        400,
                        {
                            "error": str(exc)
                            if isinstance(exc, ValueError)
                            and not isinstance(exc, json.JSONDecodeError)
                            else "配置格式不正确"
                        },
                    )
                except (OSError, sqlite3.Error):
                    self.respond(503, {"error": "保存失败，请稍后重试"})

        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", cfg["ui_port"]), Handler)
        except OSError:
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.authority = f"127.0.0.1:{self.server.server_port}"
        self.origin = "http://" + self.authority
        self.thread = threading.Thread(
            target=self.server.serve_forever, name="settings-ui", daemon=True
        )

    def start(self):
        atomic_json(
            self.state / "ui.json", {"origin": self.origin, "token": self.token}
        )
        self.thread.start()
        return self

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        endpoint = self.state / "ui.json"
        try:
            if json.loads(endpoint.read_text()).get("token") == self.token:
                endpoint.unlink()
        except (OSError, ValueError):
            pass

    def settings(self):
        cfg = load(self.config_path)
        credential = secrets(cfg)
        return {
            "settings": {key: cfg[key] for key in EDITABLE},
            "credentials": {
                "volc": bool(credential.get("volc_api_key")),
                "deepseek": bool(credential.get("deepseek_api_key")),
            },
            "saved_at": self.config_path.stat().st_mtime
            if self.config_path.exists()
            else None,
        }

    def database(self):
        db = sqlite3.connect(
            f"file:{self.state / 'state.sqlite'}?mode=ro", uri=True, timeout=2
        )
        db.row_factory = sqlite3.Row
        return db

    def status(self):
        cfg = load(self.config_path)
        path = self.state / "health.json"
        health = json.loads(path.read_text()) if path.exists() else {}
        now = dt.datetime.now().astimezone()
        fresh = now.timestamp() - health.get("updated_at", 0) < 30
        today = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        stats = {"today_played": 0, "queued": 0, "total_played": 0}
        try:
            with self.database() as db:
                stats["today_played"] = db.execute(
                    "SELECT count(*) FROM events WHERE status='played' AND kind!='test' AND played_at>=?",
                    (today,),
                ).fetchone()[0]
                stats["queued"] = db.execute(
                    "SELECT count(*) FROM events WHERE status IN ('pending','preparing','synthesizing','playing')"
                ).fetchone()[0]
                stats["total_played"] = db.execute(
                    "SELECT count(*) FROM events WHERE status='played' AND kind!='test'"
                ).fetchone()[0]
        except sqlite3.Error:
            pass
        return {
            "running": fresh,
            "enabled": cfg["enabled"],
            "updated_at": health.get("updated_at"),
            "stage": health.get("delivery_stage"),
            "observed_sessions": health.get("rollout_files", 0),
            "poll_seconds": cfg["poll_seconds"],
            "stats": stats,
            "coverage": {
                "completion": True,
                "questions": True,
                "desktop_approval": False,
            },
        }

    def events(self):
        with self.database() as db:
            rows = db.execute(
                "SELECT id,kind,status,body,speech,error,created,played_at FROM events ORDER BY created DESC LIMIT 80"
            ).fetchall()
        result = []
        for row in rows:
            body = json.loads(row["body"])
            title = redact(body.get("task_title", "Codex 任务"), 100)
            if title.startswith("# Files mentioned by the user:"):
                title = "Codex 任务"
            else:
                title = re.sub(r"\[\[链接\]\]\(\[链接\]\)", "[链接]", title).strip()
            result.append(
                {
                    "id": row["id"],
                    "kind": row["kind"],
                    "status": row["status"],
                    "title": title,
                    "speech": redact(row["speech"] or "", 300),
                    "error": redact(row["error"] or "", 180),
                    "created": row["created"],
                    "played_at": row["played_at"],
                }
            )
        return result
