import json

from takuro_collector.sites.configured import load_configured_adapters


def test_configured_adapter_extracts_only_canonical_fields(tmp_path):
    config = {
        "sites": [{
            "code": "EX",
            "label": "Example",
            "domains": ["rent.example.jp"],
            "seed_urls": ["https://rent.example.jp/search"],
            "detail_patterns": [r"/rooms/(\d+)"],
            "selectors": {
                "building_name": "h1", "room": ".room", "address": ".address",
                "rent": ".rent", "management_fee": ".fee", "layout": ".layout",
                "area": ".area", "equipment": ".equipment li"
            }
        }]
    }
    path = tmp_path / "managed-sites.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    adapter = load_configured_adapters(path)[0]
    html = """
      <h1>ＧＥＮＯＶＩＡ西川口</h1><span class='room'>102</span>
      <span class='address'>埼玉県川口市西青木3-5-27</span>
      <span class='rent'>85,500円</span><span class='fee'>10,000円</span>
      <span class='layout'>1K</span><span class='area'>25.51㎡</span>
      <ul class='equipment'><li>都市ガス</li><li>宅配ボックス</li></ul>
    """
    item = adapter.parse(html, "https://rent.example.jp/rooms/102")
    payload = item.wp_payload()
    assert payload["building_name"] == "ＧＥＮＯＶＩＡ西川口"
    assert payload["room"] == "102"
    assert payload["total_monthly_cost"] == 95500
    assert payload["layout"] == "1K"
    assert payload["area"] == 25.51
    assert payload["equipment"] == ["都市ガス", "宅配ボックス"]
