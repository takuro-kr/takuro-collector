from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from takuro_collector.db import Database
from takuro_collector.models import PropertyCandidate
from takuro_collector.package import attach_pdf, create_zip
from takuro_collector.txt_export import build_text
from takuro_collector.display import photo_state_label as _photo_state_label, property_status_label as _property_status_label, wp_state_label as _wp_state_label
from takuro_collector.wordpress import WordPressClient, WordPressError


def sample_prop() -> dict:
    return PropertyCandidate(
        source_site="kinoshita-chintai.com",
        source_property_id="11016_4",
        management_company="株式会社木下の賃貸",
        building_name="アムールライフタウン",
        room="201",
        prefecture="千葉県",
        address="千葉県習志野市鷺沼台3-16-16",
        source_url="https://kinoshita-chintai.com/details/11016_4details.html",
        rent=75000,
        management_fee=3000,
        deposit="0",
        area=44.12,
        layout="1LDK",
        built_date="2012/12",
        floor="2階",
        total_floors="2階",
        structure="木造",
        orientation="南西",
        move_in_date="2026年10月12日",
        transport=[
            {
                "line": "",
                "station": "",
                "walk_minutes": None,
                "raw": "京成本線「京成津田沼」駅 徒歩12分",
            }
        ],
        equipment=["プロパンガス", "オートロック"],
    ).to_dict()


def test_txt_preserves_kin_quoted_transport_from_raw():
    txt = build_text(sample_prop())
    assert "交通1" in txt
    assert "沿線名\n京成本線" in txt
    assert "駅名\n京成津田沼" in txt
    assert "駅より徒歩\n12分" in txt


def test_txt_preserves_unquoted_transport_from_raw():
    p = sample_prop()
    p["transport"] = [{"raw": "JR総武線 幕張本郷駅 徒歩18分"}]
    txt = build_text(p)
    assert "沿線名\nJR総武線" in txt
    assert "駅名\n幕張本郷" in txt
    assert "駅より徒歩\n18分" in txt


def test_txt_does_not_invent_transport_when_line_is_missing():
    p = sample_prop()
    p["transport"] = [{"raw": "京成津田沼駅 徒歩12分"}]
    txt = build_text(p)
    assert "\n交通1\n" not in txt


def test_zip_contains_transport_in_basic_text(tmp_path: Path, monkeypatch):
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
        candidate = PropertyCandidate(**sample_prop())
        pid, _ = db.upsert_property(candidate)
        with db.conn:
            db.conn.execute("DELETE FROM photos WHERE property_id=?", (pid,))
        pdf = tmp_path / "reins.pdf"
        pdf.write_bytes(b"%PDF-1.4\n% test\n%%EOF\n")
        attach_pdf(db, pid, pdf)
        out = create_zip(db, pid)
        with zipfile.ZipFile(out) as zf:
            txt = zf.read("基本情報.txt").decode("utf-8")
        assert "沿線名\n京成本線" in txt
        assert "駅名\n京成津田沼" in txt
    finally:
        db.close()


def test_korean_status_labels_are_readable():
    assert _photo_state_label("not_started") == "⚪ 미다운로드"
    assert _photo_state_label("ready") == "🟢 다운로드 완료"
    assert _photo_state_label("partial") == "🟡 일부 완료"
    assert _photo_state_label("error") == "🔴 다운로드 오류"
    assert _photo_state_label("no_sources") == "⚪ 사진 없음"
    assert _property_status_label("packaged") == "ZIP 완료"
    assert _wp_state_label("pending") == "대기"
    assert _wp_state_label("synced") == "🟢 동기화됨"


def test_wordpress_key_validation_gives_korean_message_before_http():
    with pytest.raises(WordPressError, match="64자리"):
        WordPressClient("https://homes.takuro.tech/kr", "연동 키 설명 문구")
    # exact 64-hex remains accepted
    client = WordPressClient("https://homes.takuro.tech/kr", "a" * 64)
    assert client.key == "a" * 64


def test_main_window_has_wider_left_panel_and_new_actions(tmp_path: Path, monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("TAKURO_COLLECTOR_HOME", str(tmp_path / "home"))
    from PySide6.QtWidgets import QApplication, QPushButton, QSplitter
    from takuro_collector.ui import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    try:
        splitters = win.findChildren(QSplitter)
        assert splitters
        splitter = splitters[0]
        assert splitter.widget(0).minimumWidth() >= 430
        labels = {b.text() for b in win.findChildren(QPushButton)}
        assert "매물명 복사" in labels
        assert "ZIP 폴더 열기" in labels
        assert "매물 폴더 열기" in labels
    finally:
        win.close()
