from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import tempfile
import time
import signal
from pathlib import Path


def install(cfg):
    """Build a native menu-bar carrier; the existing service owns all settings."""
    target = Path.home() / "Applications/Codex Voice.app"
    target.parent.mkdir(parents=True, exist_ok=True)
    label = cfg["service_label"] + ".menubar"
    agent = Path.home() / "Library/LaunchAgents" / (label + ".plist")
    with tempfile.TemporaryDirectory(
        prefix="voice-ui-", dir=cfg["state_dir"]
    ) as directory:
        bundle = Path(directory) / "Codex Voice.app"
        executable = bundle / "Contents/MacOS/CodexVoice"
        executable.parent.mkdir(parents=True)
        resources = bundle / "Contents/Resources"
        resources.mkdir()
        subprocess.run(
            [
                "/usr/bin/swiftc",
                "-O",
                str(Path(__file__).with_name("menubar.swift")),
                "-o",
                str(executable),
                "-framework",
                "AppKit",
                "-framework",
                "WebKit",
            ],
            check=True,
            timeout=180,
        )
        iconset = Path(directory) / "AppIcon.iconset"
        iconset.mkdir()
        original = Path(directory) / "icon.png"
        subprocess.run(
            [str(executable), "--render-icon", str(original)], check=True, timeout=10
        )
        for points in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                file = iconset / (
                    f"icon_{points}x{points}" + ("@2x" if scale == 2 else "") + ".png"
                )
                subprocess.run(
                    [
                        "sips",
                        "-z",
                        str(points * scale),
                        str(points * scale),
                        str(original),
                        "--out",
                        str(file),
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        subprocess.run(
            [
                "iconutil",
                "-c",
                "icns",
                str(iconset),
                "-o",
                str(resources / "AppIcon.icns"),
            ],
            check=True,
        )
        (bundle / "Contents/Info.plist").write_bytes(
            plistlib.dumps(
                {
                    "CFBundleName": "Codex Voice",
                    "CFBundleDisplayName": "Codex Voice",
                    "CFBundleIdentifier": "io.github.codex-voice-assistant.desktop",
                    "CFBundleExecutable": "CodexVoice",
                    "CFBundlePackageType": "APPL",
                    "CFBundleShortVersionString": "0.2.0",
                    "CFBundleIconFile": "AppIcon",
                    "LSUIElement": True,
                    "NSHighResolutionCapable": True,
                }
            )
        )
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # A manually opened earlier instance may not belong to launchd. Stop only
        # this app's exact executable before replacing its bundle.
        processes = subprocess.check_output(["ps", "-axo", "pid=,comm="], text=True)
        for line in processes.splitlines():
            parts = line.strip().split(maxsplit=1)
            if len(parts) == 2 and parts[1] == str(
                target / "Contents/MacOS/CodexVoice"
            ):
                try:
                    os.kill(int(parts[0]), signal.SIGTERM)
                except ProcessLookupError:
                    pass
        time.sleep(0.3)
        if target.exists():
            metadata = target / "Contents/Info.plist"
            if (
                not metadata.exists()
                or plistlib.loads(metadata.read_bytes()).get("CFBundleIdentifier")
                != "io.github.codex-voice-assistant.desktop"
            ):
                raise RuntimeError(
                    "Target app exists and does not belong to Codex Voice"
                )
            shutil.rmtree(target)
        shutil.copytree(bundle, target)
    agent.parent.mkdir(parents=True, exist_ok=True)
    agent.write_bytes(
        plistlib.dumps(
            {
                "Label": label,
                "ProgramArguments": [
                    str(target / "Contents/MacOS/CodexVoice"),
                    "--state-dir",
                    cfg["state_dir"],
                ],
                "RunAtLoad": True,
                "ProcessType": "Interactive",
                "StandardOutPath": str(Path(cfg["state_dir"]) / "menubar.log"),
                "StandardErrorPath": str(Path(cfg["state_dir"]) / "menubar.log"),
            }
        )
    )
    agent.chmod(0o600)
    deadline = time.monotonic() + 6
    while True:
        started = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(agent)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if started.returncode == 0:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("Menu-bar service could not start")
        time.sleep(0.2)
    subprocess.run(["open", "-a", str(target), "--args", "--show-settings"], check=True)
    return target


def open_settings(cfg):
    target = Path.home() / "Applications/Codex Voice.app"
    if target.exists():
        subprocess.run(
            ["open", "-a", str(target), "--args", "--show-settings"], check=True
        )
    else:
        import webbrowser

        endpoint = json.loads((Path(cfg["state_dir"]) / "ui.json").read_text())
        webbrowser.open(endpoint["origin"] + "/#token=" + endpoint["token"])
