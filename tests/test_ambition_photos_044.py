from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from takuro_collector.fetcher import FetchResult
from takuro_collector.sites.ambition import (
    AmbitionAdapter,
    _building_photo_sources,
    _parent_building_url,
    _room_photo_sources,
)


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FixtureFetcher:
    def __init__(self, pages: dict[str, str], fail_parent: bool = False):
        self.pages = pages
        self.fail_parent = fail_parent
        self.calls: list[str] = []

    def fetch(self, url, *_args, **_kwargs):
        self.calls.append(url)
        if self.fail_parent and url.endswith("/"):
            raise RuntimeError("parent unavailable")
        return FetchResult(url, self.pages[url], 200, False)


@pytest.mark.parametrize(
    "room_url,room_fixture,building_url,building_fixture,total,kinds",
    [
        (
            "https://pm.am-bition.jp/rent/2677/35436",
            "amb_photo_room_35436.html",
            "https://pm.am-bition.jp/rent/2677/",
            "amb_photo_building_2677.html",
            24,
            {"interior": 14, "floorplan": 1, "exterior": 5, "common_area": 4},
        ),
        (
            "https://pm.am-bition.jp/rent/2527/33719",
            "amb_photo_room_33719.html",
            "https://pm.am-bition.jp/rent/2527/",
            "amb_photo_building_2527.html",
            19,
            {"interior": 14, "floorplan": 1, "exterior": 4},
        ),
        (
            "https://pm.am-bition.jp/rent/2585/34310",
            "amb_photo_room_34310.html",
            "https://pm.am-bition.jp/rent/2585/",
            "amb_photo_building_2585.html",
            4,
            {"floorplan": 1, "exterior": 2, "common_area": 1},
        ),
    ],
)
def test_ambition_combines_only_room_and_building_originals(
    room_url, room_fixture, building_url, building_fixture, total, kinds
):
    fetcher = FixtureFetcher({
        room_url: fixture(room_fixture),
        building_url: fixture(building_fixture),
    })
    item = AmbitionAdapter().collect_url(fetcher, room_url)

    assert fetcher.calls == [room_url, building_url]
    assert len(item.photo_sources) == total
    assert {kind: sum(p["kind"] == kind for p in item.photo_sources) for kind in kinds} == kinds
    assert all("/img/upload/rent_" in p["url"] for p in item.photo_sources)
    assert not any("/img/cache/" in p["url"] for p in item.photo_sources)
    assert not any(term in p["url"] for p in item.photo_sources for term in (
        "facebook.com", "google", "/recommend", "/facility", "/logo", "/vr", "/ad.",
    ))


def test_ambition_35436_preserves_order_and_rejects_other_room_and_building_ids():
    room_url = "https://pm.am-bition.jp/rent/2677/35436"
    building_url = "https://pm.am-bition.jp/rent/2677/"
    fetcher = FixtureFetcher({
        room_url: fixture("amb_photo_room_35436.html"),
        building_url: fixture("amb_photo_building_2677.html"),
    })
    photos = AmbitionAdapter().collect_url(fetcher, room_url).photo_sources

    assert [p["url"].rsplit("/", 1)[-1] for p in photos[:15]] == [f"{n:03}.jpg" for n in range(1, 16)]
    assert [p["url"].rsplit("/", 1)[-1] for p in photos[15:]] == [f"{n:03}.jpg" for n in range(1, 10)]
    assert [p["kind"] for p in photos[:15]] == ["interior"] * 14 + ["floorplan"]
    assert [p["kind"] for p in photos[15:]] == ["exterior"] * 5 + ["common_area"] * 4
    assert not any("/35431/" in p["url"] or "/9999/" in p["url"] for p in photos)


def test_ambition_floorplan_uses_cache_hash_not_album_position():
    html = """
    <div id="room_photo"><div id="room_photo_album">
      <div class="photo_view"><a href="/img/upload/rent_room/3/12345/plan.jpg"><img src="/img/cache/480x360_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg"></a></div>
      <div class="photo_view"><a href="/img/upload/rent_room/3/12345/room.jpg"><img src="/img/cache/480x360_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.jpg"></a></div>
    </div></div>
    <div id="side_roomplan"><img src="/img/cache/228x350_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg"></div>
    """
    photos = _room_photo_sources(
        BeautifulSoup(html, "html.parser"),
        "https://pm.am-bition.jp/rent/999/12345",
        "12345",
    )
    assert [p["kind"] for p in photos] == ["floorplan", "interior"]
    assert [p["url"].rsplit("/", 1)[-1] for p in photos] == ["plan.jpg", "room.jpg"]


def test_ambition_rejects_foreign_domain_and_mismatched_original_paths():
    html = """
    <div id="room_photo"><div id="room_photo_album">
      <div class="photo_view"><a href="https://evil.example/img/upload/rent_room/3/12345/a.jpg"><img></a></div>
      <div class="photo_view"><a href="/img/upload/rent_room/3/99999/b.jpg"><img></a></div>
      <div class="photo_view"><a href="/img/upload/rent_mansion/0/999/a.jpg"><img></a></div>
      <div class="photo_view"><a href="/img/upload/rent_mansion/0/888/b.jpg"><img></a></div>
    </div></div>
    """
    soup = BeautifulSoup(html, "html.parser")
    assert _room_photo_sources(soup, "https://pm.am-bition.jp/rent/999/12345", "12345") == []
    assert [p["url"] for p in _building_photo_sources(soup, "https://pm.am-bition.jp/rent/999/", "999")] == [
        "https://pm.am-bition.jp/img/upload/rent_mansion/0/999/a.jpg"
    ]


def test_ambition_building_photo_with_unknown_label_is_other():
    html = """
    <div id="room_photo"><div id="room_photo_album"><div class="photo_view">
      <a href="/img/upload/rent_mansion/0/999/roof.jpg"><img alt="屋上"></a>
    </div></div></div>
    """
    photos = _building_photo_sources(
        BeautifulSoup(html, "html.parser"),
        "https://pm.am-bition.jp/rent/999/",
        "999",
    )
    assert photos == [{
        "url": "https://pm.am-bition.jp/img/upload/rent_mansion/0/999/roof.jpg",
        "alt": "屋上",
        "kind": "other",
    }]


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://pm.am-bition.jp/rent/2677/35436?x=1#photo", "https://pm.am-bition.jp/rent/2677/"),
        ("http://pm.am-bition.jp/rent/2677/35436", None),
        ("https://evil.example/rent/2677/35436", None),
        ("https://pm.am-bition.jp/rent/x/35436", None),
        ("https://pm.am-bition.jp/rent/2677/x", None),
        ("https://pm.am-bition.jp/rent/2677/35436/extra", None),
    ],
)
def test_ambition_parent_url_is_strict_and_removes_query_fragment(url, expected):
    assert _parent_building_url(url) == expected


def test_ambition_parent_failure_keeps_room_photos():
    room_url = "https://pm.am-bition.jp/rent/2677/35436"
    building_url = "https://pm.am-bition.jp/rent/2677/"
    fetcher = FixtureFetcher(
        {room_url: fixture("amb_photo_room_35436.html"), building_url: fixture("amb_photo_building_2677.html")},
        fail_parent=True,
    )
    item = AmbitionAdapter().collect_url(fetcher, room_url)
    assert len(item.photo_sources) == 15
    assert [p["kind"] for p in item.photo_sources] == ["interior"] * 14 + ["floorplan"]
    assert item.scrape_warnings == ["AMB 건물 사진 수집 실패: parent unavailable"]
