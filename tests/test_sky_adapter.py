from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

import takuro_collector.collector as collector_module
from takuro_collector.collector import CollectorEngine
from takuro_collector.db import Database
from takuro_collector.fetcher import FetchResult
from takuro_collector.models import PropertyCandidate
from takuro_collector.sites.base import DiscoveryResult, ListingInactive
from takuro_collector.sites.sky import SKYAdapter, _exclude_facility_copies, _original_image


FIXTURES = Path(__file__).parent / "fixtures"
DETAIL_URL = "https://www.skyc-chintai.jp/build-6700506/room-25662706.html"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FixtureFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def fetch(self, url, *_args, **_kwargs):
        self.calls.append(url)
        value = self.pages[url]
        if isinstance(value, FetchResult):
            return value
        return FetchResult(url=url, html=value, status_code=200, via_browser=False)


def test_discovery_follows_pagination_and_only_reads_room_cards():
    adapter = SKYAdapter()
    page2 = "https://www.skyc-chintai.jp/search-result/page-2.html?page_disp=30"
    fetcher = FixtureFetcher({adapter.seed_urls[0]: fixture("sky_list_page1.html"), page2: fixture("sky_list_page2.html")})
    result = adapter.discover(fetcher)
    assert result.urls == [DETAIL_URL, "https://www.skyc-chintai.jp/build-1/room-200.html",
                           "https://www.skyc-chintai.jp/build-2/room-300.html"]
    assert all("999" not in url for url in result.urls)
    assert result.inventory_complete and result.listed_count == 3
    assert result.inventory_site == "skyc-chintai.jp"
    assert fetcher.calls == [adapter.seed_urls[0], page2]


def test_list_inventory_has_only_observed_change_signals():
    items, _, count = SKYAdapter._list_page(fixture("sky_list_page1.html"), SKYAdapter.seed_urls[0])
    assert count == 3
    assert items[0] == {"source_property_id": "25662706", "source_url": DETAIL_URL,
                        "change_facts": {"rent": 142400, "management_fee": 9600,
                                         "layout": "2LDK", "area": 51.13}}


def test_room_url_and_source_property_id_are_stable():
    adapter = SKYAdapter()
    assert adapter.is_detail_url(DETAIL_URL)
    assert adapter.source_id(DETAIL_URL) == ("25662706", "site")
    assert not adapter.is_detail_url("https://www.skyc-chintai.jp/build-6700506/")


def test_detail_fields_money_area_floor_date_equipment_and_management():
    item = SKYAdapter().parse(fixture("sky_detail_25662706.html"), DETAIL_URL)
    assert (item.building_name, item.room) == ("スカイルーチェ川口芝中田", "301")
    assert (item.rent, item.management_fee) == (142400, 9600)
    assert (item.deposit, item.key_money) == ("1ヶ月", "1ヶ月")
    assert (item.layout, item.area) == ("2LDK", 51.13)
    assert (item.built_date, item.floor, item.total_floors) == ("2024年3月", "3階", "3階")
    assert (item.orientation, item.move_in_date, item.structure) == ("南", "即入", "鉄骨")
    assert item.prefecture == "埼玉県" and item.management_company == "スカイコート"
    assert item.source_site == "skyc-chintai.jp" and item.source_property_id == "25662706"
    assert "浴室乾燥機" in item.equipment and "宅配ボックス" in item.equipment


def test_multiple_transport_preserves_order_and_raw_text():
    routes = SKYAdapter().parse(fixture("sky_detail_25662706.html"), DETAIL_URL).transport
    assert [(x["line"], x["station"], x["walk_minutes"]) for x in routes] == [
        ("JR京浜東北・根岸線", "蕨", 10), ("JR京浜東北・根岸線", "西川口", 25)]
    assert routes[0]["raw"].endswith("徒歩10分") and routes[1]["raw"].endswith("徒歩25分")


