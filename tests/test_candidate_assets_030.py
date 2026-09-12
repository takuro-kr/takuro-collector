from pathlib import Path

from takuro_collector.wordpress import WordPressClient


class Response:
    ok = True
    status_code = 200
    def json(self):
        return {"ok": True, "candidate_id": 77, "photo_count": 3, "staged": True}


class Session:
    def __init__(self):
        self.headers = {}
        self.call = None
    def post(self, url, **kwargs):
        self.call = (url, kwargs)
        return Response()


def test_collector_uploads_txt_photos_directly_to_candidate(tmp_path: Path):
    package = tmp_path / "assets.zip"
    package.write_bytes(b"PK\x03\x04candidate-assets")
    client = WordPressClient("https://homes.example/kr", "a" * 64)
    session = Session()
    client.session = session
    result = client.upload_candidate_assets(77, package)
    assert result["staged"] is True
    url, kwargs = session.call
    assert url.endswith("/wp-json/takuro-registration/v1/candidates/77/assets")
    assert kwargs["files"]["package"][0] == "assets.zip"
