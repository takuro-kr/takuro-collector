import base64
import hashlib
import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from takuro_collector import updater, updater_helper
from scripts import release_update


class Response:
    ok, status_code = True, 200
    def __init__(self, payload=None, content=b""): self.payload, self.content = payload, content
    def json(self):
        if isinstance(self.payload, Exception): raise self.payload
        return self.payload
    def iter_content(self, _size): yield self.content
    def __enter__(self): return self
    def __exit__(self, *_args): return False


def manifest(**changes):
    value = {"schema_version": 1, "version": "0.4.5", "published_at": "2026-09-12T00:00:00+09:00",
             "download_url": "https://updates.takuro.tech/releases/0.4.5/TAKURO-Collector-0.4.5.zip",
             "sha256": "a" * 64, "size": 10, "release_notes": ["AMM 수집 지원"],
             "package": {"format": "pyinstaller-onedir-zip", "executable": "TAKURO Collector.exe",
                         "metadata": {"schema_version": 1, "version": "0.4.5"}}, "signature": "test"}
    value.update(changes)
    return value


def test_production_endpoint_allowlist_and_embedded_public_key_are_fixed():
    assert updater.UPDATE_HOST == "updates.takuro.tech"
    assert updater.UPDATE_MANIFEST_URL == "https://updates.takuro.tech/latest.json"
    assert updater.ALLOWED_UPDATE_HOSTS == {"updates.takuro.tech"}
    assert len(base64.b64decode(updater.PUBLIC_UPDATE_KEY_B64, validate=True)) == 32


def test_update_check_newer_current_and_validation(monkeypatch):
    payload = manifest()
    monkeypatch.setattr(updater.requests, "get", lambda *_a, **_k: Response(payload))
    assert updater.check(updater.UPDATE_MANIFEST_URL, "0.4.4", require_signature=False).version == "0.4.5"
    assert updater.check(updater.UPDATE_MANIFEST_URL, "0.4.5", require_signature=False) is None
    with pytest.raises(updater.UpdateError, match="HTTPS"):
        updater.check("http://updates.takuro.tech/latest.json", "0.4.4", require_signature=False)
    payload["schema_version"] = 2
    with pytest.raises(updater.UpdateError, match="지원하지 않는"):
        updater.check(updater.UPDATE_MANIFEST_URL, "0.4.4", require_signature=False)


@pytest.mark.parametrize("change", [{"version": "bad"}, {"sha256": "bad"}, {"size": 0},
    {"size": updater.MAX_PACKAGE_SIZE + 1}, {"download_url": "http://updates.example/app.zip"},
    {"download_url": "https://evil.example/app.zip"}, {"release_notes": "bad"}, {"package": {}},
    {"published_at": "2026-09-12"}])
def test_malformed_manifest_is_rejected(monkeypatch, change):
    monkeypatch.setattr(updater.requests, "get", lambda *_a, **_k: Response(manifest(**change)))
    with pytest.raises(updater.UpdateError):
        updater.check(updater.UPDATE_MANIFEST_URL, "0.4.4", require_signature=False)


def test_manifest_http_and_json_failures(monkeypatch):
    response = Response({}); response.ok, response.status_code = False, 503
    monkeypatch.setattr(updater.requests, "get", lambda *_a, **_k: response)
    with pytest.raises(updater.UpdateError, match="503"):
        updater.check(updater.UPDATE_MANIFEST_URL, "0.4.4", require_signature=False)
    monkeypatch.setattr(updater.requests, "get", lambda *_a, **_k: Response(ValueError()))
    with pytest.raises(updater.UpdateError, match="JSON"):
        updater.check(updater.UPDATE_MANIFEST_URL, "0.4.4", require_signature=False)


