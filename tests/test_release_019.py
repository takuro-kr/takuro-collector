from pathlib import Path
import zipfile

from takuro_collector.sites.kinoshita import KinoshitaAdapter
from takuro_collector.txt_export import build_text
from takuro_collector.db import Database
from takuro_collector.package import attach_pdf, create_zip

AMOUR_HTML = r"""
<html><body>
<h1>アムールライフタウン 201号室</h1>
<table>
<tr><th>住所</th><td>千葉県習志野市鷺沼台3-16-16</td><th>路線/バス会社</th><td>京成本線 /</td></tr>
<tr><th>駅名/停留所名</th><td>京成大久保駅 /</td><th>徒歩時間</th><td>京成大久保駅：14分 / 停留所：</td></tr>
<tr><th>賃料</th><td>75,000円</td><th>管理費</th><td>3,000円</td></tr>
<tr><th>敷金</th><td>無</td><th>礼金</th><td>賃料1ヵ月分</td></tr>
<tr><th>方角</th><td>南西</td><th>鍵交換費</th><td>27,500円 (税込)</td></tr>
<tr><th>消毒代</th><td>26,400円(税込)</td><th>入居日</th><td>2026/10/12</td></tr>
<tr><th>物件ID</th><td>11016＊4</td><th>備考</th><td>仲介手数料：不要</td></tr>
</table>
<img src="https://cdn.example.test/photo.jpg?w=640" alt="室内">
<img src="https://cdn.example.test/photo.jpg?w=1280" alt="室内">
</body></html>
"""
URL = "https://kinoshita-chintai.com/details/11016_4details.html"


def test_kin_4cell_key_money_and_transport():
    p = KinoshitaAdapter().parse(AMOUR_HTML, URL)
    assert p.deposit == "0"
    assert p.key_money == "1ヵ月"
    assert p.transport[0]["line"] == "京成本線"
    assert p.transport[0]["station"] == "京成大久保"
    assert p.transport[0]["walk_minutes"] == 14
    txt = build_text(p.to_dict())
    assert "礼金\n1ヵ月" in txt


def test_kin_preserves_extra_table_fields():
    p = KinoshitaAdapter().parse(AMOUR_HTML, URL)
    fields = p.collected_info["table_fields"]
    assert fields["礼金"] == ["賃料1ヵ月分"]
    assert fields["鍵交換費"] == ["27,500円 (税込)"]
    assert fields["消毒代"] == ["26,400円(税込)"]
    assert fields["備考"] == ["仲介手数料：不要"]


def test_kin_dedupes_resize_url_variants():
    p = KinoshitaAdapter().parse(AMOUR_HTML, URL)
    urls = [x["url"] for x in p.photo_sources if "photo.jpg" in x["url"]]
    assert len(urls) == 1


def test_zip_contains_basic_and_collected_files(tmp_path: Path, monkeypatch):
    from takuro_collector import package as package_mod
    from takuro_collector import paths
    from takuro_collector import photos as photos_mod

    monkeypatch.setenv("TAKURO_COLLECTOR_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(paths, "property_root", lambda: tmp_path / "properties")
    monkeypatch.setattr(paths, "export_root", lambda: tmp_path / "exports")
    monkeypatch.setattr(photos_mod, "property_root", lambda: tmp_path / "properties")
    monkeypatch.setattr(package_mod, "export_root", lambda: tmp_path / "exports")
    (tmp_path / "exports").mkdir(parents=True, exist_ok=True)

    db = Database(tmp_path / "collector.sqlite")
    try:
        candidate = KinoshitaAdapter().parse(AMOUR_HTML, URL)
        pid, _ = db.upsert_property(candidate)
        with db.conn:
            db.conn.execute("DELETE FROM photos WHERE property_id=?", (pid,))
        pdf = tmp_path / "reins.pdf"
        pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
        attach_pdf(db, pid, pdf)
        out = create_zip(db, pid)
        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
            assert {"基本情報.txt", "収集情報.txt", "収集情報.json"} <= names
            assert "礼金\n1ヵ月" in zf.read("基本情報.txt").decode("utf-8")
            raw = zf.read("収集情報.txt").decode("utf-8")
            assert "礼金\n賃料1ヵ月分" in raw
            assert "鍵交換費\n27,500円 (税込)" in raw
    finally:
        db.close()
