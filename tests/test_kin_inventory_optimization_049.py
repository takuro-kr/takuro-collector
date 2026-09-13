from datetime import datetime, timedelta, timezone
from pathlib import Path

import takuro_collector.collector as collector_module
from takuro_collector.collector import CollectorEngine
from takuro_collector.db import Database
from takuro_collector.models import PropertyCandidate
from takuro_collector.sites.base import DiscoveryResult
from takuro_collector.sites.kinoshita import KinoshitaAdapter


def candidate(source_id: str, *, rent: int = 59000, with_photo: bool = False) -> PropertyCandidate:
    return PropertyCandidate(
        source_site="kinoshita-chintai.com",
        source_property_id=source_id,
        management_company="株式会社木下の賃貸",
        building_name=f"Building {source_id}",
        room="102",
        prefecture="東京都",
        address="東京都八王子市廿里町21-1",
        source_url=f"https://kinoshita-chintai.com/details/{source_id}details.html",
        rent=rent,
        management_fee=3500,
        layout="1K",
        area=26.08,
        photo_sources=([{"url": f"https://kinoshita-chintai.com/photos/{source_id}.jpg"}]
                       if with_photo else []),
    )


def inventory_item(source_id: str, *, rent: int = 59000) -> dict:
    url = f"https://kinoshita-chintai.com/details/{source_id}details.html"
    return {
        "url": url,
        "source_url": url,
        "source_site": "kinoshita-chintai.com",
        "source_property_id": source_id,
        "management_company": "株式会社木下の賃貸",
        "building_name": f"Building {source_id}",
        "room": "102",
        "address": "東京都八王子市廿里町21-1",
        "prefecture": "東京都",
        "change_facts": {
            "rent": rent,
            "management_fee": 3500,
            "layout": "1K",
            "area": 26.08,
        },
    }


class InventoryKinAdapter(KinoshitaAdapter):
    def __init__(self, items):
        self.items = items
        self.detail_calls = []

    def discover(self, _fetcher):
        urls = [item["url"] for item in self.items]
        mapped = {item["url"]: item for item in self.items}
        return DiscoveryResult(
            urls,
            hints=mapped,
            listed_count=len(urls),
            inventory_complete=True,
            inventory_site="kinoshita-chintai.com",
            inventory_items=mapped,
        )

    def collect_url(self, _fetcher, url):
        self.detail_calls.append(url)
        item = next(item for item in self.items if item["url"] == url)
        return candidate(item["source_property_id"], rent=item["change_facts"]["rent"])


def run(monkeypatch, db, adapter, progress=None):
    monkeypatch.setattr(collector_module, "adapters", lambda: [adapter])
    return CollectorEngine(db).scan_all(progress)


def test_kin_new_room_runs_full_detail_collection(monkeypatch, tmp_path):
    db = Database(tmp_path / "db.sqlite")
    adapter = InventoryKinAdapter([inventory_item("new_1")])
    result = run(monkeypatch, db, adapter)
    assert adapter.detail_calls == [inventory_item("new_1")["url"]]
    assert result.new_count == 1
    assert result.detail_attempted == 1


def test_kin_recent_unchanged_skips_detail_and_photo_pipeline(monkeypatch, tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.upsert_property(candidate("keep_1"))
    adapter = InventoryKinAdapter([inventory_item("keep_1")])
    downloads = []
    monkeypatch.setattr(
        collector_module.PhotoManager,
        "download_for_property",
        lambda self, property_id: downloads.append(property_id),
    )
    messages = []
    result = run(monkeypatch, db, adapter, lambda _code, message, *_args: messages.append(message))
    assert adapter.detail_calls == []
    assert downloads == []
    assert result.existing_count == 1
    assert result.detail_attempted == 0
    assert any("기존/변화없음, 상세 생략" in message for message in messages)
    assert not any("신규, 상세 수집" in message for message in messages)


def test_kin_recent_unchanged_resumes_pending_photos_without_detail(monkeypatch, tmp_path):
    db = Database(tmp_path / "db.sqlite")
    property_id, _ = db.upsert_property(candidate("pending_1", with_photo=True))
    adapter = InventoryKinAdapter([inventory_item("pending_1")])
    downloads = []

    def download(_self, pid):
        downloads.append(pid)
        return {"downloaded": 1, "failed": 0}

    monkeypatch.setattr(collector_module.PhotoManager, "download_for_property", download)
    messages = []
    result = run(monkeypatch, db, adapter, lambda _code, message, *_args: messages.append(message))

    assert adapter.detail_calls == []
    assert downloads == [property_id]
    assert result.detail_attempted == 0
    assert result.existing_count == 1
    assert any("기존/변화없음, 미완료 사진 재개" in message for message in messages)


def test_kin_list_change_forces_detail_refresh_and_matching_log(monkeypatch, tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.upsert_property(candidate("changed_1", rent=59000))
    adapter = InventoryKinAdapter([inventory_item("changed_1", rent=60000)])
    messages = []
    result = run(monkeypatch, db, adapter, lambda _code, message, *_args: messages.append(message))
    assert len(adapter.detail_calls) == 1
    assert result.detail_attempted == 1
    assert any("변경 감지, 상세 재확인" in message for message in messages)


def test_kin_ttl_expired_refreshes_while_recent_room_skips(monkeypatch, tmp_path):
    db = Database(tmp_path / "db.sqlite")
    old_id, _ = db.upsert_property(candidate("old_1"))
    db.upsert_property(candidate("recent_1"))
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat(timespec="seconds")
    db.conn.execute("UPDATE properties SET last_seen_at=? WHERE id=?", (old, old_id))
    db.conn.commit()
    adapter = InventoryKinAdapter([inventory_item("old_1"), inventory_item("recent_1")])
    messages = []
    result = run(monkeypatch, db, adapter, lambda _code, message, *_args: messages.append(message))
    assert adapter.detail_calls == [inventory_item("old_1")["url"]]
    assert result.detail_attempted == 1
    assert result.existing_count == 2
    assert any("기존, 정기 재확인" in message for message in messages)


def test_kin_real_list_fixture_exposes_only_reliable_change_facts():
    html = (Path(__file__).with_name("fixtures") / "kin_tokyo_page1.html").read_text(encoding="utf-8")
    _total, rows = KinoshitaAdapter._parse_result_page(html)
    assert rows[0]["source_property_id"] == "9449_2"
    assert rows[0]["change_facts"] == {
        "rent": 59000,
        "management_fee": 3500,
        "layout": "1K",
        "area": 26.08,
    }


def test_kin_complete_inventory_snapshot_keeps_all_rooms_when_details_skip(monkeypatch, tmp_path):
    db = Database(tmp_path / "db.sqlite")
    items = [inventory_item(f"room_{index}") for index in range(441)]
    for item in items:
        db.upsert_property(candidate(item["source_property_id"]))
    adapter = InventoryKinAdapter(items)
    result = run(monkeypatch, db, adapter)
    snapshot = db.pending_inventory_snapshots()[0]
    assert snapshot["expected_count"] == 441
    assert len(snapshot["items"]) == 441
    assert adapter.detail_calls == []
    assert result.existing_count == 441
    assert result.detail_attempted == 0


def test_kin_identity_precheck_is_source_site_plus_property_id(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.upsert_property(candidate("same_1"))
    indexed = db.properties_by_source_site("kinoshita-chintai.com")
    assert set(indexed) == {"same_1"}
    assert indexed["same_1"]["source_site"] == "kinoshita-chintai.com"