def test_ed25519_signature_success_and_failure():
    crypto = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    from cryptography.hazmat.primitives import serialization
    private = crypto.Ed25519PrivateKey.generate()
    public = base64.b64encode(private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    payload = manifest(signature="")
    payload["signature"] = base64.b64encode(private.sign(updater._signed_payload(payload))).decode()
    updater.verify_signature(payload, public)
    payload["version"] = "0.4.6"
    with pytest.raises(updater.UpdateError, match="서명"): updater.verify_signature(payload, public)


def test_signed_production_manifest_accepts_right_key_and_rejects_wrong_key_or_tampering(monkeypatch):
    crypto = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    from cryptography.hazmat.primitives import serialization
    private = crypto.Ed25519PrivateKey.generate()
    other = crypto.Ed25519PrivateKey.generate()
    encode_public = lambda key: base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    payload = manifest(signature="")
    payload["signature"] = base64.b64encode(private.sign(updater._signed_payload(payload))).decode()
    monkeypatch.setattr(updater.requests, "get", lambda *_a, **_k: Response(payload))
    assert updater.check(updater.UPDATE_MANIFEST_URL, "0.4.4", public_key_b64=encode_public(private))
    with pytest.raises(updater.UpdateError, match="서명"):
        updater.check(updater.UPDATE_MANIFEST_URL, "0.4.4", public_key_b64=encode_public(other))
    payload["size"] += 1
    with pytest.raises(updater.UpdateError, match="서명"):
        updater.check(updater.UPDATE_MANIFEST_URL, "0.4.4", public_key_b64=encode_public(private))


def test_download_verifies_size_sha_and_cleans_failure(tmp_path, monkeypatch):
    content = b"verified update archive"
    info = updater.UpdateInfo("0.4.5", "https://updates.takuro.tech/releases/0.4.5/app.zip", hashlib.sha256(content).hexdigest(), len(content))
    monkeypatch.setattr(updater, "data_root", lambda: tmp_path)
    monkeypatch.setattr(updater.requests, "get", lambda *_a, **_k: Response(content=content))
    assert updater.download(info).read_bytes() == content
    with pytest.raises(updater.UpdateError, match="SHA-256"):
        updater.download(updater.UpdateInfo("0.4.6", info.url, "0" * 64, len(content)))
    assert not list((tmp_path / "updates" / "downloads").glob("takuro-update-*"))
    with pytest.raises(updater.UpdateError, match="허용되지 않은"):
        updater.download(updater.UpdateInfo("0.4.6", "https://evil.example/app.zip", "0" * 64, 1))


def zip_bytes(entries):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as bundle:
        for name, data in entries.items(): bundle.writestr(name, data)
    return stream.getvalue()


def test_stage_accepts_onedir_and_rejects_zip_slip(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "data_root", lambda: tmp_path)
    info = updater.UpdateInfo("0.4.5", "https://updates.takuro.tech/releases/0.4.5/app.zip", "a" * 64, 1)
    good = tmp_path / "good.zip"
    good.write_bytes(zip_bytes({"TAKURO Collector.exe": b"exe", "TAKURO Updater.exe": b"helper",
                                "_internal/runtime.dll": b"dll",
                                "update-package.json": b'{"schema_version":1,"version":"0.4.5"}'}))
    assert (updater.stage(info, good) / "TAKURO Collector.exe").is_file()
    bad = tmp_path / "bad.zip"; bad.write_bytes(zip_bytes({"../escape.txt": b"bad"}))
    with pytest.raises(updater.UpdateError, match="안전하지 않은"): updater.stage(info, bad)
    assert not (tmp_path / "escape.txt").exists()


def test_release_tool_creates_verifiable_static_server_files(tmp_path):
    private = tmp_path / "signing" / "private.pem"
    public = tmp_path / "signing" / "public.txt"
    release_update.generate_key(private, public)
    archive = tmp_path / "build.zip"
    archive.write_bytes(zip_bytes({"TAKURO Collector.exe": b"exe", "TAKURO Updater.exe": b"helper",
        "_internal/runtime.dll": b"dll", "update-package.json": b'{"schema_version":1,"version":"0.4.5"}'}))
    latest, release_zip = release_update.prepare(SimpleNamespace(
        private_key=private, zip=archive, version="0.4.5", output=tmp_path / "release-output",
        note=["AMM 수집 지원"]
    ))
    payload = json.loads(latest.read_text(encoding="utf-8"))
    updater.verify_signature(payload, public.read_text(encoding="ascii").strip())
    assert payload["download_url"] == "https://updates.takuro.tech/releases/0.4.5/TAKURO-Collector-0.4.5.zip"
    assert release_zip.read_bytes() == archive.read_bytes()


class Process:
    def __init__(self, marker=None, token="", succeeds=True): self.marker, self.token, self.succeeds = marker, token, succeeds
    def poll(self):
        if self.succeeds and self.marker and not self.marker.exists(): self.marker.write_text(self.token, encoding="utf-8")
        return None if self.succeeds else 1
    def terminate(self): pass


def install_fixture(tmp_path):
    install, staged, user = tmp_path / "program" / "TAKURO Collector", tmp_path / "stage", tmp_path / "user"
    for root, value in ((install, b"old"), (staged, b"new")):
        (root / "_internal").mkdir(parents=True); (root / "TAKURO Collector.exe").write_bytes(value)
    user.mkdir(); (user / "collector.db").write_bytes(b"database"); (user / "settings.txt").write_text("secret")
    marker, token = user / "updates" / "health.txt", "token"
    return {"pid": 1, "install_dir": str(install), "staged_dir": str(staged), "data_root": str(user),
            "marker": str(marker), "token": token, "executable": "TAKURO Collector.exe"}, install, user, marker


def test_helper_replaces_program_and_preserves_user_data(tmp_path):
    command, install, user, marker = install_fixture(tmp_path)
    assert updater_helper.install(command, launch=lambda *_a, **_k: Process(marker, command["token"]),
                                  timeout=.2, wait_exit=lambda *_: True) == "launch_verified"
    assert (install / "TAKURO Collector.exe").read_bytes() == b"new"
    assert (user / "collector.db").read_bytes() == b"database"
    assert (user / "settings.txt").read_text() == "secret"
    assert (user / "updates" / "backup" / "known-good" / "TAKURO Collector.exe").read_bytes() == b"old"


def test_helper_rolls_back_when_new_version_does_not_start(tmp_path):
    command, install, user, _marker = install_fixture(tmp_path); calls = []
    def launch(args, **_kwargs): calls.append(args); return Process(succeeds=False)
    assert updater_helper.install(command, launch=launch, timeout=.05, wait_exit=lambda *_: True) == "rolled_back"
    assert (install / "TAKURO Collector.exe").read_bytes() == b"old"
    assert json.loads((user / "updates" / "state.json").read_text())["state"] == "rolled_back"
    assert len(calls) == 2


def test_helper_rejects_duplicate_install(tmp_path):
    command, _install, user, _marker = install_fixture(tmp_path)
    (user / "updates").mkdir(); (user / "updates" / "install.lock").write_text("locked")
    with pytest.raises(FileExistsError): updater_helper.install(command, timeout=.01, wait_exit=lambda *_: True)
