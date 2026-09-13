from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import requests

from .paths import data_root
from .update_diagnostics import active_log, begin_log, log_phase, write_state

SCHEMA_VERSION = 1
MAX_PACKAGE_SIZE = 350 * 1024 * 1024
EXECUTABLE_NAME = "TAKURO Collector.exe"
UPDATE_HOST = "updates.takuro.tech"
UPDATE_MANIFEST_URL = f"https://{UPDATE_HOST}/latest.json"
ALLOWED_UPDATE_HOSTS = frozenset({UPDATE_HOST})
PUBLIC_UPDATE_KEY_B64 = "7DUSa7wC7c3t7Sba+Ab1iZdOiMShDbvQyumRG70PH5g="


class UpdateError(RuntimeError):
    pass


def _write_state(value: str, error: str = "") -> None:
    write_state("failed" if value == "failed" else "in_progress", value, error=error or None)


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    url: str
    sha256: str
    size: int
    notes: tuple[str, ...] = ()
    signature: str = ""
    schema_version: int = SCHEMA_VERSION


def _version(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"\d+(?:\.\d+){1,3}", value or ""):
        raise UpdateError("업데이트 버전 형식이 올바르지 않습니다.")
    return tuple(int(part) for part in value.split("."))


def _https_host(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise UpdateError("업데이트 주소는 인증정보가 없는 HTTPS 주소여야 합니다.")
    return parsed.hostname.lower()


def _signed_payload(payload: dict) -> bytes:
    signed = {key: value for key, value in payload.items() if key != "signature"}
    return json.dumps(signed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def verify_signature(payload: dict, public_key_b64: str) -> None:
    if not public_key_b64:
        raise UpdateError("업데이트 서명 공개키가 설정되지 않았습니다.")
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64, validate=True))
        key.verify(base64.b64decode(str(payload.get("signature") or ""), validate=True), _signed_payload(payload))
    except Exception as exc:
        raise UpdateError("업데이트 정보의 Ed25519 서명이 올바르지 않습니다.") from exc


def check(manifest_url: str, current_version: str, *, timeout: int = 15,
          allowed_hosts: set[str] | frozenset[str] = ALLOWED_UPDATE_HOSTS,
          public_key_b64: str = PUBLIC_UPDATE_KEY_B64,
          require_signature: bool = True) -> UpdateInfo | None:
    begin_log(current_version)
    log_phase("manifest_check", manifest_url=manifest_url, from_version=current_version)
    try:
        return _check_manifest(manifest_url, current_version, timeout, allowed_hosts, public_key_b64, require_signature)
    except Exception as exc:
        log_phase("manifest_failed", str(exc), error_type=type(exc).__name__, winerror=getattr(exc, "winerror", None))
        write_state("failed", "manifest_check", from_version=current_version, error=exc)
        raise


