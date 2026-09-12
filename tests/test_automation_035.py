from __future__ import annotations

import os
import sys

from takuro_collector import autostart


def test_startup_command_quotes_executable(monkeypatch, tmp_path):
    exe = tmp_path / "Program Files" / "TAKURO Collector.exe"
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert autostart.startup_command() == f'"{os.path.abspath(exe)}" --autostart'


def test_source_startup_command_points_to_main(monkeypatch, tmp_path):
    python = tmp_path / "Python" / "python.exe"
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.delattr(sys, "frozen", raising=False)
    command = autostart.startup_command()
    assert command.startswith(f'"{os.path.abspath(python)}" ')
    assert command.endswith('" --autostart')
    assert "main.py" in command
