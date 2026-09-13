from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image

import takuro_collector.collector as collector_module
import takuro_collector.photos as photos_module
from takuro_collector.collector import CollectorEngine
from takuro_collector.db import Database
from takuro_collector.fetcher import FetchResult
from takuro_collector.models import PropertyCandidate
from takuro_collector.photos import PhotoManager, _prepare_photo
from takuro_collector.sites.base import DiscoveryResult
from takuro_collector.sites.goodcom import GoodComAdapter, _photo_sources


FIXTURES = Path(__file__).parent / "fixtures"
DETAIL = "https://www.goodcomasset-gc.co.jp/bkndetail/100/room9001/"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FixtureFetcher:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def fetch(self, url, *_args, **kwargs):
        assert kwargs.get("browser_fallback") is False
        self.calls.append(url)
        return FetchResult(url, self.pages[url], 200, False)


def test_discovery_follows_pager_then_each_building_and_preserves_complete_inventory():
    adapter = GoodComAdapter()
    page2 = "https://www.goodcomasset-gc.co.jp/search/index/?class%5B%5D=c1&pg=2"
    b1 = "https://www.goodcomasset-gc.co.jp/bkndetail/100/"
    b2 = "https://www.goodcomasset-gc.co.jp/bkndetail/200/"
    fetcher = FixtureFetcher({
        adapter.seed_urls[0]: fixture("goodcom_search_page1.html"),
        page2: fixture("goodcom_search_page2.html"),
        b1: fixture("goodcom_building_100.html"),
        b2: fixture("goodcom_building_200.html"),
    })
    result = adapter.discover(fetcher)
    assert result.urls == [DETAIL, "https://www.goodcomasset-gc.co.jp/bkndetail/100/room9002/",
                           "https://www.goodcomasset-gc.co.jp/bkndetail/200/room9003/"]
    assert result.listed_count == 3 and result.inventory_complete
    assert result.inventory_site == "goodcomasset-gc.co.jp"
    assert fetcher.calls == [adapter.seed_urls[0], page2, b1, b2]
    assert all("9999" not in url for url in result.urls)


def test_inventory_merges_optional_visible_signals_without_inventing_missing_values():
    adapter = GoodComAdapter()
    page2 = "https://www.goodcomasset-gc.co.jp/search/index/?class%5B%5D=c1&pg=2"
    fetcher = FixtureFetcher({adapter.seed_urls[0]: fixture("goodcom_search_page1.html"),
                              page2: fixture("goodcom_search_page2.html"),
                              "https://www.goodcomasset-gc.co.jp/bkndetail/100/": fixture("goodcom_building_100.html"),
                              "https://www.goodcomasset-gc.co.jp/bkndetail/200/": fixture("goodcom_building_200.html")})
    items = adapter.discover(fetcher).inventory_items
    assert items[DETAIL]["change_facts"] == {"rent": 86500, "area": 23.85, "management_fee": 10000, "layout": "1K"}
    assert items["https://www.goodcomasset-gc.co.jp/bkndetail/100/room9002/"]["change_facts"] == {"rent": 95000, "area": 29.75}


def test_site_identity_and_management_contract_are_canonical():
    adapter = GoodComAdapter()
    assert (adapter.code, adapter.management_company) == ("GOO", "goo")
    assert adapter.source_id(DETAIL) == ("9001", "site")
    assert adapter.is_detail_url(DETAIL)


