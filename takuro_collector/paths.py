from __future__ import annotations

import os
from pathlib import Path

APP_DIR_NAME = "TAKURO Collector"


def data_root() -> Path:
    override = os.environ.get("TAKURO_COLLECTOR_HOME", "").strip()
    if override:
        root = Path(override).expanduser().resolve()
    elif os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
        root = Path(base) / APP_DIR_NAME
    else:
        root = Path.home() / ".takuro_collector"
    root.mkdir(parents=True, exist_ok=True)
    return root


def database_path() -> Path:
    return data_root() / "collector.db"


def log_dir() -> Path:
    p = data_root() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def browser_profile_dir(site_code: str) -> Path:
    p = data_root() / "browser_profiles" / site_code.lower()
    p.mkdir(parents=True, exist_ok=True)
    return p


def property_root() -> Path:
    if os.name == "nt":
        docs = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Documents"
        p = docs / APP_DIR_NAME / "properties"
    else:
        p = data_root() / "properties"
    p.mkdir(parents=True, exist_ok=True)
    return p


def export_root() -> Path:
    if os.name == "nt":
        docs = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Documents"
        p = docs / APP_DIR_NAME / "exports"
    else:
        p = data_root() / "exports"
    p.mkdir(parents=True, exist_ok=True)
    return p
