import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QTableWidget

import takuro_collector.ui as ui
from takuro_collector.ui import MainWindow, PropertyTableItem, _room_sort_key, _text_sort_key


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    yield instance


def item(text, key, known=True, property_id=None):
    value = PropertyTableItem(text)
    value.set_sort_value(key, known=known)
    if property_id is not None:
        value.setData(Qt.UserRole, property_id)
    return value


def ordered_text(table, column=0):
    return [table.item(row, column).text() for row in range(table.rowCount())]


def test_rent_numeric_ascending_descending_and_unknown_last(app):
    table = QTableWidget(4, 1)
    for row, (text, key, known) in enumerate([
        ("53,000円", 53000, True), ("-", 0, False), ("126,000円", 126000, True), ("9,000円", 9000, True),
    ]):
        table.setItem(row, 0, item(text, key, known))

    PropertyTableItem.descending = False
    table.sortItems(0, Qt.AscendingOrder)
    assert ordered_text(table) == ["9,000円", "53,000円", "126,000円", "-"]

    PropertyTableItem.descending = True
    table.sortItems(0, Qt.DescendingOrder)
    assert ordered_text(table) == ["126,000円", "53,000円", "9,000円", "-"]


def test_management_fee_numeric_sort_and_blank_unknown(app):
    table = QTableWidget(4, 1)
    for row, (text, key, known) in enumerate([
        ("10,000円", 10000, True), ("", 0, False), ("4,000円", 4000, True), ("8,000円", 8000, True),
    ]):
        table.setItem(row, 0, item(text, key, known))
    PropertyTableItem.descending = False
    table.sortItems(0, Qt.AscendingOrder)
    assert ordered_text(table) == ["4,000円", "8,000円", "10,000円", ""]


def test_room_uses_natural_numeric_order_with_unknown_last(app):
    table = QTableWidget(5, 1)
    for row, text in enumerate(["101", "20", "9", "101A", "-"]):
        known = text != "-"
        table.setItem(row, 0, item(text, _room_sort_key(text if known else ""), known))
    PropertyTableItem.descending = False
    table.sortItems(0, Qt.AscendingOrder)
    assert ordered_text(table) == ["9", "20", "101", "101A", "-"]


@pytest.mark.parametrize(
    "values,expected",
    [
        (["アンビション", "アムス", "木下の賃貸"], ["アムス", "アンビション", "木下の賃貸"]),
        (["高松ハイム", "キャッスル・日野台", "プレデパルク３"], ["キャッスル・日野台", "プレデパルク３", "高松ハイム"]),
        (["東京都新宿区", "埼玉県川口市", "神奈川県横浜市"], ["埼玉県川口市", "東京都新宿区", "神奈川県横浜市"]),
    ],
)
def test_japanese_company_building_and_address_text_sort(app, values, expected):
    table = QTableWidget(len(values), 1)
    for row, value in enumerate(values):
        table.setItem(row, 0, item(value, _text_sort_key(value)))
    PropertyTableItem.descending = False
    table.sortItems(0, Qt.AscendingOrder)
    assert ordered_text(table) == expected


def test_state_sort_uses_internal_state_not_icon_label(app):
    table = QTableWidget(3, 1)
    rows = [("🟢 완료", "ready"), ("⚪ 없음", "none"), ("🔴 오류", "error")]
    for row, (label, state) in enumerate(rows):
        table.setItem(row, 0, item(label, state))
    PropertyTableItem.descending = False
    table.sortItems(0, Qt.AscendingOrder)
    assert ordered_text(table) == ["🔴 오류", "⚪ 없음", "🟢 완료"]


class Harness:
    _current_property_id = MainWindow._current_property_id
    _select_property_id = MainWindow._select_property_id
    _apply_property_sort = MainWindow._apply_property_sort
    sort_property_table = MainWindow.sort_property_table
    selected_property = MainWindow.selected_property
    open_source = MainWindow.open_source


class FakeDB:
    def __init__(self, properties):
        self.properties = properties

    def property(self, property_id):
        return self.properties[property_id]


def property_table(rows):
    table = QTableWidget(len(rows), 2)
    for row, (pid, rent) in enumerate(rows):
        table.setItem(row, 0, item(str(pid), pid, property_id=pid))
        table.setItem(row, 1, item(f"{rent:,}円", rent))
    return table


def test_header_click_toggles_and_selected_property_identity_follows_sorted_row(app, monkeypatch):
    harness = Harness()
    harness.table = property_table([(1, 126000), (2, 9000), (3, 53000)])
    harness.db = FakeDB({
        1: {"id": 1, "source_url": "https://example.test/1"},
        2: {"id": 2, "source_url": "https://example.test/2"},
        3: {"id": 3, "source_url": "https://example.test/3"},
    })
    harness._property_sort_column = None
    harness._property_sort_order = Qt.AscendingOrder
    harness.table.selectRow(0)  # property 1

    harness.sort_property_table(1)
    assert ordered_text(harness.table, 1) == ["9,000円", "53,000円", "126,000円"]
    assert harness.selected_property()["id"] == 1

    # Select the visually first row after sorting; actions must use its moved ID.
    harness.table.selectRow(0)
    assert harness.selected_property()["id"] == 2
    opened = []
    monkeypatch.setattr(ui, "open_url", opened.append)
    harness.open_source()
    assert opened == ["https://example.test/2"]

    harness.sort_property_table(1)
    assert ordered_text(harness.table, 1) == ["126,000円", "53,000円", "9,000円"]


def test_sort_remains_safe_after_row_add_and_table_rebuild(app):
    table = property_table([(1, 53000), (2, 9000)])
    PropertyTableItem.descending = False
    table.sortItems(1, Qt.AscendingOrder)

    table.insertRow(table.rowCount())
    table.setItem(2, 0, item("3", 3, property_id=3))
    table.setItem(2, 1, item("126,000円", 126000))
    table.sortItems(1, Qt.AscendingOrder)
    assert ordered_text(table, 1) == ["9,000円", "53,000円", "126,000円"]

    table.setRowCount(2)
    table.setItem(0, 0, item("4", 4, property_id=4))
    table.setItem(0, 1, item("80,000円", 80000))
    table.setItem(1, 0, item("5", 5, property_id=5))
    table.setItem(1, 1, item("40,000円", 40000))
    table.sortItems(1, Qt.AscendingOrder)
    assert ordered_text(table, 1) == ["40,000円", "80,000円"]