def test_detail_parses_labels_nbsp_multiple_transport_and_all_core_fields():
    item = GoodComAdapter().parse(fixture("goodcom_detail_9001.html"), DETAIL)
    assert (item.building_name, item.room, item.address) == ("GENOVIA船橋", "101", "千葉県船橋市夏見1-15")
    assert (item.rent, item.management_fee, item.deposit, item.key_money) == (86500, 10000, "0ヶ月", "0ヶ月")
    assert (item.layout, item.area, item.built_date) == ("1K", 23.85, "2026年6月(新築)")
    assert (item.floor, item.total_floors, item.orientation) == ("1階", "5階建", "北")
    assert (item.move_in_date, item.structure) == ("2026年08月01日予定", "鉄筋コンクリート")
    assert item.prefecture == "千葉県" and item.source_property_id == "9001"
    assert item.collected_info["site_property_number"] == "9001"
    assert [(x["line"], x["station"], x["walk_minutes"]) for x in item.transport] == [
        ("東葉高速鉄道", "東海神", 9), ("総武線", "船橋", 11)]
    assert "浴室乾燥機" in item.equipment and "都市ガス" in item.equipment


def test_gallery_scope_classification_reference_policy_and_wrong_room_rejection():
    item = GoodComAdapter().parse(fixture("goodcom_detail_9001.html"), DETAIL)
    assert [p["kind"] for p in item.photo_sources] == ["exterior", "floorplan", "reference_photo", "common_area"]
    assert len(item.photo_sources) == 4
    assert all("banner" not in p["url"] and "9999" not in p["url"] for p in item.photo_sources)
    assert "類似タイプ写真参考" in item.photo_sources[2]["alt"]


def test_two_photos_and_no_floorplan_are_valid():
    item = GoodComAdapter().parse(
        fixture("goodcom_detail_two_photos.html"),
        "https://www.goodcomasset-gc.co.jp/bkndetail/100/room9002/",
    )
    assert len(item.photo_sources) == 2
    assert all(photo["kind"] != "floorplan" for photo in item.photo_sources)


def test_photo_source_rejects_other_hosts_and_other_room_ids():
    html = fixture("goodcom_detail_9001.html").replace(
        "</div>\n<img", '<a class="js-gallery-thum" data-note="【室内】"><img src="https://cdn.img-asp.jp/bkn/9999_8_0_0_3.jpg"></a>'
        '<a class="js-gallery-thum" data-note="【室内】"><img src="https://evil.example/bkn/9001_8_0_0_3.jpg"></a></div>\n<img')
    from bs4 import BeautifulSoup
    assert len(_photo_sources(BeautifulSoup(html, "html.parser"), "9001")) == 4


def _candidate(rent=86500):
    return PropertyCandidate(source_site="goodcomasset-gc.co.jp", source_property_id="9001", management_company="goo",
                             building_name="GENOVIA船橋", room="101", prefecture="千葉県", address="千葉県船橋市",
                             source_url=DETAIL, rent=rent, management_fee=10000, layout="1K", area=23.85)


def _item(**facts):
    base = {"rent": 86500, "area": 23.85}
    base.update(facts)
    return {"source_property_id": "9001", "source_url": DETAIL, "change_facts": base}


def test_existing_precheck_uses_only_present_signals_and_30_day_ttl():
    now = datetime.now(timezone.utc)
    existing = {**_candidate().to_dict(), "last_seen_at": now.isoformat()}
    adapter = GoodComAdapter()
    assert adapter.existing_inventory_action(existing, _item(), now=now) == "unchanged"
    assert adapter.existing_inventory_action(existing, _item(rent=87000), now=now) == "changed"
    assert adapter.existing_inventory_action(existing, _item(management_fee=11000), now=now) == "changed"
    existing["last_seen_at"] = (now - timedelta(days=31)).isoformat()
    assert adapter.existing_inventory_action(existing, _item(), now=now) == "ttl"


class InventoryGOO(GoodComAdapter):
    def __init__(self, item):
        self.item, self.detail_calls = item, []

    def discover(self, _fetcher):
        return DiscoveryResult([DETAIL], listed_count=1, inventory_complete=True,
                               inventory_site="goodcomasset-gc.co.jp", inventory_items={DETAIL: self.item})

    def collect_url(self, _fetcher, url):
        self.detail_calls.append(url)
        return _candidate()


