import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import takuro_collector.ui as ui_module
import takuro_collector.update_diagnostics as diagnostics
from takuro_collector import updater, updater_helper
from takuro_collector.ui import MainWindow


class Process:
    def __init__(self, marker=None, token="", *, healthy=True):
        self.marker, self.token, self.healthy = marker, token, healthy

    def poll(self):
        if self.healthy and self.marker and not self.marker.exists():
            self.marker.write_text(self.token, encoding="utf-8")
        return None if self.healthy else 1

    def terminate(self):
        pass


def command_fixture(tmp_path):
    install, staged, user = tmp_path / "portable path" / "TAKURO Collector", tmp_path / "stage", tmp_path / "user"
    for root, value in ((install, b"old"), (staged, b"new")):
        (root / "_internal").mkdir(parents=True)
        (root / "TAKURO Collector.exe").write_bytes(value)
    user.mkdir()
    marker = user / "health.txt"
    log = user / "updates" / "logs" / "update-test.log"
    return {
        "pid": 123,
        "install_dir": str(install),
        "staged_dir": str(staged),
        "data_root": str(user),
        "marker": str(marker),
        "token": "health-secret-token",
        "executable": "TAKURO Collector.exe",
        "version": "0.4.11",
        "from_version": "0.4.10",
        "log_path": str(log),
    }, install, user, marker, log


def state(user):
    return json.loads((user / "updates" / "state.json").read_text(encoding="utf-8"))


def test_success_writes_complete_state_and_all_helper_phases(tmp_path):
    command, install, user, marker, log = command_fixture(tmp_path)
    result = updater_helper.install(
        command,
        launch=lambda *_a, **_k: Process(marker, command["token"]),
        wait_exit=lambda *_a: True,
        timeout=.2,
    )
    payload, text = state(user), log.read_text(encoding="utf-8")
    assert result == "launch_verified"
    assert payload["status"] == "success" and payload["phase"] == "complete"
    assert payload["from_version"] == "0.4.10" and payload["to_version"] == "0.4.11"
    for phase in ("parent_wait", "prepared_copy", "known_good_backup", "install_replace", "relaunch", "health_marker", "complete"):
        assert f"phase={phase}" in text
    assert (install / "TAKURO Collector.exe").read_bytes() == b"new"


def test_parent_wait_failure_records_phase_and_error(tmp_path):
    command, _install, user, _marker, log = command_fixture(tmp_path)
    with pytest.raises(RuntimeError, match="종료되지"):
        updater_helper.install(command, wait_exit=lambda *_a: False, timeout=.01)
    payload = state(user)
    assert payload["status"] == "failed" and payload["phase"] == "parent_wait"
    assert "종료되지" in payload["error_message"]
    assert "phase=failed" in log.read_text(encoding="utf-8")


def test_backup_failure_records_exact_phase_and_winerror(tmp_path, monkeypatch):
    command, install, user, marker, _log = command_fixture(tmp_path)
    original = Path.replace
    prepared = install.parent / ".TAKURO Collector.update-new"

    def fail_selected(path, destination):
        if path == install:
            error = OSError("simulated Windows failure")
            error.winerror = 32
            raise error
        return original(path, destination)

    monkeypatch.setattr(Path, "replace", fail_selected)
    with pytest.raises(OSError):
        updater_helper.install(command, launch=lambda *_a, **_k: Process(marker, command["token"]),
                               wait_exit=lambda *_a: True, timeout=.1)
    payload = state(user)
    assert payload["status"] == "failed" and payload["phase"] == "known_good_backup"
    assert payload["winerror"] == 32


def test_replace_failure_reaches_successful_rollback(tmp_path, monkeypatch):
    command, install, user, _marker, log = command_fixture(tmp_path)
    original = Path.replace
    prepared = install.parent / ".TAKURO Collector.update-new"

    def fail_replace(path, destination):
        if path == prepared:
            error = OSError("replace failed")
            error.winerror = 5
            raise error
        return original(path, destination)

    monkeypatch.setattr(Path, "replace", fail_replace)
    result = updater_helper.install(command, launch=lambda *_a, **_k: Process(healthy=False),
                                    wait_exit=lambda *_a: True, timeout=.01)
    assert result == "rolled_back"
    assert state(user)["status"] == "rolled_back"
    assert "replace failed" in log.read_text(encoding="utf-8")