def _check_manifest(manifest_url: str, current_version: str, timeout: int,
                    allowed_hosts: set[str] | frozenset[str], public_key_b64: str,
                    require_signature: bool) -> UpdateInfo | None:
    manifest_host = _https_host(manifest_url)
    hosts = {host.lower() for host in allowed_hosts}
    if manifest_host not in hosts:
        raise UpdateError("허용되지 않은 업데이트 서버입니다.")
    try:
        response = requests.get(manifest_url, timeout=timeout, headers={"Accept": "application/json"})
    except requests.RequestException as exc:
        raise UpdateError("업데이트 서버에 연결할 수 없습니다.") from exc
    if not response.ok:
        raise UpdateError(f"업데이트 확인 실패: HTTP {response.status_code}")
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise UpdateError("업데이트 정보가 올바른 JSON이 아닙니다.") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise UpdateError("지원하지 않는 업데이트 정보 형식입니다.")
    if require_signature:
        verify_signature(payload, public_key_b64)
        log_phase("signature_verified")
    notes = payload.get("release_notes", [])
    if not isinstance(notes, list) or not all(isinstance(note, str) for note in notes):
        raise UpdateError("업데이트 변경 내용 형식이 올바르지 않습니다.")
    try:
        published_at = datetime.fromisoformat(str(payload.get("published_at") or ""))
        if published_at.tzinfo is None:
            raise ValueError
    except ValueError as exc:
        raise UpdateError("업데이트 게시 시각 형식이 올바르지 않습니다.") from exc
    expected_package = {
        "format": "pyinstaller-onedir-zip",
        "executable": EXECUTABLE_NAME,
        "metadata": {"schema_version": SCHEMA_VERSION, "version": str(payload.get("version") or "")},
    }
    if payload.get("package") != expected_package:
        raise UpdateError("업데이트 package 정보가 올바르지 않습니다.")
    info = UpdateInfo(str(payload.get("version") or ""), str(payload.get("download_url") or ""),
                      str(payload.get("sha256") or "").lower(), int(payload.get("size") or 0),
                      tuple(notes), str(payload.get("signature") or ""))
    _version(info.version)
    if _https_host(info.url) not in hosts:
        raise UpdateError("허용되지 않은 업데이트 다운로드 서버입니다.")
    if not re.fullmatch(r"[0-9a-f]{64}", info.sha256):
        raise UpdateError("업데이트 SHA-256 값이 올바르지 않습니다.")
    if info.size <= 0 or info.size > MAX_PACKAGE_SIZE:
        raise UpdateError("업데이트 파일 크기가 허용 범위를 벗어났습니다.")
    log_phase("manifest_verified", from_version=current_version, to_version=info.version)
    return info if _version(info.version) > _version(current_version) else None


def download(info: UpdateInfo, *, timeout: int = 120) -> Path:
    log_phase("download", source=info.url, to_version=info.version)
    if _https_host(info.url) not in ALLOWED_UPDATE_HOSTS:
        raise UpdateError("허용되지 않은 업데이트 다운로드 서버입니다.")
    target_dir = data_root() / "updates" / "downloads"
    target_dir.mkdir(parents=True, exist_ok=True)
    final_path = target_dir / f"TAKURO-Collector-{info.version}-Windows.zip"
    fd, temp_name = tempfile.mkstemp(prefix="takuro-update-", suffix=".zip", dir=target_dir)
    os.close(fd)
    temp_path = Path(temp_name)
    digest, written = hashlib.sha256(), 0
    try:
        try:
            response_context = requests.get(info.url, timeout=timeout, stream=True)
        except requests.RequestException as exc:
            raise UpdateError("업데이트 파일을 다운로드할 수 없습니다.") from exc
        with response_context as response:
            if not response.ok:
                raise UpdateError(f"업데이트 다운로드 실패: HTTP {response.status_code}")
            if _https_host(str(getattr(response, "url", info.url))) not in ALLOWED_UPDATE_HOSTS:
                raise UpdateError("업데이트 다운로드가 허용되지 않은 서버로 이동했습니다.")
            with temp_path.open("wb") as output:
                for chunk in response.iter_content(1024 * 1024):
                    if chunk:
                        written += len(chunk)
                        if written > MAX_PACKAGE_SIZE or written > info.size:
                            raise UpdateError("업데이트 파일 크기가 manifest와 일치하지 않습니다.")
                        digest.update(chunk)
                        output.write(chunk)
        _write_state("downloaded")
        if written != info.size:
            raise UpdateError("업데이트 파일 크기가 manifest와 일치하지 않습니다.")
        if digest.hexdigest().lower() != info.sha256:
            raise UpdateError("업데이트 파일의 SHA-256이 일치하지 않습니다.")
        log_phase("sha_verified", source=temp_path, size=written)
        temp_path.replace(final_path)
        _write_state("verified")
        return final_path
    except Exception as exc:
        log_phase("download_failed", str(exc), error_type=type(exc).__name__, winerror=getattr(exc, "winerror", None))
        temp_path.unlink(missing_ok=True)
        write_state("failed", "download", to_version=info.version, error=exc)
        raise


def _safe_member(name: str) -> bool:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    return bool(normalized) and not path.is_absolute() and ".." not in path.parts and not re.match(r"^[A-Za-z]:", normalized)


