from datetime import datetime, timedelta, timezone

import takuro_collector.collector as collector_module
from takuro_collector.collector import CollectorEngine
from takuro_collector.db import Database
from takuro_collector.models import PropertyCandidate
from takuro_collector.sites.ambition import AmbitionAdapter
from takuro_collector.sites.base import DiscoveryResult


def candidate(room_id: str, *, rent: int = 69000, photo=False) -> PropertyCandidate:
    return PropertyCandidate(
        source_site="pm.am-bition.jp", source_property_id=room_id,
        management_company="アンビション", building_name=f"Building {room_id}", room="101",
        prefecture="東京都", address="東京都新宿区", source_url=f"https://pm.am-bition.jp/rent/1/{room_id}",
        rent=rent, management_fee=4000, layout="1K", area=25.0, floor="1階",
        photo_sources=([{"url": f"https://pm.am-bition.jp/upload/rent_room/{room_id}/001.jpg",
                         "kind": "interior", "alt": ""}] if photo else []),
    )


def item(room_id: str, *, rent: int = 69000) -> dict:
    url = f"https://pm.am-bition.jp/rent/1/{room_id}"
    return {"source_property_id": room_id, "source_url": url,
            "change_facts": {"rent": rent, "management_fee": 4000, "layout": "1K", "area": 25.0, "floor": "1階"}}


class InventoryAdapter(AmbitionAdapter):
    def __init__(self, items): self.items, self.detail_calls = items, []
    def discover(self, _fetcher):
        urls = [v["source_url"] for v in self.items]
        mapped = {v["source_url"]: v for v in self.items}
        return DiscoveryResult(urls, listed_count=len(urls), inventory_complete=True,
                               inventory_site="pm.am-bition.jp", inventory_items=mapped)
    def collect_url(self, _fetcher, url):
        self.detail_calls.append(url)
        return candidate(url.rsplit("/", 1)[-1])


def run(monkeypatch, db, adapter):
    monkeypatch.setattr(collector_module, "adapters", lambda: [adapter])
    engine = CollectorEngine(db)
    return engine.scan_all()


def test_new_room_still_runs_full_collection(monkeypatch, tmp_path):
    db = Database(tmp_path / "db.sqlite")
    adapter = InventoryAdapter([item("200")])
    result = run(monkeypatch, db, adapter)
    assert adapter.detail_calls == ["https://pm.am-bition.jp/rent/1/200"]
    assert result.new_count == 1 and result.detail_attempted == 1


def test_unchanged_existing_skips_all_detail_building_and_photo_work(monkeypatch, tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.upsert_property(candidate("201", photo=True))
    adapter = InventoryAdapter([item("201")])
    downloads = []
    monkeypatch.setattr(collector_module.PhotoManager, "download_for_property",
                        lambda self, pid: downloads.append(pid))
    messages = []
    engine = CollectorEngine(db)
    monkeypatch.setattr(collector_module, "adapters", lambda: [adapter])
    result = engine.scan_all(lambda _c, message, *_args: messages.append(message))
    assert adapter.detail_calls == []
    assert downloads == []
    assert result.existing_count == 1 and result.detail_attempted == 0
    assert any("기존/변화없음, 상세 생략" in message for message in messages)


def test_listing_change_forces_detail_refresh(monkeypatch, tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.upsert_property(candidate("202", rent=69000))
    adapter = InventoryAdapter([item("202", rent=70000)])
    result = run(monkeypatch, db, adapter)
    assert len(adapter.detail_calls) == 1 and result.detail_attempted == 1


def test_changed_photo_identity_downloads_only_pending_work(monkeypatch, tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.upsert_property(candidate("206", rent=69000, photo=True))
    adapter = InventoryAdapter([item("206", rent=70000)])
    adapter.collect_url = lambda fetcher, url: candidate("206", rent=70000, photo=True)
    downloads = []
    monkeypatch.setattr(collector_module.PhotoManager, "local_photo_paths", lambda self, pid: [tmp_path / "old.jpg"])
    monkeypatch.setattr(collector_module.PhotoManager, "download_for_property",
                        lambda self, pid: downloads.append(pid) or {"failed": 0})
    run(monkeypatch, db, adapter)
    assert len(downloads) == 1


def test_ttl_expired_refreshes_but_recent_listing_skips(monkeypatch, tmp_path):
    db = Database(tmp_path / "db.sqlite")
    old_id, _ = db.upsert_property(candidate("203"))
    db.upsert_property(candidate("204"))
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat(timespec="seconds")
    db.conn.execute("UPDATE properties SET last_seen_at=? WHERE id=?", (old, old_id)); db.conn.commit()
    adapter = InventoryAdapter([item("203"), item("204")])
    result = run(monkeypatch, db, adapter)
    assert adapter.detail_calls == ["https://pm.am-bition.jp/rent/1/203"]
    assert result.detail_attempted == 1 and result.existing_count == 2


def test_complete_inventory_records_missing_room_without_detail_probe(monkeypatch, tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.upsert_property(candidate("gone"))
    adapter = InventoryAdapter([item("live")])
    run(monkeypatch, db, adapter)
    snapshot = db.pending_inventory_snapshots()[0]
    assert [x["source_property_id"] for x in snapshot["items"]] == ["live"]
    assert all("gone" not in url for url in adapter.detail_calls)


def test_source_site_and_property_id_identity_is_preserved(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.upsert_property(candidate("205"))
    indexed = db.properties_by_source_site("pm.am-bition.jp")
    assert set(indexed) == {"205"}
    assert indexed["205"]["source_site"] == "pm.am-bition.jp"


def test_amb_list_row_provides_safe_change_facts():
    adapter = AmbitionAdapter(); seed = adapter.seed_urls[0]
    html = '''<div class="item_room_table"><table class="check_table"><tr>
      <td></td><td>5階</td><td>2LDK/48.69m²</td>
      <td><strong class="price">229,000円</strong>/20,000円</td><td>なし / なし</td>
      <td><a href="/rent/2748/36664">物件詳細</a></td></tr></table></div>'''
    class F:
        def fetch(self, url, *_a, **_k):
            return type("R", (), {"url": url, "html": html if url == seed else "", "via_browser": False})()
    found = adapter.discover(F())
    facts = found.inventory_items[found.urls[0]]["change_facts"]
    assert facts == {"floor": "5階", "layout": "2LDK", "area": 48.69,
                     "rent": 229000, "management_fee": 20000}


def test_db_layout_suffix_does_not_create_false_change():
    now = datetime.now(timezone.utc)
    existing = {"rent": 229000, "management_fee": 20000, "layout": "2LDK 間取り内訳",
                "area": 48.69, "floor": "unreliable extracted text", "last_seen_at": now.isoformat()}
    assert AmbitionAdapter().existing_inventory_action(existing, item("36664", rent=229000), now=now) == "changed"
    comparable = item("36664", rent=229000)
    comparable["change_facts"].update({"layout": "2LDK", "area": 48.69, "management_fee": 20000})
    assert AmbitionAdapter().existing_inventory_action(existing, comparable, now=now) == "unchanged"
