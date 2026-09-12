import hashlib

import pytest

from takuro_collector import updater


class Response:
    ok = True
    status_code = 200

    def __init__(self, *, payload=None, content=b""):
        self.payload = payload
        self.content = content

    def json(self):
        return self.payload

    def iter_content(self, _size):
        yield self.content

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_update_check_requires_newer_https_manifest(monkeypatch):
    payload = {"version": "0.4.2", "url": "https://updates.example/app.zip", "sha256": "a" * 64}
    monkeypatch.setattr(updater.requests, "get", lambda *_args, **_kwargs: Response(payload=payload))
    assert updater.check("https://updates.example/manifest.json", "0.4.1").version == "0.4.2"
    assert updater.check("https://updates.example/manifest.json", "0.4.2") is None
    with pytest.raises(updater.UpdateError, match="HTTPS"):
        updater.check("http://updates.example/manifest.json", "0.4.1")


def test_update_download_verifies_sha256(tmp_path, monkeypatch):
    content = b"verified update archive"
    info = updater.UpdateInfo("0.4.2", "https://updates.example/app.zip", hashlib.sha256(content).hexdigest())
    monkeypatch.setattr(updater, "data_root", lambda: tmp_path)
    monkeypatch.setattr(updater.requests, "get", lambda *_args, **_kwargs: Response(content=content))
    path = updater.download(info)
    assert path.read_bytes() == content

    bad = updater.UpdateInfo("0.4.3", info.url, "0" * 64)
    with pytest.raises(updater.UpdateError, match="SHA-256"):
        updater.download(bad)