def test_gallery_uses_originals_scope_and_order_without_duplicates_or_surroundings():
    photos = SKYAdapter().parse(fixture("sky_detail_25662706.html"), DETAIL_URL).photo_sources
    assert [photo["kind"] for photo in photos] == ["interior", "exterior", "common_area", "floorplan"]
    assert [photo["url"] for photo in photos] == [
        "https://image.reblo.net/cl_img/room_other_img_1/room_4397_25662706_1.jpg",
        "https://image.reblo.net/cl_img/build_photo_img/build_4397_6700506.jpg",
        "https://image.reblo.net/cl_img/build_other_img_1/build_4397_6700506_1.jpg",
        "https://image.reblo.net/cl_img/room_layout_img/layout_4397_25662706.jpg",
    ]
    assert all("img_thumb.php" not in photo["url"] for photo in photos)


def test_surrounding_facilities_dom_is_excluded_without_brand_rules():
    photos = SKYAdapter().parse(fixture("sky_detail_25662706.html"), DETAIL_URL).photo_sources
    assert all("build_facility_img" not in photo["url"] for photo in photos)
    assert all("近隣スーパー" not in photo["alt"] for photo in photos)
    assert {photo["kind"] for photo in photos} >= {"interior", "exterior", "common_area"}


def test_separate_room_layout_is_collected_as_original_floorplan():
    photos = SKYAdapter().parse(fixture("sky_detail_25662706.html"), DETAIL_URL).photo_sources
    plans = [photo for photo in photos if photo["kind"] == "floorplan"]
    assert plans == [{"url": "https://image.reblo.net/cl_img/room_layout_img/layout_4397_25662706.jpg",
                      "alt": "間取", "kind": "floorplan"}]


def test_original_conversion_rejects_other_hosts_and_path_traversal():
    assert _original_image("https://image.reblo.net/img_thumb.php?x=1&f=./cl_img/a.jpg") == "https://image.reblo.net/cl_img/a.jpg"
    assert _original_image("https://image.reblo.net/img_thumbnail.php?w=240&dir=./cl_img/build_facility_img_1/&nm=facility.jpg") == "https://image.reblo.net/cl_img/build_facility_img_1/facility.jpg"
    assert not _original_image("https://evil.example/img_thumb.php?f=./cl_img/a.jpg")
    assert not _original_image("https://image.reblo.net/img_thumb.php?f=../secret")


def test_generic_gallery_copy_of_facility_is_excluded_by_exact_content_not_brand():
    html = fixture("sky_detail_25662706.html").replace(
        "</div>\n<section id=\"facilities\"",
        '<img alt="その他画像" data-src="https://image.reblo.net/img_thumb.php?x=640&amp;y=640&amp;f=./cl_img/room_other_img_2/room_4397_25662706_2.jpg"></div>\n<section id="facilities"',
    )
    soup = BeautifulSoup(html, "html.parser")
    photos = SKYAdapter().parse(html, DETAIL_URL).photo_sources
    content = {
        "https://image.reblo.net/cl_img/build_facility_img_1/build_facility_4397_6700506_1.jpg": b"same-facility-image",
        "https://image.reblo.net/cl_img/room_other_img_2/room_4397_25662706_2.jpg": b"same-facility-image",
    }
    filtered = _exclude_facility_copies(soup, photos, lambda url: content[url])
    assert all("room_other_img_2" not in photo["url"] for photo in filtered)
    assert any(photo["kind"] == "interior" for photo in filtered)


def test_ambiguous_generic_photo_is_retained_when_facility_comparison_fails():
    html = fixture("sky_detail_25662706.html").replace(
        "</div>\n<section id=\"facilities\"",
        '<img alt="その他画像" data-src="https://image.reblo.net/img_thumb.php?x=640&amp;y=640&amp;f=./cl_img/room_other_img_2/room_4397_25662706_2.jpg"></div>\n<section id="facilities"',
    )
    soup = BeautifulSoup(html, "html.parser")
    photos = SKYAdapter().parse(html, DETAIL_URL).photo_sources
    filtered = _exclude_facility_copies(soup, photos, lambda _url: (_ for _ in ()).throw(OSError("offline")))
    assert filtered == photos


@pytest.mark.parametrize("status", [404, 410])
def test_http_inactive_status_is_normal_skip(status):
    fetcher = FixtureFetcher({DETAIL_URL: FetchResult(DETAIL_URL, "gone", status)})
    with pytest.raises(ListingInactive):
        SKYAdapter().collect_url(fetcher, DETAIL_URL)


