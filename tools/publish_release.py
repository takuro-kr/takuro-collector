from __future__ import annotations

import argparse
import base64
import ctypes
import ftplib
import hashlib
import io
import json
import os
import re
import ssl
import sys
import time
import urllib.request
import zipfile
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from takuro_collector.updater import (  # noqa: E402
    EXECUTABLE_NAME,
    PUBLIC_UPDATE_KEY_B64,
    SCHEMA_VERSION,
    _safe_member,
    _version,
    verify_signature,
)

HOST = "www1139.onamae.ne.jp"
PORT = 21
USERNAME = "takuro@updates.takuro.tech"
CREDENTIAL_TARGET = "TAKURO Collector Release Publisher"
HTTPS_ROOT = "https://updates.takuro.tech"
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


class PublishError(RuntimeError):
    pass


def _console(value: object, *, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    encoding = stream.encoding or "utf-8"
    text = str(value).encode(encoding, errors="backslashreplace").decode(encoding)
    print(text, file=stream)


@dataclass(frozen=True)
class LocalRelease:
    manifest_path: Path
    archive_path: Path
    manifest: dict
    manifest_bytes: bytes
    archive_size: int
    archive_sha256: str


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD), ("Attributes", wintypes.LPVOID),
        ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR),
    ]


def read_windows_credential(target: str = CREDENTIAL_TARGET) -> tuple[str, str]:
    if os.name != "nt":
        raise PublishError("Windows Credential Manager는 Windows에서만 사용할 수 있습니다.")
    advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    pointer = ctypes.POINTER(_CREDENTIALW)()
    advapi.CredReadW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)))
    advapi.CredReadW.restype = wintypes.BOOL
    advapi.CredFree.argtypes = (wintypes.LPVOID,)
    if not advapi.CredReadW(target, 1, 0, ctypes.byref(pointer)):
        raise PublishError(
            f"Windows Credential Manager에서 '{target}'을 찾을 수 없습니다. "
            "docs/UPDATE_SERVER.md의 등록 절차를 실행하세요."
        )
    try:
        cred = pointer.contents
        raw = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        password = raw.decode("utf-16-le")
        return str(cred.UserName or USERNAME), password
    finally:
        advapi.CredFree(pointer)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_local(output: Path) -> LocalRelease:
    manifest_path = output.resolve() / "latest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PublishError(f"manifest parse 실패: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION:
        raise PublishError("manifest schema version이 올바르지 않습니다.")
    version = str(manifest.get("version") or "")
    try:
        _version(version)
        verify_signature(manifest, PUBLIC_UPDATE_KEY_B64)
    except Exception as exc:
        raise PublishError(f"manifest signature/version 검증 실패: {exc}") from exc
    archive = output.resolve() / "releases" / version / f"TAKURO-Collector-{version}.zip"
    if not archive.is_file():
        raise PublishError(f"ZIP을 찾을 수 없습니다: {archive}")
    size, digest = archive.stat().st_size, sha256_file(archive)
    if manifest.get("size") != size or str(manifest.get("sha256", "")).lower() != digest:
        raise PublishError("ZIP size/SHA-256이 manifest와 일치하지 않습니다.")
    expected_url = f"{HTTPS_ROOT}/releases/{version}/{archive.name}"
    if manifest.get("download_url") != expected_url:
        raise PublishError("manifest download URL이 운영 경로와 일치하지 않습니다.")
    try:
        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
            if not names or any(not _safe_member(name) for name in names):
                raise PublishError("ZIP에 안전하지 않은 경로가 있습니다.")
            required = {EXECUTABLE_NAME, "TAKURO Updater.exe", "update-package.json"}
            if not required.issubset(names) or not any(name.startswith("_internal/") for name in names):
                raise PublishError("ZIP이 공식 PyInstaller onedir 구조가 아닙니다.")
            metadata = json.loads(bundle.read("update-package.json").decode("utf-8"))
    except (OSError, zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
        raise PublishError(f"ZIP/package metadata 검증 실패: {exc}") from exc
    if metadata != {"schema_version": SCHEMA_VERSION, "version": version}:
        raise PublishError("ZIP package version이 manifest와 일치하지 않습니다.")
    return LocalRelease(manifest_path, archive, manifest, manifest_bytes, size, digest)


def connect_ftps(username: str, password: str) -> ftplib.FTP_TLS:
    context = ssl.create_default_context()
    ftp = ftplib.FTP_TLS(context=context, timeout=30)
    ftp.connect(HOST, PORT)
    ftp.auth()
    ftp.login(username, password)
    ftp.prot_p()
    return ftp


def _progress(label: str, total: int) -> Callable[[bytes], None]:
    sent = 0
    last = -1
    def callback(block: bytes) -> None:
        nonlocal sent, last
        sent += len(block)
        percent = 100 if not total else min(100, sent * 100 // total)
        if percent != last and (percent == 100 or percent >= last + 5):
            _console(f"{label}: {percent}% ({sent}/{total} bytes)")
            last = percent
    return callback


def _download_hash(ftp: ftplib.FTP_TLS, path: str) -> tuple[int, str]:
    digest, size = hashlib.sha256(), 0
    def consume(block: bytes) -> None:
        nonlocal size
        digest.update(block)
        size += len(block)
    ftp.retrbinary(f"RETR {path}", consume)
    return size, digest.hexdigest()


def _download_bytes(ftp: ftplib.FTP_TLS, path: str) -> bytes:
    target = io.BytesIO()
    ftp.retrbinary(f"RETR {path}", target.write)
    return target.getvalue()


def _ensure_dir(ftp: ftplib.FTP_TLS, path: str) -> None:
    current = ""
    for part in PurePosixPath(path).parts:
        if part == "/":
            continue
        current += "/" + part
        try:
            ftp.mkd(current)
        except ftplib.error_perm as exc:
            if not str(exc).startswith("550"):
                raise


def _safe_rmtree_release(ftp: ftplib.FTP_TLS, version: str) -> None:
    if not SEMVER.fullmatch(version):
        raise PublishError(f"삭제 대상이 semantic version이 아닙니다: {version}")
    directory = f"/releases/{version}"
    entries = list(ftp.mlsd(directory))
    allowed = {f"TAKURO-Collector-{version}.zip", f"TAKURO-Collector-{version}.zip.uploading"}
    for name, facts in entries:
        if name in {".", ".."}:
            continue
        if facts.get("type") != "file" or name not in allowed:
            raise PublishError(f"예상하지 못한 파일이 있어 retention 삭제를 중단합니다: {directory}/{name}")
    for name, facts in entries:
        if name not in {".", ".."} and facts.get("type") == "file":
            ftp.delete(f"{directory}/{name}")
    ftp.rmd(directory)


def cleanup_releases(ftp: ftplib.FTP_TLS, keep: int = 3) -> list[str]:
    versions = []
    for name, facts in ftp.mlsd("/releases"):
        if facts.get("type") == "dir" and SEMVER.fullmatch(name):
            versions.append(name)
    versions.sort(key=_version, reverse=True)
    removed = []
    for version in versions[keep:]:
        _safe_rmtree_release(ftp, version)
        removed.append(version)
    return removed


def _http_bytes(url: str, timeout: int = 60) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "TAKURO-Release-Publisher/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def verify_https(release: LocalRelease, fetch: Callable[[str], bytes] = _http_bytes) -> None:
    version = release.manifest["version"]
    zip_url = release.manifest["download_url"] + f"?verify={time.time_ns()}"
    if hashlib.sha256(fetch(zip_url)).hexdigest() != release.archive_sha256:
        raise PublishError("HTTPS ZIP SHA-256 검증에 실패했습니다.")
    bust = json.loads(fetch(f"{HTTPS_ROOT}/latest.json?verify={time.time_ns()}").decode("utf-8"))
    plain = json.loads(fetch(f"{HTTPS_ROOT}/latest.json").decode("utf-8"))
    for candidate in (bust, plain):
        verify_signature(candidate, PUBLIC_UPDATE_KEY_B64)
        if candidate.get("version") != version:
            raise PublishError("query 없는 latest.json까지 새 버전으로 갱신되지 않았습니다.")


def publish(release: LocalRelease, ftp: ftplib.FTP_TLS,
            https_check: Callable[[LocalRelease], None] = verify_https) -> list[str]:
    version, name = release.manifest["version"], release.archive_path.name
    release_dir = f"/releases/{version}"
    uploading = f"{release_dir}/{name}.uploading"
    final_zip = f"{release_dir}/{name}"
    manifest_temp = f"/latest.json.uploading-{int(time.time())}"
    previous = f"/latest.json.previous-{int(time.time())}"
    _ensure_dir(ftp, release_dir)
    _console("[upload_zip] FTPS ZIP 업로드")
    with release.archive_path.open("rb") as stream:
        ftp.storbinary(f"STOR {uploading}", stream, callback=_progress("ZIP", release.archive_size))
    remote_size, remote_hash = _download_hash(ftp, uploading)
    if (remote_size, remote_hash) != (release.archive_size, release.archive_sha256):
        raise PublishError("업로드된 ZIP size/SHA-256 검증에 실패했습니다.")
    try:
        ftp.delete(final_zip)
    except ftplib.error_perm as exc:
        if not str(exc).startswith("550"):
            raise
    ftp.rename(uploading, final_zip)
    _console("[manifest_stage] temporary manifest 업로드/검증")
    ftp.storbinary(f"STOR {manifest_temp}", io.BytesIO(release.manifest_bytes))
    remote_manifest = _download_bytes(ftp, manifest_temp)
    parsed = json.loads(remote_manifest.decode("utf-8"))
    verify_signature(parsed, PUBLIC_UPDATE_KEY_B64)
    if remote_manifest != release.manifest_bytes:
        raise PublishError("업로드된 manifest bytes가 로컬과 일치하지 않습니다.")
    backed_up = False
    published = False
    try:
        try:
            ftp.rename("/latest.json", previous)
            backed_up = True
        except ftplib.error_perm as exc:
            if not str(exc).startswith("550"):
                raise
        ftp.rename(manifest_temp, "/latest.json")
        published = True
        _console("[https_verify] HTTPS ZIP/manifest 최종 검증")
        https_check(release)
    except Exception:
        if published:
            try:
                ftp.delete("/latest.json")
            except ftplib.error_perm:
                pass
        if backed_up:
            ftp.rename(previous, "/latest.json")
        raise
    if backed_up:
        ftp.delete(previous)
    _console("[retention] 성공 후 오래된 semantic version release 정리")
    return cleanup_releases(ftp, keep=3)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Publish a verified TAKURO Collector release over explicit FTPS")
    parser.add_argument("--output", type=Path, default=ROOT / "release-output")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    password = ""
    try:
        _console("[local_verify] manifest/signature/ZIP/package 검증")
        release = validate_local(args.output)
        _console(f"검증 완료: v{release.manifest['version']} ({release.archive_size} bytes)")
        if args.dry_run:
            _console("DRY RUN: 서버 연결/업로드/rename/delete를 수행하지 않았습니다.")
            return 0
        username, password = read_windows_credential()
        if username != USERNAME:
            raise PublishError("Credential Manager의 username이 배포 계정과 일치하지 않습니다.")
        _console(f"[connect] explicit FTPS {HOST}:{PORT}")
        ftp = connect_ftps(username, password)
        try:
            removed = publish(release, ftp)
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()
        _console(f"배포 완료: v{release.manifest['version']}; retention 삭제: {removed or '없음'}")
        return 0
    except Exception as exc:
        message = str(exc)
        if password:
            message = message.replace(password, "[REDACTED]")
        _console(f"배포 실패 [{type(exc).__name__}]: {message}", error=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
