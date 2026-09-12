from pathlib import Path
import zipfile

from takuro_collector.db import Database
from takuro_collector.models import PropertyCandidate
from takuro_collector.wordpress import WordPressSync


def test_wordpress_selected_pdf_creates_and_submits_draft_package(tmp_path, monkeypatch):
    from takuro_collector import package as package_mod
    from takuro_collector import paths
    from takuro_collector import photos as photos_mod

    properties = tmp_path / "properties"
    exports = tmp_path / "exports"
    exports.mkdir()
    monkeypatch.setattr(paths, "property_root", lambda: properties)
    monkeypatch.setattr(paths, "export_root", lambda: exports)
    monkeypatch.setattr(photos_mod, "property_root", lambda: properties)
    monkeypatch.setattr(package_mod, "export_root", lambda: exports)

    db = Database(tmp_path / "collector.sqlite3")
    try:
        candidate = PropertyCandidate(
            source_site="kinoshita-chintai.com",
            source_property_id="13757_2",
            management_company="株式会社木下の賃貸",
            building_name="Batelira",
            room="102",
            prefecture="神奈川県",
            address="神奈川県横浜市戸塚区深谷町715-7",
            rent=150000,
            management_fee=5000,
            deposit="0",
            key_money="1ヵ月",
            photo_sources=[{"url": "https://example.test/room.jpg", "kind": "photo"}],
            source_url="https://kinoshita-chintai.com/details/13757_2details.html",
        )
        pid, _ = db.upsert_property(candidate)
        photo = properties / "Batelira 102" / "room.jpg"
        photo.parent.mkdir(parents=True)
        photo.write_bytes(b"\xff\xd8" + b"photo" * 100 + b"\xff\xd9")
        photo_row = db.photos(pid)[0]
        db.mark_photo(int(photo_row["id"]), status="downloaded", local_path=str(photo), sha256="a" * 64)

        class FakeClient:
            uploaded = None

            def ready(self):
                return [{
                    "id": 321,
                    "source_site": "kinoshita-chintai.com",
                    "source_property_id": "13757_2",
                    "building_name": "Batelira",
                    "room": "102",
                }]

            def download_drawing(self, candidate_id, destination):
                assert candidate_id == 321
                path = Path(destination)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"%PDF-1.4\n%%EOF\n")
                return path

            def upload_package(self, zip_path, *, source_property_id="", auto_fast=False):
                assert source_property_id == "13757_2"
                assert auto_fast is True
                with zipfile.ZipFile(zip_path) as zf:
                    names = set(zf.namelist())
                    assert "REINS.pdf" in names
                    assert "基本情報.txt" in names
                    assert "photo-001.jpg" in names
                self.uploaded = Path(zip_path)
                return {"job_id": "b" * 32, "status": "queued", "draft_only": True}

        fake = FakeClient()
        sync = WordPressSync(db)
        monkeypatch.setattr(sync, "client", lambda: fake)
        result = sync.pull_ready_drawings(auto_package=True, auto_fast=True)

        assert result["downloaded"] == 1
        assert result["packaged"] == 1
        assert result["submitted"] == 1
        assert result["errors"] == 0
        assert fake.uploaded and fake.uploaded.is_file()
        stored = db.property(pid)
        assert stored["wp_package_job_id"] == "b" * 32
        assert stored["wp_package_state"] == "queued"
    finally:
        db.close()
