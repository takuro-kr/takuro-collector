import json

import pytest

from takuro_collector.required_listing import RequiredListingError, homepage_payload


def sample_row():
    return {
        "source_site": "example.jp",
        "source_property_id": "102",
        "management_company": "관리회사",
        "source_url": "https://example.jp/rooms/102",
        "building_name": "ＧＥＮＯＶＩＡ西川口",
        "room": "102",
        "prefecture": "埼玉県",
        "address": "埼玉県川口市西青木3-5-27",
        "transport_json": json.dumps([{"line": "京浜東北線", "station": "西川口", "walk_minutes": 9}], ensure_ascii=False),
        "rent": 85500,
        "management_fee": 10000,
        "deposit": "0",
        "key_money": "0",
        "layout": "1K",
        "area": 25.51,
        "built_date": "2025-03",
        "floor": "1",
        "total_floors": "5",
        "orientation": "北",
        "move_in_date": "2026-10-early",
        "structure": "RC",
        "equipment_json": json.dumps(["都市ガス", "バス・トイレ別", "宅配ボックス"], ensure_ascii=False),
        "raw_payload": "ignored",
    }


def test_homepage_contract_contains_only_required_property_information_and_identity():
    payload = homepage_payload(sample_row())
    assert payload["building_name"] == "ＧＥＮＯＶＩＡ西川口"
    assert payload["room"] == "102"
    assert payload["total_monthly_cost"] == 95500
    assert payload["transport"][0]["walk_minutes"] == 9
    assert payload["layout"] == "1K"
    assert payload["area"] == 25.51
    assert payload["equipment"] == ["都市ガス", "バス・トイレ別", "宅配ボックス"]
    assert "raw_payload" not in payload
    assert "photo_sources" not in payload
    assert "collected_info" not in payload


def test_homepage_contract_rejects_missing_essential_values():
    row = sample_row()
    row["rent"] = 0
    with pytest.raises(RequiredListingError, match="월세"):
        homepage_payload(row)
