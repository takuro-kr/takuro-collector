from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _payload(prop: dict) -> dict[str, Any]:
    raw = prop.get("raw_payload") or {}
    if not isinstance(raw, dict):
        raw = {}
    info = raw.get("collected_info") or prop.get("collected_info") or {}
    return info if isinstance(info, dict) else {}


def build_collected_text(prop: dict) -> str:
    info = _payload(prop)
    lines = ["収集情報", "source_url", str(info.get("source_url") or prop.get("source_url") or "")]
    fields = info.get("table_fields") or {}
    if isinstance(fields, dict):
        for key, values in fields.items():
            vals = values if isinstance(values, list) else [values]
            for value in vals:
                lines.extend([str(key), str(value)])
    return "\n".join(lines).rstrip() + "\n"


def write_collected_files(prop: dict, folder: Path) -> tuple[Path, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    info = _payload(prop)
    txt = folder / "収集情報.txt"
    js = folder / "収集情報.json"
    txt.write_text(build_collected_text(prop), encoding="utf-8", newline="\n")
    js.write_text(json.dumps(info, ensure_ascii=False, indent=2, sort_keys=False) + "\n", encoding="utf-8", newline="\n")
    return txt, js
