from pathlib import Path

from takuro_collector import __version__
from takuro_collector.wordpress import WordPressClient


class FakeResponse:
    ok = True
    status_code = 200
    def json(self):
        return {"ok": True, "job_id": "a" * 32, "status": "queued", "draft_only": True}


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.last = None
    def post(self, url, **kwargs):
        self.last = (url, kwargs)
        return FakeResponse()


def test_version_020():
    assert tuple(map(int, __version__.split("."))) >= (0, 2, 0)


def test_upload_package_requests_autofast_draft_only(tmp_path: Path):
    p = tmp_path / "listing.zip"
    p.write_bytes(b"PK\x03\x04test-package")
    c = WordPressClient("https://example.com", "a" * 64)
    fake = FakeSession()
    c.session = fake
    result = c.upload_package(p, source_property_id="11016_4", auto_fast=True)
    assert result["status"] == "queued"
    url, kw = fake.last
    assert url.endswith("/wp-json/takuro/v1/collection/packages")
    assert kw["data"]["auto_fast"] == "1"
    assert kw["data"]["draft_only"] == "1"
    assert kw["headers"]["X-Takuro-Draft-Only"] == "1"
    assert len(kw["headers"]["X-Takuro-Package-SHA256"]) == 64
    assert kw["files"]["package"][0] == "listing.zip"
