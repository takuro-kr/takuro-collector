from pathlib import Path
from takuro_collector import __version__
from takuro_collector.wordpress import WordPressClient

class Response:
    ok = True
    status_code = 200
    def json(self):
        return {"job_id": "c" * 32, "status": "draft_created", "draft_only": True}

class Session:
    def __init__(self):
        self.headers = {}
        self.calls = []
    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return Response()

def test_release_0210_version():
    assert tuple(map(int, __version__.split("."))) >= (0, 2, 9)

def test_package_status_uses_existing_collection_package_job():
    c = WordPressClient("https://example.test", "a" * 64)
    s = Session(); c.session = s
    out = c.package_status("c" * 32)
    assert out["status"] == "draft_created"
    assert s.calls[0][1].endswith("/wp-json/takuro/v1/collection/packages/" + "c" * 32)

def test_source_enforces_wordpress_pdf_autofast_draft_contract():
    source = Path(__file__).parents[1] / "takuro_collector" / "wordpress.py"
    text = source.read_text(encoding="utf-8")
    assert "pull_ready_drawings(self, *, auto_package: bool = True, auto_fast: bool = True)" in text
    assert "create_zip(self.db, pid, require_pdf=True)" in text
    assert "self.auto_submit_package(pid, out, auto_fast=bool(auto_fast))" in text
    assert 'result.get("draft_only") is not True' in text

def test_existing_job_branch_is_strictly_idempotent():
    source = Path(__file__).parents[1] / "takuro_collector" / "wordpress.py"
    text = source.read_text(encoding="utf-8")
    block = text.split("if existing_job:", 1)[1].split("from .fetcher import Fetcher", 1)[0]
    assert "already_submitted += 1" in block
    assert "continue" in block
    assert "auto_submit_package" not in block
