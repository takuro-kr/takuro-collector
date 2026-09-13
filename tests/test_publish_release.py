from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from pathlib import Path, PurePosixPath

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tools import publish_release as publisher
from takuro_collector.updater import _signed_payload


def make_release(tmp_path: Path, monkeypatch, version="0.4.12"):
    output = tmp_path / "release-output"
    archive = output / "releases" / version / f"TAKURO-Collector-{version}.zip"
    archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("TAKURO Collector.exe", b"collector")
        bundle.writestr("TAKURO Updater.exe", b"helper")
        bundle.writestr("_internal/runtime.dll", b"runtime")
        bundle.writestr("update-package.json", json.dumps({"schema_version": 1, "version": version}))
    content = archive.read_bytes()
    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    monkeypatch.setattr(publisher, "PUBLIC_UPDATE_KEY_B64", public)
    manifest = {
        "schema_version": 1, "version": version, "published_at": "2026-09-13T12:00:00+09:00",
        "download_url": f"https://updates.takuro.tech/releases/{version}/{archive.name}",
        "sha256": hashlib.sha256(content).hexdigest(), "size": len(content), "release_notes": [],
        "package": {"format": "pyinstaller-onedir-zip", "executable": "TAKURO Collector.exe",
                    "metadata": {"schema_version": 1, "version": version}},
    }
    manifest["signature"] = base64.b64encode(key.sign(_signed_payload(manifest))).decode()
    (output / "latest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return output, manifest


class FakeFTP:
    def __init__(self):
        self.files = {"/latest.json": b"old"}
        self.dirs = {"/", "/releases"}
        self.operations = []
        self.fail_store = False
        self.fail_manifest_rename = False

    def mkd(self, path):
        self.dirs.add(path); self.operations.append(("mkd", path))

    def storbinary(self, command, stream, callback=None):
        path = command.split(" ", 1)[1]
        if self.fail_store:
            raise OSError("upload failed")
        chunks = []
        while True:
            block = stream.read(8192)
            if not block:
                break
            chunks.append(block)
            if callback:
                callback(block)
        self.files[path] = b"".join(chunks); self.operations.append(("store", path))

    def retrbinary(self, command, callback):
        callback(self.files[command.split(" ", 1)[1]])

    def rename(self, source, destination):
        if self.fail_manifest_rename and source.startswith("/latest.json.uploading"):
            raise OSError("swap failed")
        self.files[destination] = self.files.pop(source); self.operations.append(("rename", source, destination))

    def delete(self, path):
        if path not in self.files:
            import ftplib
            raise ftplib.error_perm("550 missing")
        del self.files[path]; self.operations.append(("delete", path))

    def mlsd(self, path):
        prefix = path.rstrip("/") + "/"
        found = {}
        for directory in self.dirs:
            if directory.startswith(prefix):
                rest = directory[len(prefix):]
                if rest and "/" not in rest:
                    found[rest] = {"type": "dir"}
        for file in self.files:
            if file.startswith(prefix):
                rest = file[len(prefix):]
                if rest and "/" not in rest:
                    found[rest] = {"type": "file"}
        return list(found.items())

    def rmd(self, path):
        self.dirs.remove(path); self.operations.append(("rmd", path))

    def pwd(self):
        return "/"

    def quit(self):
        self.operations.append(("quit",))

    def close(self):
        self.operations.append(("close",))


def test_validate_local_checks_signature_sha_and_package(tmp_path, monkeypatch):
    output, manifest = make_release(tmp_path, monkeypatch)
    release = publisher.validate_local(output)
    assert release.manifest["version"] == manifest["version"]


def test_validate_local_rejects_sha_before_server_access(tmp_path, monkeypatch):
    output, _ = make_release(tmp_path, monkeypatch)
    manifest = json.loads((output / "latest.json").read_text())
    manifest["sha256"] = "0" * 64
    (output / "latest.json").write_text(json.dumps(manifest))
    with pytest.raises(publisher.PublishError, match="signature"):
        publisher.validate_local(output)


def test_validate_local_rejects_zip_tamper(tmp_path, monkeypatch):
    output, _ = make_release(tmp_path, monkeypatch)
    archive = next((output / "releases").rglob("*.zip"))
    archive.write_bytes(archive.read_bytes() + b"tamper")
    with pytest.raises(publisher.PublishError, match="size/SHA"):
        publisher.validate_local(output)


def test_publish_upload_failure_never_touches_latest(tmp_path, monkeypatch):
    output, _ = make_release(tmp_path, monkeypatch); release = publisher.validate_local(output)
    ftp = FakeFTP(); ftp.fail_store = True
    with pytest.raises(OSError, match="upload failed"):
        publisher.publish(release, ftp, lambda _: None)
    assert ftp.files["/latest.json"] == b"old"


def test_manifest_swap_failure_restores_latest(tmp_path, monkeypatch):
    output, _ = make_release(tmp_path, monkeypatch); release = publisher.validate_local(output)
    ftp = FakeFTP(); ftp.fail_manifest_rename = True
    with pytest.raises(OSError, match="swap failed"):
        publisher.publish(release, ftp, lambda _: None)
    assert ftp.files["/latest.json"] == b"old"


def test_https_failure_rolls_back_latest_and_skips_retention(tmp_path, monkeypatch):
    output, _ = make_release(tmp_path, monkeypatch); release = publisher.validate_local(output)
    ftp = FakeFTP(); ftp.dirs |= {"/releases/0.4.9", "/releases/0.4.10", "/releases/0.4.11"}
    with pytest.raises(RuntimeError, match="https failed"):
        publisher.publish(release, ftp, lambda _: (_ for _ in ()).throw(RuntimeError("https failed")))
    assert ftp.files["/latest.json"] == b"old"
    assert all(op[0] != "rmd" for op in ftp.operations)


def test_retention_keeps_latest_and_previous_and_ignores_other_dirs():
    ftp = FakeFTP(); versions = ["0.4.8", "0.4.9", "0.4.10", "0.4.11", "error"]
    for version in versions:
        ftp.dirs.add(f"/releases/{version}")
        if publisher.SEMVER.fullmatch(version):
            ftp.files[f"/releases/{version}/TAKURO-Collector-{version}.zip"] = b"zip"
    removed = publisher.cleanup_releases(ftp)
    assert removed == ["0.4.9", "0.4.8"]
    assert "/releases/error" in ftp.dirs
    assert all(f"/releases/{v}" in ftp.dirs for v in ("0.4.11", "0.4.10"))


def test_dry_run_performs_no_credential_or_server_action(tmp_path, monkeypatch, capsys):
    output, _ = make_release(tmp_path, monkeypatch)
    monkeypatch.setattr(publisher, "read_windows_credential", lambda: pytest.fail("credential read"))
    monkeypatch.setattr(publisher.sys, "argv", ["publish_release.py", "--output", str(output), "--dry-run"])
    assert publisher.main() == 0
    assert "DRY RUN" in capsys.readouterr().out


def test_password_is_redacted_from_errors(tmp_path, monkeypatch, capsys):
    output, _ = make_release(tmp_path, monkeypatch); secret = "do-not-print-this"
    monkeypatch.setattr(publisher, "read_windows_credential", lambda: (publisher.USERNAME, secret))
    monkeypatch.setattr(publisher, "connect_ftps", lambda *_: (_ for _ in ()).throw(RuntimeError(secret)))
    monkeypatch.setattr(publisher.sys, "argv", ["publish_release.py", "--output", str(output)])
    assert publisher.main() == 1
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert "[REDACTED]" in captured.err


def test_verify_https_requires_plain_and_cache_busted_latest(tmp_path, monkeypatch):
    output, manifest = make_release(tmp_path, monkeypatch); release = publisher.validate_local(output)
    archive = release.archive_path.read_bytes()
    stale = dict(manifest, version="0.4.11")
    def fetch(url):
        if ".zip" in url:
            return archive
        return json.dumps(manifest if "?verify=" in url else stale).encode()
    with pytest.raises(Exception, match="Ed25519|signature|latest.json"):
        publisher.verify_https(release, fetch)


def test_check_connection_is_strictly_read_only(monkeypatch, capsys):
    password = "connection-test-password"
    ftp = FakeFTP()
    ftp.files["/latest.json"] = json.dumps({"version": "0.4.12"}).encode()
    ftp.dirs |= {"/releases/0.4.11", "/releases/0.4.12"}
    monkeypatch.setattr(publisher, "read_windows_credential", lambda: (publisher.USERNAME, password))
    monkeypatch.setattr(publisher, "connect_ftps", lambda *_args: ftp)
    monkeypatch.setattr(publisher.sys, "argv", ["publish_release.py", "--check-connection"])
    assert publisher.main() == 0
    output = capsys.readouterr().out
    assert "credential load OK" in output and "FTPS login OK" in output
    assert "pwd: /" in output and "latest version: 0.4.12" in output
    assert "0.4.11, 0.4.12" in output and password not in output
    assert not any(operation[0] in {"store", "mkd", "rename", "delete", "rmd"}
                   for operation in ftp.operations)


def test_check_connection_failure_redacts_password(monkeypatch, capsys):
    password = "connection-secret"
    monkeypatch.setattr(publisher, "read_windows_credential", lambda: (publisher.USERNAME, password))
    monkeypatch.setattr(publisher, "connect_ftps",
                        lambda *_args: (_ for _ in ()).throw(RuntimeError(password)))
    monkeypatch.setattr(publisher.sys, "argv", ["publish_release.py", "--check-connection"])
    assert publisher.main() == 1
    output = capsys.readouterr()
    assert password not in output.out + output.err
    assert "[REDACTED]" in output.err