def test_redirect_from_room_to_building_is_normal_inactive():
    final = "https://www.skyc-chintai.jp/build-6700506/"
    fetcher = FixtureFetcher({DETAIL_URL: FetchResult(final, "<html>building</html>", 200)})
    with pytest.raises(ListingInactive):
        SKYAdapter().collect_url(fetcher, DETAIL_URL)


def _candidate(room_id="25662706", rent=142400):
    return PropertyCandidate(source_site="skyc-chintai.jp", source_property_id=room_id,
                             management_company="スカイコート", building_name="Sky", room="301",
                             prefecture="埼玉県", address="埼玉県川口市", source_url=DETAIL_URL,
                             rent=rent, management_fee=9600, layout="2LDK", area=51.13)


def _inventory_item(rent=142400):
    return {"source_property_id": "25662706", "source_url": DETAIL_URL,
            "change_facts": {"rent": rent, "management_fee": 9600, "layout": "2LDK", "area": 51.13}}


def test_existing_action_skips_recent_unchanged_and_refreshes_change_or_ttl():
    now = datetime.now(timezone.utc)
    existing = {**_candidate().to_dict(), "last_seen_at": now.isoformat()}
    adapter = SKYAdapter()
    assert adapter.existing_inventory_action(existing, _inventory_item(), now=now) == "unchanged"
    assert adapter.existing_inventory_action(existing, _inventory_item(143000), now=now) == "changed"
    existing["last_seen_at"] = (now - timedelta(days=31)).isoformat()
    assert adapter.existing_inventory_action(existing, _inventory_item(), now=now) == "ttl"


class InventorySKY(SKYAdapter):
    def __init__(self, item):
        self.item, self.detail_calls = item, []

    def discover(self, _fetcher):
        return DiscoveryResult([DETAIL_URL], listed_count=1, inventory_complete=True,
                               inventory_site="skyc-chintai.jp", inventory_items={DETAIL_URL: self.item})

    def collect_url(self, _fetcher, url):
        self.detail_calls.append(url)
        return _candidate()


def test_new_and_changed_collect_detail_but_unchanged_skips_detail_and_photos(monkeypatch, tmp_path):
    monkeypatch.setattr(collector_module.PhotoManager, "download_for_property",
                        lambda *_args: pytest.fail("unchanged photo pipeline must be skipped"))
    db = Database(tmp_path / "db.sqlite")
    db.upsert_property(_candidate())
    adapter = InventorySKY(_inventory_item())
    monkeypatch.setattr(collector_module, "adapters", lambda: [adapter])
    result = CollectorEngine(db).scan_all()
    assert adapter.detail_calls == [] and result.detail_attempted == 0 and result.existing_count == 1

    fresh_db = Database(tmp_path / "fresh.sqlite")
    new_adapter = InventorySKY(_inventory_item())
    monkeypatch.setattr(collector_module, "adapters", lambda: [new_adapter])
    monkeypatch.setattr(collector_module.PhotoManager, "download_for_property", lambda *_args: {"failed": 0})
    new_result = CollectorEngine(fresh_db).scan_all()
    assert new_adapter.detail_calls == [DETAIL_URL] and new_result.new_count == 1

    changed_adapter = InventorySKY(_inventory_item(143000))
    monkeypatch.setattr(collector_module, "adapters", lambda: [changed_adapter])
    CollectorEngine(db).scan_all()
    assert changed_adapter.detail_calls == [DETAIL_URL]


def test_complete_inventory_snapshot_is_preserved(monkeypatch, tmp_path):
    db = Database(tmp_path / "db.sqlite")
    adapter = InventorySKY(_inventory_item())
    monkeypatch.setattr(collector_module, "adapters", lambda: [adapter])
    monkeypatch.setattr(collector_module.PhotoManager, "download_for_property", lambda *_args: {"failed": 0})
    CollectorEngine(db).scan_all()
    snapshot = db.pending_inventory_snapshots()[0]
    assert snapshot["source_site"] == "skyc-chintai.jp"
    assert [item["source_property_id"] for item in snapshot["items"]] == ["25662706"]
