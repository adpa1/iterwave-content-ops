"""HOT-013: login child processes and export threads are drained before path changes."""
from __future__ import annotations

import json
import threading
from pathlib import Path

from src.core import account_login


class FakeProcess:
    def __init__(self, running=True):
        self.running = running
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self.running else 0

    def terminate(self):
        self.terminated = True
        self.running = False

    def wait(self, timeout=None):
        self.running = False
        return 0

    def kill(self):
        self.killed = True
        self.running = False


def test_hot_013_stop_login_processes_preserves_exported_storage_state(monkeypatch, tmp_path: Path):
    active = FakeProcess(running=True)
    completed = FakeProcess(running=False)
    processes = {"xhs": active, "douyin": completed}
    monkeypatch.setattr(account_login, "_LOGIN_PROCESSES", processes)
    monkeypatch.setattr(account_login, "_PROCESS_LOCK", threading.Lock())
    monkeypatch.setattr(account_login, "_EXPORT_LOCK", threading.Lock())
    monkeypatch.setattr(account_login, "_EXPORTING", set())

    paths = {}
    for platform in ("xhs", "douyin"):
        folder = tmp_path / platform
        folder.mkdir()
        items = {
            "qr": folder / "qr.png",
            "status": folder / "status.json",
            "log": folder / "login.log",
            "code": folder / "code.txt",
            "storage": folder / "storage.json",
        }
        items["qr"].write_text("qr", encoding="utf-8")
        items["status"].write_text(json.dumps({"state": "success"}), encoding="utf-8")
        items["code"].write_text("1234", encoding="utf-8")
        items["storage"].write_text("persisted", encoding="utf-8")
        paths[platform] = items

    monkeypatch.setattr(account_login, "_files", lambda platform: paths[platform])
    exported = []
    monkeypatch.setattr(account_login, "_schedule_export", lambda platform: exported.append(platform))

    account_login.stop_all_logins(timeout_seconds=1)

    assert active.terminated
    assert not active.killed
    assert processes == {}
    assert exported == ["douyin"]
    for platform in ("xhs", "douyin"):
        assert not paths[platform]["qr"].exists()
        assert not paths[platform]["status"].exists()
        assert not paths[platform]["code"].exists()
        assert paths[platform]["storage"].read_text(encoding="utf-8") == "persisted"
