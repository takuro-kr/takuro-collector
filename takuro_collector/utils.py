from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ALLOWED_PREFECTURES = ("東京都", "千葉県", "埼玉県", "神奈川県")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def normalize_room(value: str) -> str:
    value = unicodedata.normalize("NFKC", (value or "").strip())
    value = re.sub(r"(?:号室|호실|호)$", "", value).strip()
    if re.fullmatch(r"\d+", value):
        value = value.lstrip("0") or "0"
    return value


def extract_prefecture(address: str, explicit: str = "") -> str:
    if explicit in ALLOWED_PREFECTURES:
        return explicit
    for pref in ALLOWED_PREFECTURES:
        if pref in (address or ""):
            return pref
    return ""


def display_address_to_chome(address: str) -> str:
    address = (address or "").strip()
    m = re.match(r"^(.+?\d+丁目)", unicodedata.normalize("NFKC", address))
    return m.group(1) if m else address


def parse_yen(value: Any) -> int:
    if value is None:
        return 0
    s = unicodedata.normalize("NFKC", str(value)).strip()
    if not s or s in {"なし", "無し", "無", "不要", "-", "—"}:
        return 0
    s = s.replace(",", "").replace("￥", "").replace("¥", "").replace("円", "").replace(" ", "")
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)万", s)
    if m:
        return int(round(float(m.group(1)) * 10000))
    m = re.search(r"([0-9]+)", s)
    return int(m.group(1)) if m else 0


def parse_area(value: Any) -> float | None:
    if value is None:
        return None
    s = unicodedata.normalize("NFKC", str(value)).replace(",", "")
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(?:㎡|m2|m²)", s, re.I)
    if not m:
        m = re.search(r"([0-9]+(?:\.[0-9]+)?)", s)
    return float(m.group(1)) if m else None


def canonical_host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower().strip(".")
    return host[4:] if host.startswith("www.") else host


def source_id_fallback(url: str) -> str:
    digest = hashlib.sha1(url.encode("utf-8", "ignore")).hexdigest()[:20]
    return f"urlhash-{digest}"


def safe_filename(value: str, max_len: int = 90) -> str:
    value = unicodedata.normalize("NFKC", (value or "").strip())
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = "property"
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if value.upper() in reserved:
        value = "_" + value
    return value[:max_len].rstrip(" .") or "property"


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def open_path(path: str | Path) -> None:
    p = str(Path(path))
    if os.name == "nt":
        os.startfile(p)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p])


def open_url(url: str) -> None:
    import webbrowser
    webbrowser.open(url)
