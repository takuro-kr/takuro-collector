from pathlib import Path

from takuro_collector import __version__
from takuro_collector.wordpress import WordPressClient


class FakeResponse:
    ok = True
    status_code = 200
    def json(self):
        return {"ok": True, "job_id": "b" * 32, "status": "uploaded", "draft_only": True}


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.last = None
    def post(self, url, **kwargs):
        self.last = (url, kwargs)
        return FakeResponse()


def test_version_021():
    assert tuple(map(int, __version__.split("."))) >= (0, 2, 1)


def test_upload_package_defaults_to_upload_only(tmp_path: Path):
    p = tmp_path / "listing.zip"
    p.write_bytes(b"PK\\x03\\x04test-package")
    c = WordPressClient("https://example.com", "a" * 64)
    fake = FakeSession()
    c.session = fake
    c.upload_package(p, source_property_id="11016_4")
    _url, kw = fake.last
    assert kw["data"]["auto_fast"] == "0"
    assert kw["data"]["draft_only"] == "1"


def test_upload_package_can_opt_into_autofast(tmp_path: Path):
    p = tmp_path / "listing.zip"
    p.write_bytes(b"PK\\x03\\x04test-package")
    c = WordPressClient("https://example.com", "a" * 64)
    fake = FakeSession()
    c.session = fake
    c.upload_package(p, auto_fast=True)
    _url, kw = fake.last
    assert kw["data"]["auto_fast"] == "1"
