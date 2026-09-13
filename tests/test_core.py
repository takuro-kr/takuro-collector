from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

os.environ.setdefault("TAKURO_COLLECTOR_HOME", str(Path(__file__).parent / ".tmp_home"))

from takuro_collector.db import Database
from takuro_collector.models import PropertyCandidate
from takuro_collector.package import attach_pdf, create_zip
from takuro_collector.sites import adapter_for_url, adapters
from takuro_collector.txt_export import build_text
from takuro_collector.utils import display_address_to_chome, normalize_room
from takuro_collector.wordpress import WordPressSync


HTML = """
<html><head><title>GENOVIA西川口Ⅳ 0302号室 | 賃貸</title></head><body>
<h1>GENOVIA西川口Ⅳ 0302号室</h1>
<table>
<tr><th>物件名</th><td>GENOVIA西川口Ⅳ</td></tr>
<tr><th>部屋番号</th><td>0302号室</td></tr>
<tr><th>所在地</th><td>埼玉県川口市西川口4丁目2-12</td></tr>
<tr><th>賃料</th><td>89,000円</td></tr>
<tr><th>管理費</th><td>10,000円</td></tr>
<tr><th>敷金</th><td>0</td></tr>
<tr><th>礼金</th><td>1ヶ月</td></tr>
<tr><th>専有面積</th><td>25.0㎡</td></tr>
<tr><th>間取り</th><td>1K</td></tr>
<tr><th>築年月</th><td>2020年3月</td></tr>
<tr><th>構造</th><td>RC</td></tr>
<tr><th>所在階</th><td>3階</td></tr>
<tr><th>総階数</th><td>10階</td></tr>
<tr><th>向き</th><td>西</td></tr>
<tr><th>入居可能</th><td>2026年10月12日</td></tr>
<tr><th>交通</th><td>京浜東北線 西川口駅 徒歩8分</td></tr>
<tr><th>設備</th><td>オートロック、宅配ボックス、浴室乾燥機</td></tr>
</table>
<a href='/images/room01.jpg'><img src='/thumb/room01.jpg' alt='洋室'></a>
<img src='/images/madori.png' alt='間取り図'>
<img src='/images/map.png' alt='周辺地図'>
</body></html>
"""


def sample_candidate(url="https://ref.namiki-grp.co.jp/estate/building2327959/room6472696"):
    adapter = adapter_for_url(url)
    assert adapter is not None
    return adapter.parse(HTML, url)


def test_room_normalization():
    assert normalize_room("0505号室") == "505"
    assert normalize_room("A-05") == "A-05"
    assert display_address_to_chome("埼玉県川口市西川口4丁目2-12") == "埼玉県川口市西川口4丁目"


def test_all_site_url_detection_and_ids():
    cases = {
        "https://www.otoku-chintai.com/x/room107061105.html": "107061105",
        "https://www.skyc-chintai.jp/build-7004418/room-38756239.html": "38756239",
        "https://www.goodcomasset-gc.co.jp/bkndetail/2673365286/room101312034/": "101312034",
        "https://ref.namiki-grp.co.jp/estate/building2327959/room6472696": "6472696",
        "https://pm.am-bition.jp/rent/2387/22422": "22422",
        "https://kinoshita-chintai.com/details/13075_76details.html": "13075_76",
        "https://rent.syla.jp/a/room104008687.html": "104008687",
        "https://stageplan.es-ws.jp/es/rent/1113666967450000010150": "1113666967450000010150",
    }
    for url, expected in cases.items():
        a = adapter_for_url(url)
        assert a is not None, url
        assert a.source_id(url)[0] == expected


def test_generic_property_extraction():
    p = sample_candidate()
    assert p.building_name == "GENOVIA西川口IV" or p.building_name == "GENOVIA西川口Ⅳ"
    # NFKC normalizes roman numeral in helper paths but extractor keeps normalized text; both are acceptable.
    assert p.room == "302"
    assert p.prefecture == "埼玉県"
    assert p.rent == 89000
    assert p.management_fee == 10000
    assert p.area == 25.0
    assert p.layout == "1K"
    assert len(p.photo_sources) == 2
    assert any(x["kind"] == "floorplan" for x in p.photo_sources)
    assert not any("map.png" in x["url"] for x in p.photo_sources)


def test_db_idempotency(tmp_path: Path):
    db = Database(tmp_path / "collector.db")
    try:
        p = sample_candidate()
        pid1, new1 = db.upsert_property(p)
        pid2, new2 = db.upsert_property(p)
        assert pid1 == pid2
        assert new1 is True and new2 is False
        rows = db.list_properties()
        assert len(rows) == 1
        assert rows[0]["room"] == "302"
        assert len(db.photos(pid1)) == 2
    finally:
        db.close()


