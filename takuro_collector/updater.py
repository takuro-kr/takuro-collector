from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests

from .paths import data_root


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    url: str
    sha256: str
    notes: str = ""


def _version(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"\d+(?:\.\d+){1,3}", value or ""):
        raise UpdateError("업데이트 버전 형식이 올바르지 않습니다.")
    return tuple(int(part) for part in value.split("."))


def _require_https(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise UpdateError("업데이트 주소는 HTTPS만 사용할 수 있습니다.")


def check(manifest_url: str, current_version: str, *, timeout: int = 15) -> UpdateInfo | None:
    _require_https(manifest_url)
    response = requests.get(manifest_url, timeout=timeout, headers={"Accept": "application/json"})
    if not response.ok:
        raise UpdateError(f"업데이트 확인 실패: HTTP {response.status_code}")
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise UpdateError("업데이트 정보가 올바른 JSON이 아닙니다.") from exc
    info = UpdateInfo(
        version=str(payload.get("version") or ""),
        url=str(payload.get("url") or ""),
        sha256=str(payload.get("sha256") or "").lower(),
        notes=str(payload.get("notes") or ""),
    )
    _version(info.version)
    _require_https(info.url)
    if not re.fullmatch(r"[0-9a-f]{64}", info.sha256):
        raise UpdateError("업데이트 SHA-256 값이 올바르지 않습니다.")
    return info if _version(info.version) > _version(current_version) else None


def download(info: UpdateInfo, *, timeout: int = 120) -> Path:
    target_dir = data_root() / "updates" / info.version
    target_dir.mkdir(parents=True, exist_ok=True)
    final_path = target_dir / f"TAKURO-Collector-{info.version}-Portable-Windows.zip"
    fd, temp_name = tempfile.mkstemp(prefix="takuro-update-", suffix=".zip", dir=target_dir)
    os.close(fd)
    temp_path = Path(temp_name)
    digest = hashlib.sha256()
    try:
        with requests.get(info.url, timeout=timeout, stream=True) as response:
            if not response.ok:
                raise UpdateError(f"업데이트 다운로드 실패: HTTP {response.status_code}")
            with temp_path.open("wb") as output:
                for chunk in response.iter_content(1024 * 1024):
                    if chunk:
                        digest.update(chunk)
                        output.write(chunk)
        if digest.hexdigest().lower() != info.sha256:
            raise UpdateError("업데이트 파일의 SHA-256이 일치하지 않습니다.")
        temp_path.replace(final_path)
        return final_path
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise

