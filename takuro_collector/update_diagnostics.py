from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .paths import data_root

_active_log: Path | None = None
_SECRET_KEYS = ("token", "password", "cookie", "credential", "authorization", "private_key", "api_key")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _redact(value: object) -> str:
    text = str(value or "")
    text = re.sub(r"(?i)(authorization|password|token|cookie|api[_-]?key)\s*[:=]\s*[^\s,;]+", r"\1=[REDACTED]", text)
    return re.sub(r"(https?://)[^/@\s]+@", r"\1[REDACTED]@", text)


def logs_dir() -> Path:
    path = data_root() / "updates" / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def retain_logs(*, keep: int = 10, current: Path | None = None) -> None:
    files = sorted(logs_dir().glob("update-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    protected = current.resolve() if current else None
    kept = 0
    for path in files:
        if protected and path.resolve() == protected:
            continue
        kept += 1
        if kept >= keep:
            path.unlink(missing_ok=True)


def begin_log(from_version: str = "", to_version: str = "") -> Path:
    global _active_log
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    _active_log = logs_dir() / f"update-{stamp}.log"
    _active_log.touch()
    retain_logs(current=_active_log)
    log_phase("start", from_version=from_version, to_version=to_version)
    return _active_log


def use_log(path: str | Path | None) -> Path:
    global _active_log
    _active_log = Path(path).resolve() if path else begin_log()
    _active_log.parent.mkdir(parents=True, exist_ok=True)
    return _active_log


def active_log() -> Path:
    return _active_log or begin_log()


def log_phase(phase: str, message: str = "", **context: object) -> None:
    safe = []
    for key, value in context.items():
        safe.append(f"{key}={'[REDACTED]' if any(secret in key.lower() for secret in _SECRET_KEYS) else _redact(value)}")
    line = f"{_now()} phase={phase}"
    if message:
        line += f" message={_redact(message)}"
    if safe:
        line += " " + " ".join(safe)
    with active_log().open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")


def write_state(status: str, phase: str, *, from_version: str = "", to_version: str = "",
                error: BaseException | str | None = None, shown: bool = False,
                root: Path | None = None, **context: object) -> dict:
    exc = error if isinstance(error, BaseException) else None
    message = _redact(error or "")
    payload = {
        "state": status if status in {"success", "failed", "rolled_back"} else phase,
        "status": status,
        "phase": phase,
        "from_version": from_version,
        "to_version": to_version,
        "error_type": type(exc).__name__ if exc else ("" if not error else "Error"),
        "error_message": message,
        "error": message,
        "winerror": getattr(exc, "winerror", None),
        "errno": getattr(exc, "errno", None),
        "log_path": str(active_log()),
        "timestamp": _now(),
        "shown": bool(shown),
    }
    payload.update({key: "[REDACTED]" if any(secret in key.lower() for secret in _SECRET_KEYS) else _redact(value)
                    for key, value in context.items()})
    update_root = (root or data_root()) / "updates"
    update_root.mkdir(parents=True, exist_ok=True)
    temporary = update_root / "state.json.tmp"
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(update_root / "state.json")
    return payload


def read_state() -> dict:
    try:
        value = json.loads((data_root() / "updates" / "state.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def mark_shown() -> None:
    path = data_root() / "updates" / "state.json"
    state = read_state()
    if not state:
        return
    state["shown"] = True
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