def stage(info: UpdateInfo, archive: Path) -> Path:
    destination = data_root() / "updates" / "staging" / f"{info.version}-{uuid.uuid4().hex}"
    destination.mkdir(parents=True, exist_ok=False)
    log_phase("staging", source=archive, destination=destination, to_version=info.version)
    try:
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            if not members or any(not _safe_member(member.filename) for member in members):
                raise UpdateError("안전하지 않은 경로가 포함된 업데이트 ZIP입니다.")
            bundle.extractall(destination)
        roots = [destination, *[p for p in destination.iterdir() if p.is_dir()]]
        package = next((p for p in roots if (p / EXECUTABLE_NAME).is_file() and (p / "TAKURO Updater.exe").is_file()
                        and (p / "_internal").is_dir() and (p / "update-package.json").is_file()), None)
        if package is None:
            raise UpdateError("업데이트 ZIP의 onedir 구조가 올바르지 않습니다.")
        try:
            metadata = json.loads((package / "update-package.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise UpdateError("업데이트 package 정보가 올바르지 않습니다.") from exc
        if metadata != {"schema_version": SCHEMA_VERSION, "version": info.version}:
            raise UpdateError("manifest와 package 버전이 일치하지 않습니다.")
        _write_state("staged")
        log_phase("staged", destination=package)
        return package
    except Exception as exc:
        log_phase("staging_failed", str(exc), source=archive, destination=destination,
                  error_type=type(exc).__name__, winerror=getattr(exc, "winerror", None))
        shutil.rmtree(destination, ignore_errors=True)
        write_state("failed", "staging", to_version=info.version, error=exc,
                    source_path=archive, destination_path=destination)
        raise


def launch_helper(info: UpdateInfo, package: Path) -> None:
    if not getattr(sys, "frozen", False):
        raise UpdateError("자동 설치는 배포 EXE에서만 실행할 수 있습니다.")
    install_dir = Path(sys.executable).resolve().parent
    source_helper = install_dir / "TAKURO Updater.exe"
    if not source_helper.is_file():
        error = UpdateError("업데이트 helper를 찾을 수 없습니다.")
        log_phase("helper_launch_failed", str(error), install_dir=install_dir)
        write_state("failed", "helper_launch", to_version=info.version, error=error, install_dir=install_dir)
        raise error
    update_root = data_root() / "updates"
    helper_dir = update_root / "helper"
    helper_dir.mkdir(parents=True, exist_ok=True)
    helper_copy = helper_dir / f"TAKURO-Updater-{uuid.uuid4().hex}.exe"
    shutil.copy2(source_helper, helper_copy)
    marker, token = update_root / f"health-{uuid.uuid4().hex}.txt", uuid.uuid4().hex
    command_path = update_root / f"install-{uuid.uuid4().hex}.json"
    command_path.write_text(json.dumps({"version": info.version, "pid": os.getpid(),
        "install_dir": str(install_dir), "staged_dir": str(package.resolve()),
        "data_root": str(data_root().resolve()), "marker": str(marker), "token": token,
        "executable": EXECUTABLE_NAME, "from_version": __import__("takuro_collector", fromlist=["__version__"]).__version__,
        "log_path": str(active_log())}), encoding="utf-8")
    log_phase("helper_launch", install_dir=install_dir, staged_dir=package.resolve(), helper_cwd=helper_dir.resolve(),
              parent_pid=os.getpid(), to_version=info.version)
    try:
        subprocess.Popen(
            [str(helper_copy), str(command_path)],
            close_fds=True,
            cwd=str(helper_dir.resolve()),
        )
    except Exception as exc:
        log_phase("helper_launch_failed", str(exc), error_type=type(exc).__name__,
                  winerror=getattr(exc, "winerror", None), install_dir=install_dir, helper_cwd=helper_dir.resolve())
        write_state("failed", "helper_launch", to_version=info.version, error=exc,
                    install_dir=install_dir, helper_cwd=helper_dir.resolve())
        raise