def test_health_failure_rolls_back_and_records_result(tmp_path):
    command, install, user, _marker, log = command_fixture(tmp_path)
    result = updater_helper.install(command, launch=lambda *_a, **_k: Process(healthy=False),
                                    wait_exit=lambda *_a: True, timeout=.01)
    assert result == "rolled_back"
    assert (install / "TAKURO Collector.exe").read_bytes() == b"old"
    assert state(user)["status"] == "rolled_back"
    assert "phase=rollback_complete" in log.read_text(encoding="utf-8")


def test_rollback_failure_is_distinct_failed_state(tmp_path, monkeypatch):
    command, install, user, _marker, _log = command_fixture(tmp_path)
    original = Path.replace
    backup = install.parent / ".TAKURO Collector.known-good"

    def fail_restore(path, destination):
        if path == backup:
            raise PermissionError("rollback blocked")
        return original(path, destination)

    monkeypatch.setattr(Path, "replace", fail_restore)
    with pytest.raises(PermissionError, match="rollback blocked"):
        updater_helper.install(command, launch=lambda *_a, **_k: Process(healthy=False),
                               wait_exit=lambda *_a: True, timeout=.01)
    payload = state(user)
    assert payload["status"] == "failed" and payload["phase"] == "rollback"


def test_log_redacts_secrets_and_retains_latest_ten(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "data_root", lambda: tmp_path)
    for index in range(12):
        path = diagnostics.logs_dir() / f"update-20260101-0000{index:02d}.log"
        path.write_text("old", encoding="utf-8")
        os.utime(path, (index + 1, index + 1))
    current = diagnostics.begin_log("0.4.10", "0.4.11")
    diagnostics.log_phase("test", token="raw-token", message="password=secret-value cookie=session-value")
    text = current.read_text(encoding="utf-8")
    assert "raw-token" not in text and "secret-value" not in text and "session-value" not in text
    assert len(list(diagnostics.logs_dir().glob("update-*.log"))) == 10


def test_failed_update_dialog_opens_log_once(tmp_path, monkeypatch):
    log = tmp_path / "update.log"
    log.write_text("details", encoding="utf-8")
    state_value = {"status": "failed", "phase": "known_good_backup", "error_message": "rename failed",
                   "winerror": 17, "log_path": str(log), "shown": False}
    shown, opened, messages = [], [], []
    monkeypatch.setattr(diagnostics, "read_state", lambda: state_value)
    monkeypatch.setattr(diagnostics, "mark_shown", lambda: shown.append(True))
    monkeypatch.setattr(ui_module, "open_path", lambda path: opened.append(Path(path)))

    class Box:
        Warning, ActionRole, Ok = 1, 2, 3
        def __init__(self, _parent): self.log_button = None
        def setIcon(self, _value): pass
        def setWindowTitle(self, value): messages.append(value)
        def setText(self, value): messages.append(value)
        def addButton(self, value, *_role):
            if value == "로그 열기": self.log_button = object(); return self.log_button
            return object()
        def exec(self): pass
        def clickedButton(self): return self.log_button

    monkeypatch.setattr(ui_module, "QMessageBox", Box)
    harness = SimpleNamespace(append_log=lambda value: messages.append(value))
    MainWindow.show_last_update_result(harness)
    assert shown == [True] and opened == [log]
    assert any("known_good_backup" in value and "WinError 17" in value for value in messages)
    state_value["shown"] = True
    MainWindow.show_last_update_result(harness)
    assert shown == [True]


def test_helper_launch_failure_is_persisted(tmp_path, monkeypatch):
    install = tmp_path / "install"
    install.mkdir()
    executable = install / "TAKURO Collector.exe"
    executable.write_bytes(b"collector")
    (install / "TAKURO Updater.exe").write_bytes(b"helper")
    staged = tmp_path / "stage"
    staged.mkdir()
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater.sys, "executable", str(executable))
    monkeypatch.setattr(updater, "data_root", lambda: tmp_path / "user")
    monkeypatch.setattr(diagnostics, "data_root", lambda: tmp_path / "user")
    diagnostics.begin_log("0.4.10", "0.4.11")
    monkeypatch.setattr(updater.subprocess, "Popen", lambda *_a, **_k: (_ for _ in ()).throw(OSError("launch blocked")))
    with pytest.raises(OSError, match="launch blocked"):
        updater.launch_helper(updater.UpdateInfo("0.4.11", "https://updates.takuro.tech/app.zip", "a" * 64, 1), staged)
    payload = json.loads((tmp_path / "user" / "updates" / "state.json").read_text(encoding="utf-8"))
    assert payload["status"] == "failed" and payload["phase"] == "helper_launch"
