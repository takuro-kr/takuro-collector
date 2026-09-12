from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from takuro_collector.fetcher import FetchResult
from takuro_collector.sites.amm import AMMAdapter, _photo_sources


FIXTURES = Path(__file__).parent / "fixtures"
URLS = {
    "105835727": "https://www.otoku-chintai.com/line_c1_oc/station_oc225/bknsta_oc225020/x/room105835727.html",
    "103884330": "https://www.otoku-chintai.com/line_c1_oc/station_oc16/bknsta_oc16100/x/room103884330.html",
    "106601057": "https://www.otoku-chintai.com/line_c1_oc/station_oc181/bknsta_oc181020/x/room106601057.html",
}


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FixtureFetcher:
    def __init__(self, callback):
        self.callback = callback
        self.calls: list[str] = []

    def fetch(self, url, *_args, **_kwargs):
        self.calls.append(url)
        html = self.callback(url)
        return FetchResult(url, html, 200, False)


@pytest.mark.parametrize(
    "room_id,expected",
    [
        ("105835727", {
            "building_name": "プレデパルク３", "room": "505", "rent": 124000,
            "management_fee": 8000, "deposit": "1ヶ月", "key_money": "1ヶ月",
            "layout": "1LDK", "area": 33.62, "built_date": "2008年 3月",
            "floor": "5階", "total_floors": "6階建", "structure": "鉄筋コンクリート",
            "orientation": "南東", "move_in_date": "即時",
        }),
        ("103884330", {
            "building_name": "キャッスル・日野台", "room": "105", "rent": 33000,
            "management_fee": 4000, "deposit": "0ヶ月", "key_money": "0ヶ月",
            "layout": "1R", "area": 15.0, "built_date": "1989年 2月",
            "floor": "1階", "total_floors": "3階建", "structure": "鉄骨造",
            "orientation": "南", "move_in_date": "即時",
        }),
        ("106601057", {
            "building_name": "高松ハイム", "room": "403", "rent": 95000,
            "management_fee": 5000, "deposit": "0ヶ月", "key_money": "1ヶ月",
            "layout": "1R", "area": 28.61, "built_date": "1993年 11月",
            "floor": "4階", "total_floors": "6階建", "structure": "鉄筋コンクリート",
            "orientation": "西", "move_in_date": "2026年11月中旬",
        }),
    ],
)
def test_amm_parses_scoped_four_cell_summary_and_compound_fields(room_id, expected):
    item = AMMAdapter().parse(fixture(f"amm_detail_{room_id}.html"), URLS[room_id])
    for field, value in expected.items():
        assert getattr(item, field) == value
    assert item.source_property_id == room_id
    assert item.source_site == "otoku-chintai.com"
    assert item.prefecture == "東京都"
    assert item.wp_payload()["total_monthly_cost"] == item.rent + item.management_fee


def test_amm_other_vacancy_summary_never_overrides_current_room():
    item = AMMAdapter().parse(fixture("amm_detail_103884330.html"), URLS["103884330"])
    assert item.rent == 33000
    assert item.area == 15.0
    assert item.rent != 36000
    assert item.area != 19.0


def test_amm_preserves_multiple_transport_rows_as_structured_routes():
    first = AMMAdapter().parse(fixture("amm_detail_105835727.html"), URLS["105835727"])
    assert first.transport == [
        {"line": "東武東上線", "station": "大山", "walk_minutes": 11, "bus_minutes": None,
         "raw": "東武東上線 「 大山 」駅 徒歩11分"},
        {"line": "東武東上線", "station": "中板橋", "walk_minutes": 15, "bus_minutes": None,
         "raw": "東武東上線 「 中板橋 」駅 徒歩15分"},
    ]
    third = AMMAdapter().parse(fixture("amm_detail_106601057.html"), URLS["106601057"])
    assert [(x["line"], x["station"], x["walk_minutes"]) for x in third.transport] == [
        ("日比谷線", "入谷", 10), ("つくばエクスプレス", "浅草", 7),
    ]


def test_amm_unknown_transport_expression_keeps_only_raw():
    html = fixture("amm_detail_105835727.html").replace(
        '<li><a>東武東上線</a>「<a>大山</a>」駅 徒歩11分</li>',
        '<li>将来の新しい交通表現</li>',
    )
    item = AMMAdapter().parse(html, URLS["105835727"])
    assert item.transport[0] == {"raw": "将来の新しい交通表現"}


def test_amm_equipment_is_scoped_and_preserves_source_labels():
    item = AMMAdapter().parse(fixture("amm_detail_105835727.html"), URLS["105835727"])
    assert item.equipment == ["都市ガス", "バス・トイレ別", "浴室乾燥機", "オートロック", "宅配ボックス"]


def test_amm_rejects_url_and_dom_source_id_mismatch():
    with pytest.raises(ValueError, match="source property ID 불일치"):
        AMMAdapter().parse(fixture("amm_detail_105835727.html"), URLS["103884330"])