def test_unchanged_skips_detail_and_photo_while_new_and_changed_collect(monkeypatch, tmp_path):
    db = Database(tmp_path / "existing.sqlite")
    db.upsert_property(_candidate())
    adapter = InventoryGOO(_item())
    monkeypatch.setattr(collector_module, "adapters", lambda: [adapter])
    monkeypatch.setattr(collector_module.PhotoManager, "download_for_property",
                        lambda *_args: pytest.fail("unchanged must skip photo pipeline"))
    result = CollectorEngine(db).scan_all()
    assert adapter.detail_calls == [] and result.detail_attempted == 0

    fresh = Database(tmp_path / "new.sqlite")
    new_adapter = InventoryGOO(_item())
    monkeypatch.setattr(collector_module, "adapters", lambda: [new_adapter])
    monkeypatch.setattr(collector_module.PhotoManager, "download_for_property", lambda *_args: {"failed": 0})
    assert CollectorEngine(fresh).scan_all().new_count == 1
    assert new_adapter.detail_calls == [DETAIL]

    changed = InventoryGOO(_item(rent=87000))
    monkeypatch.setattr(collector_module, "adapters", lambda: [changed])
    CollectorEngine(db).scan_all()
    assert changed.detail_calls == [DETAIL]


def _image_bytes(size=(320, 200), color=(80, 120, 160)):
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, "JPEG")
    return output.getvalue()


def test_reference_footer_appends_below_source_but_normal_photo_does_not():
    raw = _image_bytes()
    normal, _ = _prepare_photo(raw, "jpg")
    reference, _ = _prepare_photo(raw, "jpg", reference_notice=True)
    with Image.open(io.BytesIO(normal)) as ordinary, Image.open(io.BytesIO(reference)) as marked:
        assert ordinary.size == (320, 200)
        assert marked.width == 320 and marked.height > 200


def test_photo_manager_marks_only_goo_reference_and_does_not_double_footer(monkeypatch, tmp_path):
    db = Database(tmp_path / "photos.sqlite")
    candidate = _candidate()
    candidate.photo_sources = [
        {"url": "https://cdn.img-asp.jp/bkn/9001_1_0_0_3.jpg", "alt": "外観", "kind": "exterior"},
        {"url": "https://cdn.img-asp.jp/bkn/9001_3_0_0_3.jpg", "alt": "類似タイプ写真参考", "kind": "reference_photo"},
    ]
    pid, _ = db.upsert_property(candidate)
    monkeypatch.setattr(photos_module, "property_root", lambda: tmp_path / "out")

    class Images:
        calls = 0
        def download(self, *_args, **_kwargs):
            self.calls += 1
            return _image_bytes(), "image/jpeg"

    fetcher = Images()
    PhotoManager(db, fetcher).download_for_property(pid)
    rows = db.photos(pid)
    with Image.open(rows[0]["local_path"]) as exterior, Image.open(rows[1]["local_path"]) as reference:
        first_sizes = exterior.size, reference.size
        assert exterior.height == 200 and reference.height > 200
    PhotoManager(db, fetcher).download_for_property(pid)
    assert fetcher.calls == 2
    with Image.open(rows[1]["local_path"]) as reference:
        assert reference.size == first_sizes[1]


def test_non_goo_site_never_gets_reference_footer_even_if_kind_matches(monkeypatch, tmp_path):
    db = Database(tmp_path / "other.sqlite")
    candidate = _candidate()
    candidate.source_site = "example.test"
    candidate.source_url = "https://example.test/room/1"
    candidate.photo_sources = [{"url": "https://example.test/a.jpg", "alt": "reference", "kind": "reference_photo"}]
    pid, _ = db.upsert_property(candidate)
    monkeypatch.setattr(photos_module, "property_root", lambda: tmp_path / "out-other")
    class Images:
        def download(self, *_args, **_kwargs): return _image_bytes(), "image/jpeg"
    PhotoManager(db, Images()).download_for_property(pid)
    with Image.open(db.photos(pid)[0]["local_path"]) as image:
        assert image.height == 200
