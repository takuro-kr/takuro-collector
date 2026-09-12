from pathlib import Path

from takuro_collector.db import Database
from takuro_collector.models import PropertyCandidate


def _candidate(source_id: str, building: str = "アムールA&A", room: str = "202") -> PropertyCandidate:
    return PropertyCandidate(
        source_site="kinoshita-chintai.com",
        source_property_id=source_id,
        management_company="株式会社木下の賃貸",
        building_name=building,
        room=room,
        prefecture="神奈川県",
        address="神奈川県横浜市瀬谷区本郷1-54-4",
        source_url="https://kinoshita-chintai.com/example",
    )


def test_ready_fallback_matches_unique_building_room_when_source_id_changed(tmp_path):
    db = Database(tmp_path / "collector.sqlite3")
    try:
        db.upsert_property(_candidate("new-local-id"))
        row = db.property_by_ready_fallback("kinoshita-chintai.com", "アムールA&A", "202号室")
        assert row is not None
        assert row["source_property_id"] == "new-local-id"
    finally:
        db.close()


def test_ready_fallback_refuses_ambiguous_match(tmp_path):
    db = Database(tmp_path / "collector.sqlite3")
    try:
        db.upsert_property(_candidate("id-1"))
        db.upsert_property(_candidate("id-2"))
        assert db.property_by_ready_fallback("kinoshita-chintai.com", "アムールA&A", "202") is None
    finally:
        db.close()


def test_poll_does_not_refresh_full_table_when_nothing_changed():
    text = Path("takuro_collector/ui.py").read_text(encoding="utf-8")
    block = text.split("def poll_ready_packages", 1)[1].split("def sync_wordpress", 1)[0]
    assert "refresh_on_finish=False" in block
    assert '("downloaded", "packaged", "submitted")' in block
    assert '("matched", "downloaded", "packaged", "submitted")' not in block


def test_primary_identity_remains_first_choice():
    text = Path("takuro_collector/wordpress.py").read_text(encoding="utf-8")
    block = text.split("for remote in rows:", 1)[1].split("matched += 1", 1)[0]
    assert block.index("property_by_identity") < block.index("property_by_ready_fallback")


def test_ready_pull_preserves_bare_hostname_for_identity_lookup(tmp_path, monkeypatch):
    db = Database(tmp_path / "collector.sqlite3")
    try:
        pid, _ = db.upsert_property(_candidate("stable-id"))
        # Existing local PDF means this test exercises only ready identity matching.
        pdf = tmp_path / "existing.pdf"
        pdf.write_bytes(b"%PDF-1.4\\n%%EOF\\n")
        with db.conn:
            db.conn.execute("UPDATE properties SET pdf_path=? WHERE id=?", (str(pdf), pid))

        class FakeClient:
            def ready(self):
                return [{
                    "id": 77,
                    "source_site": "kinoshita-chintai.com",
                    "source_property_id": "stable-id",
                    "building_name": "アムールA&A",
                    "room": "202",
                }]

        from takuro_collector.wordpress import WordPressSync
        sync = WordPressSync(db)
        monkeypatch.setattr(sync, "client", lambda: FakeClient())
        result = sync.pull_ready_drawings(auto_package=False)
        assert result["remote_ready"] == 1
        assert result["matched"] == 1
        assert result["skipped"] == 0
        assert result["skip_details"] == []
    finally:
        db.close()


def test_ready_pull_reports_actionable_skip_details(tmp_path, monkeypatch):
    db = Database(tmp_path / "collector.sqlite3")
    try:
        class FakeClient:
            def ready(self):
                return [{
                    "id": 88,
                    "source_site": "kinoshita-chintai.com",
                    "source_property_id": "old-id",
                    "building_name": "없는매물",
                    "room": "999",
                }]

        from takuro_collector.wordpress import WordPressSync
        sync = WordPressSync(db)
        monkeypatch.setattr(sync, "client", lambda: FakeClient())
        result = sync.pull_ready_drawings(auto_package=False)
        assert result["matched"] == 0
        assert result["skipped"] == 1
        detail = result["skip_details"][0]
        assert detail["source_site"] == "kinoshita-chintai.com"
        assert detail["normalized_site"] == "kinoshita-chintai.com"
        assert detail["reason"] == "local_identity_not_found"
    finally:
        db.close()