@pytest.mark.parametrize("missing", ["building", "room", "address", "rent", "fee", "layout", "area", "structure"])
def test_amm_validation_rejects_missing_core_fields(missing):
    html = fixture("amm_detail_105835727.html")
    replacements = {
        "building": ("<h1>プレデパルク３ 0505</h1>", "<h1></h1>"),
        "room": ("0505/5階/6階建", "/5階/6階建"),
        "address": ("東京都 板橋区 大山西町", ""),
        "rent": ("12.4万円", "-"),
        "fee": ("8,000円", ""),
        "layout": ("1LDK <span>LDK 10.1帖</span>", ""),
        "area": ("33.62㎡/-", "-/-"),
        "structure": ("マンション/鉄筋コンクリート", "マンション/"),
    }
    old, new = replacements[missing]
    with pytest.raises(ValueError, match="AMM 필수 매물 정보 누락"):
        AMMAdapter().parse(html.replace(old, new), URLS["105835727"])


def test_amm_photo_popup_keeps_only_current_room_original_quality_photos():
    photos = _photo_sources(fixture("amm_gallery_105835727.html"), "105835727")
    assert len(photos) == 22
    assert [p["url"].split("_")[1] for p in photos] == [str(n) for n in range(1, 23)]
    assert not any("182_136" in p["url"] or "999999999" in p["url"] for p in photos)
    assert not any("周辺" in p["alt"] for p in photos)
    assert [p["kind"] for p in photos[:3]] == ["exterior", "floorplan", "interior"]
    assert photos[3]["kind"] == "other"
    assert [p["kind"] for p in photos[18:]] == ["exterior", "common_area", "common_area", "common_area"]


def test_amm_collect_url_adds_gallery_without_common_downloader_changes():
    def pages(url):
        if "ajax/library" in url:
            return fixture("amm_gallery_105835727.html")
        return fixture("amm_detail_105835727.html")

    fetcher = FixtureFetcher(pages)
    item = AMMAdapter().collect_url(fetcher, URLS["105835727"])
    assert len(item.photo_sources) == 22
    assert fetcher.calls == [
        URLS["105835727"],
        "https://www.otoku-chintai.com/bkn/ajax/library/?roomId=105835727",
    ]


def test_amm_gallery_failure_keeps_valid_property_data():
    class FailingGallery(FixtureFetcher):
        def fetch(self, url, *_args, **_kwargs):
            self.calls.append(url)
            if "ajax/library" in url:
                raise RuntimeError("gallery unavailable")
            return FetchResult(url, fixture("amm_detail_105835727.html"), 200, False)

    item = AMMAdapter().collect_url(FailingGallery(lambda _: ""), URLS["105835727"])
    assert item.building_name == "プレデパルク３"
    assert item.photo_sources == []
    assert item.scrape_warnings == ["AMM 사진 gallery 수집 실패: gallery unavailable"]


def test_amm_list_discovery_follows_pager_and_deduplicates_scoped_links():
    def pages(url):
        return fixture("amm_list_page2.html" if parse_qs(urlsplit(url).query).get("pg") == ["2"] else "amm_list_page1.html")

    result = AMMAdapter().discover(FixtureFetcher(pages))
    assert len(result.urls) == 3
    assert [AMMAdapter().source_id(url)[0] for url in result.urls] == ["105835727", "105980261", "103884330"]
    assert result.listed_count == 3
    assert not any("evil.example" in url or "777777777" in url for url in result.urls)


def test_amm_discovery_can_exceed_100_details_and_follow_seven_pages():
    def page_html(url):
        page = int(parse_qs(urlsplit(url).query).get("pg", ["1"])[0])
        start = (page - 1) * 20
        rows = "".join(
            f'<tr><td class="detail btn"><a href="/x/room{100000000 + n}.html">detail</a></td></tr>'
            for n in range(start, start + 20)
        )
        next_link = (
            f'<a href="/search/index/?class%5B%5D=c1&amp;selectFlg=1&amp;pg={page + 1}">{page + 1}</a>'
            if page < 7 else ""
        )
        return (
            f'<div class="pager"><p>空き数 140 件</p><a href="?pg={max(1, page-1)}">prev</a>{next_link}</div>'
            f'<div class="list_area"><div class="list_detail2"><table>{rows}</table></div></div>'
        )

    fetcher = FixtureFetcher(page_html)
    result = AMMAdapter().discover(fetcher)
    assert len(fetcher.calls) == 7
    assert len(result.urls) == 140
    assert result.listed_count == 140


def test_amm_seed_preserves_all_selected_regions_without_duplicate_select_flag():
    query = parse_qs(urlsplit(AMMAdapter.seed_urls[0]).query)
    assert query["address[]"] == list(AMMAdapter.TARGET_ADDRESS_IDS)
    assert len(query["address[]"]) == 68
    assert query["class[]"] == ["c1"]
    assert query["selectFlg"] == ["1"]