def test_txt_contract_avoids_unknown_blank_deposit():
    p = sample_candidate().to_dict()
    p["deposit"] = ""
    p["key_money"] = ""
    txt = build_text(p)
    assert "建物名\nGENOVIA" in txt
    assert "部屋番号\n302" in txt
    assert "賃料\n89,000円" in txt
    assert "管理費\n10,000円" in txt
    assert "\n敷金\n" not in txt
    assert "\n礼金\n" not in txt
    assert "交通1" in txt and "京浜東北線" in txt and "西川口" in txt


def test_zip_build(tmp_path: Path, monkeypatch):
    from takuro_collector import paths
    monkeypatch.setenv("TAKURO_COLLECTOR_HOME", str(tmp_path / "home"))
    # Patch document/export roots for deterministic test under Linux.
    monkeypatch.setattr(paths, "property_root", lambda: tmp_path / "properties")
    monkeypatch.setattr(paths, "export_root", lambda: tmp_path / "exports")
    # Modules imported the functions directly, so patch there too.
    import takuro_collector.photos as photos_mod
    import takuro_collector.package as pkg_mod
    monkeypatch.setattr(photos_mod, "property_root", lambda: tmp_path / "properties")
    monkeypatch.setattr(pkg_mod, "export_root", lambda: tmp_path / "exports")
    (tmp_path / "exports").mkdir(parents=True, exist_ok=True)

    db = Database(tmp_path / "db.sqlite")
    try:
        p = sample_candidate()
        pid, _ = db.upsert_property(p)
        # Clear photo source rows so the ZIP contract can be tested without network downloads.
        with db.conn:
            db.conn.execute("DELETE FROM photos WHERE property_id=?", (pid,))
        pdf = tmp_path / "reins.pdf"
        pdf.write_bytes(b"%PDF-1.4\n% test\n%%EOF\n")
        attach_pdf(db, pid, pdf)
        out = create_zip(db, pid)
        assert out.exists()
        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
            assert "基本情報.txt" in names
            assert "REINS.pdf" in names
    finally:
        db.close()


def test_wp_payload_excludes_photo_sources(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite")
    try:
        p = sample_candidate()
        pid, _ = db.upsert_property(p)
        row = db.property(pid)
        payload = WordPressSync.payload_from_row(row)
        assert payload["source_property_id"] == p.source_property_id
        assert payload["building_name"] == p.building_name
        assert "photo_sources" not in payload
    finally:
        db.close()

def test_wordpress_client_sends_scoped_header():
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from takuro_collector.wordpress import WordPressClient

    seen = {}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen['path'] = self.path
            seen['key'] = self.headers.get('X-Takuro-Collector-Key')
            body = json.dumps({'ok': True, 'version': '0.36.74', 'candidate_count': 0, 'ready_count': 0}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass

    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    try:
        key = 'a' * 64
        client = WordPressClient(f'http://127.0.0.1:{server.server_port}', key, timeout=3)
        data = client.status()
        assert data['version'] == '0.36.74'
        assert seen['path'] == '/wp-json/takuro-registration/v1/status'
        assert seen['key'] == key
    finally:
        thread.join(timeout=3)
        server.server_close()

KIN_HTML = """
<html><head><title>テストレジデンス 0405号室</title></head><body>
<h1>テストレジデンス 0405号室</h1>
<table>
<tr><th>物件名</th><td>テストレジデンス</td></tr>
<tr><th>部屋番号</th><td>0405号室</td></tr>
<tr><th>所在地</th><td>神奈川県横浜市神奈川区子安通3丁目123-4</td></tr>
<tr><th>家賃（管理費）</th><td>91,000円（8,000円）</td></tr>
<tr><th>敷金</th><td>0ヶ月</td></tr>
<tr><th>礼金</th><td>1ヶ月</td></tr>
<tr><th>専有面積</th><td>25.65㎡</td></tr>
<tr><th>間取り</th><td>1K</td></tr>
<tr><th>交通</th><td>京浜東北線 新子安駅 徒歩5分</td></tr>
</table>
</body></html>
"""


def test_kinoshita_combined_rent_fee_and_full_address():
    url = "https://kinoshita-chintai.com/details/13553_4details.html"
    adapter = adapter_for_url(url)
    assert adapter is not None and adapter.code == "KIN"
    p = adapter.parse(KIN_HTML, url)
    assert p.room == "405"
    assert p.address == "神奈川県横浜市神奈川区子安通3丁目123-4"
    assert p.prefecture == "神奈川県"
    assert p.rent == 91000
    assert p.management_fee == 8000
    assert p.source_property_id == "13553_4"
