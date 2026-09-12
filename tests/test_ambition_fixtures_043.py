from pathlib import Path

import pytest

from takuro_collector.fetcher import FetchResult
from takuro_collector.sites.ambition import AmbitionAdapter


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    "property_id,building,room,rent,fee,deposit,key_money,layout,structure,routes",
    [
        ("34291", "ヴェール・ラヴニール", "201", 65000, 4000, "0", "0", "1R", "木造", 3),
        ("34310", "ヒルズプレミアム上板橋", "105", 69000, 4000, "1ヶ月", "1ヶ月", "1R", "木造", 3),
        ("36644", "アーバンパーク成増Ⅱ", "315", 140000, 15000, "0", "0", "1LDK", "RC(鉄筋コンクリート)", 3),
    ],
)
def test_ambition_minimal_real_shape_fixtures(
    property_id, building, room, rent, fee, deposit, key_money, layout, structure, routes
):
    html = (FIXTURES / f"amb_detail_{property_id}.html").read_text(encoding="utf-8")
    url = {
        "34291": "https://pm.am-bition.jp/rent/2591/34291",
        "34310": "https://pm.am-bition.jp/rent/2585/34310",
        "36644": "https://pm.am-bition.jp/rent/2650/36644",
    }[property_id]
    item = AmbitionAdapter().parse(html, url)
    assert (item.building_name, item.room) == (building, room)
    assert (item.rent, item.management_fee) == (rent, fee)
    assert (item.deposit, item.key_money) == (deposit, key_money)
    assert (item.layout, item.structure) == (layout, structure)
    assert len(item.transport) == routes
    assert all(set(route) == {"line", "station", "walk_minutes", "raw"} for route in item.transport)


def test_ambition_transport_preserves_source_order_and_values():
    html = (FIXTURES / "amb_detail_34310.html").read_text(encoding="utf-8")
    item = AmbitionAdapter().parse(html, "https://pm.am-bition.jp/rent/2585/34310")
    assert item.transport == [
        {"line": "東武鉄道東上線", "station": "上板橋", "walk_minutes": 10, "raw": "東武鉄道東上線/上板橋 徒歩10分"},
        {"line": "都営三田線", "station": "志村三丁目", "walk_minutes": 28, "raw": "都営三田線/志村三丁目 徒歩28分"},
        {"line": "東京メトロ有楽町線", "station": "平和台(東京)", "walk_minutes": 30, "raw": "東京メトロ有楽町線/平和台(東京) 徒歩30分"},
    ]


def test_ambition_list_discovers_only_ambition_detail_urls():
    html = (FIXTURES / "amb_list.html").read_text(encoding="utf-8")

    class Fetcher:
        timeout = 20

        def fetch(self, url, *_args, **_kwargs):
            return FetchResult(url, html, 200, True)

        class Session:
            def get(self, *_args, **_kwargs):
                raise RuntimeError("offline fixture")

        session = Session()

    result = AmbitionAdapter().discover(Fetcher())
    assert result.urls == [
        "https://pm.am-bition.jp/rent/2585/34310",
        "https://pm.am-bition.jp/rent/2650/36644",
    ]
