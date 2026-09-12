import os
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QRect, QTimer
from PySide6.QtWidgets import QApplication, QWidget

import takuro_collector.ui as ui_module
from takuro_collector.db import Database
from takuro_collector.sites.ambition import AmbitionAdapter
from takuro_collector.ui import MainWindow, SettingsDialog, TaskThread, _fit_rect_to_available, _fit_window_to_screen


def app():
    return QApplication.instance() or QApplication([])


def test_normal_saved_geometry_is_unchanged():
    available = QRect(0, 0, 1600, 900)
    rect = QRect(100, 80, 1200, 700)
    assert _fit_rect_to_available(rect, available) == rect


def test_fully_offscreen_geometry_is_moved_inside():
    assert _fit_rect_to_available(QRect(4000, -2000, 900, 600), QRect(0, 0, 1200, 800)) == QRect(300, 0, 900, 600)


def test_partially_offscreen_geometry_is_fully_clamped():
    assert _fit_rect_to_available(QRect(-100, 500, 800, 500), QRect(0, 0, 1200, 800)) == QRect(0, 300, 800, 500)


def test_oversized_geometry_is_limited_to_available_area():
    assert _fit_rect_to_available(QRect(-500, -500, 2000, 1200), QRect(20, 30, 1000, 700)) == QRect(20, 30, 1000, 700)


def test_window_fit_uses_qt_logical_available_geometry(monkeypatch):
    app()
    window = QWidget(); window.setGeometry(1500, -300, 1000, 900)
    screen = SimpleNamespace(availableGeometry=lambda: QRect(0, 0, 800, 600))
    monkeypatch.setattr(ui_module, "_preferred_screen", lambda *_args: screen)
    _fit_window_to_screen(window)
    assert QRect(0, 0, 800, 600).contains(window.frameGeometry())


def test_settings_buttons_remain_inside_small_available_geometry(monkeypatch, tmp_path):
    qt = app(); db = Database(tmp_path / "db.sqlite")
    monkeypatch.setattr(ui_module, "is_windows_startup_enabled", lambda: False)
    screen = SimpleNamespace(availableGeometry=lambda: QRect(0, 0, 640, 480))
    monkeypatch.setattr(ui_module, "_preferred_screen", lambda *_args: screen)
    dialog = SettingsDialog(db); dialog.show(); qt.processEvents()
    _fit_window_to_screen(dialog); qt.processEvents()
    assert QRect(0, 0, 640, 480).contains(dialog.frameGeometry())
    assert dialog.button_box.geometry().bottom() <= dialog.rect().bottom()
    dialog.close(); db.close()


def test_task_thread_emits_cancel_not_failure():
    qt = app(); events = []
    def work(progress):
        while True:
            progress("AMB", "page", 1, 1)
            time.sleep(.01)
    task = TaskThread(work)
    task.cancelled.connect(lambda message: events.append(("cancelled", message)))
    task.failed.connect(lambda message: events.append(("failed", message)))
    loop = QEventLoop(); task.finished.connect(loop.quit)
    task.start(); QTimer.singleShot(30, task.requestInterruption); QTimer.singleShot(2000, loop.quit); loop.exec()
    task.wait(1000); qt.processEvents()
    assert events and events[0][0] == "cancelled"
    assert not any(kind == "failed" for kind, _ in events)


def test_cancel_handler_unlocks_ui_without_failure_retry(monkeypatch):
    calls = []
    class Label:
        def setText(self, value): calls.append(("status", value))
    harness = SimpleNamespace(
        set_busy=lambda value: calls.append(("busy", value)), status=Label(),
        append_log=lambda value: calls.append(("log", value)), refresh_sites=lambda: None,
        refresh_wp_label=lambda: None, refresh_properties=lambda: None, task=object(),
        schedule_retry=lambda *_: calls.append(("retry", True)),
    )
    monkeypatch.setattr(ui_module.QTimer, "singleShot", lambda _ms, fn: fn())
    MainWindow._task_cancelled(harness, "사용자가 작업을 취소했습니다.")
    assert ("busy", False) in calls and ("status", "사용자 취소") in calls
    assert not any(kind == "retry" for kind, _ in calls)
    assert harness.task is None


def test_amb_pagination_checks_cancel_before_each_sequential_page():
    adapter = AmbitionAdapter(); first = adapter.seed_urls[0]; second = first + "/page:2"
    pages = {
        first: f'<div class="item_room_table"><table class="check_table"></table></div><a class="next" href="{second}">次へ</a>',
        second: '<div class="item_room_table"><table class="check_table"></table></div>',
    }
    calls = []
    class Fetcher:
        def fetch(self, url, *_args, **_kwargs):
            calls.append(url)
            return SimpleNamespace(url=url, html=pages.get(url, pages[first]), via_browser=False)
    class Cancelled(RuntimeError): pass
    def check(page):
        if page == 1: raise Cancelled()
    adapter.cancel_check = check
    try:
        adapter.discover(Fetcher())
    except Cancelled:
        pass
    assert calls == [first]


def test_cancel_path_does_not_contain_retry_or_timer_reset():
    import inspect
    source = inspect.getsource(MainWindow._task_cancelled)
    assert "schedule_retry" not in source
    assert "configure_timer" not in source
