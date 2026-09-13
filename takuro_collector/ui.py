from __future__ import annotations

import os
import re
import sys
import time
import traceback
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QRect, QSettings, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QCloseEvent, QDesktopServices, QGuiApplication, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import APP_NAME, __version__
from .collector import CollectorEngine
from .db import Database
from .fetcher import Fetcher, open_login_browser
from .package import attach_pdf, create_zip
from .paths import data_root, database_path, export_root, log_dir
from .photos import PhotoManager
from .sites import adapters
from .utils import display_address_to_chome, open_path, open_url
from .wordpress import WordPressSync
from .autostart import is_windows_startup_enabled, set_windows_startup


from .display import photo_state_label as _photo_state_label, property_status_label as _property_status_label, wp_state_label as _wp_state_label


class PropertyTableItem(QTableWidgetItem):
    """QTableWidget item with a typed key and unknown-last ordering."""

    descending = False

    def __init__(self, text: str = ""):
        super().__init__(text)
        self._sort_key: Any = ""
        self._sort_known = True

    def set_sort_value(self, value: Any, *, known: bool = True) -> None:
        self._sort_key = value
        self._sort_known = bool(known)

    def __lt__(self, other) -> bool:
        if isinstance(other, PropertyTableItem):
            if self._sort_known != other._sort_known:
                # Qt reverses the comparator for descending order. Flip this
                # boundary too so unknown values remain at the bottom.
                if self.descending:
                    return not self._sort_known
                return self._sort_known
            return self._sort_key < other._sort_key
        return super().__lt__(other)


def _text_sort_key(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold()


def _room_sort_key(value: Any) -> tuple:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in re.split(r"(\d+)", text)
        if part
    )


def _package_state_label(prop: dict) -> str:
    state = str(prop.get("wp_package_state") or "").strip().lower()
    if state in {"uploaded", "ready", "property_match_ready", "accepted", "queued"}:
        return "🟢 ZIP 업로드"
    if state in {"draft_created", "completed", "draft_complete"}:
        return "🟢 Draft 생성"
    if state in {"needs_review", "needs_configuration"}:
        return "🟡 확인 필요"
    if state in {"error", "failed"}:
        return "🔴 업로드 오류"
    return _wp_state_label(str(prop.get("wp_sync_state") or "pending"))

class TaskCancelled(RuntimeError):
    user_cancelled = True


def _fit_rect_to_available(rect: QRect, available: QRect) -> QRect:
    width = min(max(1, rect.width()), max(1, available.width()))
    height = min(max(1, rect.height()), max(1, available.height()))
    x = min(max(rect.x(), available.x()), available.x() + available.width() - width)
    y = min(max(rect.y(), available.y()), available.y() + available.height() - height)
    return QRect(x, y, width, height)


def _preferred_screen(window, parent=None):
    if parent is not None:
        handle = parent.windowHandle()
        if handle and handle.screen():
            return handle.screen()
        screen = QGuiApplication.screenAt(parent.frameGeometry().center())
        if screen:
            return screen
    rect, screens = window.frameGeometry(), QGuiApplication.screens()
    if screens:
        def overlap(screen):
            intersection = rect.intersected(screen.availableGeometry())
            return intersection.width() * intersection.height()
        best = max(screens, key=overlap)
        if overlap(best) > 0:
            return best
    return QGuiApplication.primaryScreen()


def _fit_window_to_screen(window, parent=None) -> None:
    if window.isMaximized() or window.isFullScreen():
        return
    screen = _preferred_screen(window, parent)
    if screen:
        frame = window.frameGeometry()
        fitted = _fit_rect_to_available(frame, screen.availableGeometry())
        frame_extra_width = max(0, frame.width() - window.width())
        frame_extra_height = max(0, frame.height() - window.height())
        window.resize(max(1, fitted.width() - frame_extra_width), max(1, fitted.height() - frame_extra_height))
        window.move(fitted.topLeft())
        # QWidget.move() and native frame origins can differ by the DPI-scaled
        # decoration margins. Correct once using Qt's reported frame geometry.
        actual = window.frameGeometry()
        window.move(window.x() + fitted.x() - actual.x(), window.y() + fitted.y() - actual.y())


class TaskThread(QThread):
    progress = Signal(str, str, int, int)
    result = Signal(object)
    failed = Signal(str)
    cancelled = Signal(str)

    def __init__(self, fn: Callable[[Callable[[str, str, int, int], None]], Any], parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self) -> None:
        try:
            # A large crawl can call progress hundreds/thousands of times. Emitting
            # every call floods Qt's queued-signal/event processing and makes Windows
            # report the GUI as hung even though the network work is in this thread.
            # Keep cancellation checks immediate, but coalesce UI progress to ~12 Hz.
            last_emit = 0.0
            pending: tuple[str, str, int, int] | None = None

            def report(a: str, b: str, c: int, d: int) -> None:
                nonlocal last_emit, pending
                if self.isInterruptionRequested():
                    raise TaskCancelled("사용자가 작업을 취소했습니다.")
                now = time.monotonic()
                pending = (a, b, c, d)
                if now - last_emit >= 0.08:
                    self.progress.emit(a, b, c, d)
                    last_emit = now
                    pending = None

            value = self.fn(report)
            if self.isInterruptionRequested():
                raise TaskCancelled("사용자가 작업을 취소했습니다.")
            if pending is not None:
                self.progress.emit(*pending)
            self.result.emit(value)
        except TaskCancelled as e:
            self.cancelled.emit(str(e))
        except Exception as e:
            self.failed.emit(f"{e}\n\n{traceback.format_exc(limit=4)}")


class UrlDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("URL 수동 수집")
        self.resize(700, 320)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("관리회사 매물 상세 URL을 한 줄에 하나씩 붙여넣으세요."))
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("https://...\nhttps://...")
        layout.addWidget(self.text)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("수집")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def urls(self) -> list[str]:
        return [x.strip() for x in self.text.toPlainText().splitlines() if x.strip()]




class PropertyDetailDialog(QDialog):
    def __init__(self, prop: dict, photo_count: int = 0, parent=None):
        super().__init__(parent)
        self.prop = prop
        self.setWindowTitle("매물 상세보기")
        self.resize(820, 760)
        outer = QVBoxLayout(self)
        form = QFormLayout()
        outer.addLayout(form)

        def val(key: str, default: str = "-") -> str:
            value = prop.get(key)
            if value is None or value == "":
                return default
            return str(value)

        money = lambda key: f"{int(prop.get(key) or 0):,}円" if int(prop.get(key) or 0) else "-"
        fields = [
            ("관리회사", val("management_company")),
            ("매물명", val("building_name")),
            ("호실", val("room")),
            ("전체 주소", val("address")),
            ("도도부현", val("prefecture")),
            ("월세", money("rent")),
            ("관리비", money("management_fee")),
            ("시키킹", val("deposit")),
            ("레이킹", val("key_money")),
            ("면적", f"{prop.get('area')}㎡" if prop.get("area") is not None else "-"),
            ("간토리", val("layout")),
            ("축년월", val("built_date")),
            ("층", val("floor")),
            ("전체층", val("total_floors")),
            ("구조", val("structure")),
            ("방향", val("orientation")),
            ("입주일", val("move_in_date")),
            ("사진 후보", str(photo_count)),
            ("사진 상태", _photo_state_label(str(prop.get("photo_state") or ""))),
            ("PDF", val("pdf_path")),
            ("ZIP", val("zip_path")),
            ("TAKURO", _package_state_label(prop)),
        ]
        for label, value in fields:
            widget = QLineEdit(value)
            widget.setReadOnly(True)
            widget.setCursorPosition(0)
            form.addRow(label, widget)

        # REINS 재입력용: 표 셀 선택이 아니라 일반 텍스트처럼 마우스로
        # 원하는 범위를 드래그해 복사할 수 있는 별도 영역을 제공한다.
        copy_text = QPlainTextEdit()
        copy_text.setReadOnly(True)
        copy_text.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        copy_text.setFocusPolicy(Qt.StrongFocus)
        copy_text.setMaximumHeight(180)
        copy_text.setToolTip("마우스로 원하는 글자를 드래그한 뒤 Ctrl+C로 복사할 수 있습니다.")
        copy_text.setPlainText(
            "매물명\t" + val("building_name")
            + "\n호실\t" + val("room")
            + "\n주소\t" + val("address")
            + "\n월세\t" + money("rent")
            + "\n관리비\t" + money("management_fee")
            + "\n敷金\t" + val("deposit")
            + "\n礼金\t" + val("key_money")
            + "\n입주일\t" + val("move_in_date")
        )
        form.addRow("REINS 복사용", copy_text)

        transport = prop.get("transport") or []
        equipment = prop.get("equipment") or []
        warnings = (prop.get("raw_payload") or {}).get("scrape_warnings", []) if isinstance(prop.get("raw_payload"), dict) else []
        extra = QPlainTextEdit()
        extra.setReadOnly(True)
        extra.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        extra.setPlainText(
            "교통\n" + ("\n".join(str(x.get("raw") if isinstance(x, dict) else x) for x in transport) or "-")
            + "\n\n설비\n" + (", ".join(str(x) for x in equipment) or "-")
            + "\n\n수집 경고\n" + ("\n".join(str(x) for x in warnings) or "없음")
            + "\n\n원본 URL\n" + val("source_url")
        )
        outer.addWidget(extra, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("닫기")
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)


class SettingsDialog(QDialog):
    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("TAKURO Collector 설정")
        self.resize(720, 640)
        outer = QVBoxLayout(self)
        tabs = QTabWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(tabs)
        outer.addWidget(scroll, 1)

        general = QWidget()
        form = QFormLayout(general)
        self.visible_browser = QCheckBox("수집 중 브라우저 화면 표시")
        self.visible_browser.setChecked(db.get_bool("visible_browser", False))
        self.auto_start = QCheckBox("프로그램 실행 후 자동으로 한 번 수집")
        self.auto_start.setChecked(db.get_bool("auto_collect_on_start", True))
        self.windows_start = QCheckBox("Windows 로그인 시 Collector 자동 실행")
        self.windows_start.setChecked(db.get_bool("start_with_windows", True) or is_windows_startup_enabled())
        self.interval = QSpinBox()
        self.interval.setRange(0, 1440)
        self.interval.setSuffix(" 분")
        self.interval.setSpecialValueText("사용 안 함")
        self.interval.setValue(int(db.get_setting("auto_collect_interval_minutes", "60") or 60))
        self.retry_interval = QSpinBox()
        self.retry_interval.setRange(1, 120)
        self.retry_interval.setSuffix(" 분")
        self.retry_interval.setValue(int(db.get_setting("auto_retry_interval_minutes", "10") or 10))
        from .updater import UPDATE_MANIFEST_URL
        manifest_url = db.get_setting("update_manifest_url", "").strip() or UPDATE_MANIFEST_URL
        self.update_manifest = QLineEdit(manifest_url)
        self.update_manifest.setPlaceholderText("https://.../update-manifest.json")
        form.addRow("브라우저", self.visible_browser)
        form.addRow("Windows 자동 실행", self.windows_start)
        form.addRow("시작 자동수집", self.auto_start)
        form.addRow("자동수집 간격", self.interval)
        form.addRow("실패 후 재시도", self.retry_interval)
        form.addRow("업데이트 정보 URL", self.update_manifest)
        note = QLabel("노트북 로그인 후 Collector가 자동으로 실행되어 수집하고 TAKURO에 동기화합니다. 노트북은 켜져 있고 인터넷에 연결되어 있어야 합니다.")
        note.setWordWrap(True)
        form.addRow(note)
        tabs.addTab(general, "일반")

        wp = QWidget()
        wpform = QFormLayout(wp)
        self.wp_enabled = QCheckBox("TAKURO WordPress 연동 사용")
        self.wp_enabled.setChecked(db.get_bool("wp_enabled", False))
        self.wp_site = QLineEdit(db.get_setting("wp_site_url", "https://homes.takuro.tech/kr"))
        self.wp_key = QLineEdit()
        self.wp_key.setEchoMode(QLineEdit.Password)
        self.wp_key.setPlaceholderText("이미 저장된 키는 표시하지 않습니다. 새 키를 넣을 때만 입력")
        self.wp_auto = QCheckBox("수집 완료 후 미전송 후보 자동 동기화")
        self.wp_auto.setChecked(db.get_bool("wp_auto_sync", True))
        self.test_button = QPushButton("연결 테스트")
        self.test_result = QLabel("")
        self.test_result.setWordWrap(True)
        wpform.addRow("사용", self.wp_enabled)
        wpform.addRow("WordPress 주소", self.wp_site)
        wpform.addRow("Collector 연동 키", self.wp_key)
        wpform.addRow("자동 동기화", self.wp_auto)
        wpform.addRow(self.test_button, self.test_result)
        hint = QLabel("Collector는 TXT와 사진만 후보 저장소로 전송합니다. REINS PDF는 WordPress 직원 전용 페이지에서 직접 업로드합니다. 연동 키는 신규매물의 Collector 연동 설정에서 발급합니다.")
        hint.setWordWrap(True)
        wpform.addRow(hint)
        self.translation_key = QLineEdit()
        self.translation_key.setEchoMode(QLineEdit.Password)
        self.translation_key.setPlaceholderText("OpenAI API 키 · 기존 키는 표시하지 않습니다")
        self.translation_enabled = QCheckBox("WordPress에서 요청한 역·노선 번역 처리")
        self.translation_enabled.setChecked(db.get_bool("translation_enabled", False))
        wpform.addRow("번역 API 키", self.translation_key)
        wpform.addRow(self.translation_enabled)
        tabs.addTab(wp, "TAKURO 연동")
        self._test_task: TaskThread | None = None
        self.test_button.clicked.connect(self._test_wp)

        sites_tab = QWidget()
        sites_layout = QVBoxLayout(sites_tab)
        sites_layout.addWidget(QLabel("[수집 시작]에서 검색할 사이트를 선택하세요. 특정 매물 URL은 비활성 사이트라도 [URL 수집]으로 직접 수집할 수 있습니다."))
        self.import_sites_button = QPushButton("관리회사 설정 파일 가져오기")
        self.import_sites_button.clicked.connect(self._import_managed_sites)
        sites_layout.addWidget(self.import_sites_button)
        self.site_checks: dict[str, QCheckBox] = {}
        for adapter in adapters():
            chk = QCheckBox(f"{adapter.code} · {adapter.label} · {', '.join(adapter.domains)}")
            chk.setChecked(db.get_bool(f"site_enabled_{adapter.code}", True))
            self.site_checks[adapter.code] = chk
            sites_layout.addWidget(chk)
        sites_layout.addStretch(1)
        tabs.addTab(sites_tab, "수집 사이트")

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("저장")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self.button_box = buttons
        QTimer.singleShot(0, lambda: _fit_window_to_screen(self, parent))

    def _import_managed_sites(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "관리회사 설정 파일", "", "JSON (*.json)")
        if not path:
            return
        try:
            import json
            from pathlib import Path
            from .sites.configured import ConfiguredAdapter

            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            specs = payload.get("sites") if isinstance(payload, dict) else None
            if not isinstance(specs, list) or not specs:
                raise ValueError("sites 목록이 비어 있습니다.")
            configured = [ConfiguredAdapter(spec) for spec in specs if spec.get("enabled", True)]
            built_in_codes = {adapter.code for adapter in adapters() if adapter.__class__.__name__ != "ConfiguredAdapter"}
            duplicates = sorted({adapter.code for adapter in configured} & built_in_codes)
            if duplicates:
                raise ValueError("내장 관리회사 코드는 덮어쓸 수 없습니다: " + ", ".join(duplicates))
            target = data_root() / "managed-sites.json"
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            QMessageBox.information(self, "관리회사 설정", f"{len(configured)}개 관리회사 설정을 가져왔습니다. 다음 수집부터 적용됩니다.")
        except Exception as exc:
            QMessageBox.warning(self, "관리회사 설정 오류", str(exc))

    def _test_wp(self) -> None:
        if self._test_task and self._test_task.isRunning():
            return
        from .wordpress import WordPressClient
        key = self.wp_key.text().strip() or WordPressSync(self.db).key()
        site = self.wp_site.text().strip()
        self.test_button.setEnabled(False)
        self.test_result.setText("연결 확인 중…")

        def job(_progress):
            return WordPressClient(site, key).status()

        task = TaskThread(job, self)
        self._test_task = task

        def finish(data):
            self.test_button.setEnabled(True)
            self.test_result.setText(
                f"✅ 연결됨 · TAKURO {data.get('version','?')} · 후보 {data.get('candidate_count',0)} · 등록준비 {data.get('ready_count',0)}"
            )
            self._test_task = None

        def fail(message):
            self.test_button.setEnabled(True)
            self.test_result.setText(f"❌ {message.split(chr(10) + chr(10), 1)[0]}")
            self._test_task = None

        task.result.connect(finish)
        task.failed.connect(fail)
        task.start()

    def _save(self) -> None:
        self.db.set_bool("visible_browser", self.visible_browser.isChecked())
        self.db.set_bool("auto_collect_on_start", self.auto_start.isChecked())
        self.db.set_bool("start_with_windows", self.windows_start.isChecked())
        self.db.set_setting("auto_collect_interval_minutes", str(self.interval.value()))
        self.db.set_setting("auto_retry_interval_minutes", str(self.retry_interval.value()))
        self.db.set_setting("update_manifest_url", self.update_manifest.text().strip())
        set_windows_startup(self.windows_start.isChecked())
        WordPressSync(self.db).save_config(
            self.wp_enabled.isChecked(),
            self.wp_site.text().strip(),
            self.wp_key.text().strip(),
            self.wp_auto.isChecked(),
        )
        from .secrets import protect
        if self.translation_key.text().strip():
            self.db.set_setting("openai_translation_key", protect(self.translation_key.text().strip()))
        self.db.set_bool("translation_enabled", self.translation_enabled.isChecked())
        for code, chk in self.site_checks.items():
            self.db.set_bool(f"site_enabled_{code}", chk.isChecked())
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.db = Database()
        self._apply_automation_defaults()
        self._apply_kin_photo_order_refresh()
        self.task: TaskThread | None = None
        self._automatic_run = False
        self._property_refresh_generation = 0
        self._property_sort_column: int | None = None
        self._property_sort_order = Qt.AscendingOrder
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1450, 860)
        saved_geometry = QSettings().value("main_window_geometry")
        if saved_geometry:
            self.restoreGeometry(saved_geometry)
        self._build_ui()
        self.refresh_all()
        self.auto_timer = QTimer(self)
        self.auto_timer.timeout.connect(lambda: self.collect_all(automatic=True))
        self.wp_ready_timer = QTimer(self)
        self.wp_ready_timer.timeout.connect(self.poll_ready_packages)
        self.translation_timer = QTimer(self)
        self.translation_timer.timeout.connect(self.poll_translation)
        self.translation_timer.start(30000)
        self.configure_timer()
        if self.db.get_bool("auto_collect_on_start", False):
            QTimer.singleShot(5000, lambda: self.collect_all(automatic=True))
        if "--autostart" in sys.argv:
            QTimer.singleShot(0, self.showMinimized)
        else:
            QTimer.singleShot(0, lambda: _fit_window_to_screen(self))
        QTimer.singleShot(300, self.show_last_update_result)

    def show_last_update_result(self) -> None:
        from .update_diagnostics import mark_shown, read_state

        state = read_state()
        status = str(state.get("status") or "")
        if status not in {"failed", "rolled_back"} or bool(state.get("shown")):
            return
        phase = str(state.get("phase") or "unknown")
        error = str(state.get("error_message") or state.get("error") or "알 수 없는 오류")
        winerror = state.get("winerror")
        prefix = f"WinError {winerror}: " if winerror is not None and f"WinError {winerror}" not in error else ""
        summary = f"❌ 업데이트 {'롤백' if status == 'rolled_back' else '실패'} · {phase} · {prefix}{error}"
        self.append_log(summary)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("업데이트 실패" if status == "failed" else "업데이트 롤백")
        box.setText(f"단계: {phase}\n\n{prefix}{error}")
        log_path = Path(str(state.get("log_path") or ""))
        log_button = box.addButton("로그 열기", QMessageBox.ActionRole) if log_path.is_file() else None
        box.addButton(QMessageBox.Ok)
        box.exec()
        mark_shown()
        if log_button is not None and box.clickedButton() is log_button:
            open_path(log_path)

    def _apply_automation_defaults(self) -> None:
        if self.db.get_bool("automation_035_initialized", False):
            return
        self.db.set_bool("automation_035_initialized", True)
        self.db.set_bool("auto_collect_on_start", True)
        self.db.set_setting("auto_collect_interval_minutes", "60")
        self.db.set_setting("auto_retry_interval_minutes", "10")
        self.db.set_bool("start_with_windows", True)
        self.db.set_bool("automation_paused", False)
        try:
            set_windows_startup(True)
        except OSError:
            pass

    def _apply_kin_photo_order_refresh(self) -> None:
        """Re-send existing KIN assets once with ordered ZIP entry names."""
        if self.db.get_bool("kin_photo_order_039_refreshed", False):
            return
        from .sites.kinoshita import KinoshitaAdapter
        now = datetime.now().isoformat(timespec="seconds")
        with self.db.conn:
            ids = [int(r[0]) for r in self.db.conn.execute(
                "SELECT id FROM properties WHERE source_site='kinoshita-chintai.com'"
            ).fetchall()]
            if ids:
                for pid in ids:
                    photos = [dict(row) for row in self.db.conn.execute(
                        "SELECT * FROM photos WHERE property_id=? ORDER BY sort_order,id", (pid,)
                    ).fetchall()]
                    for photo in photos:
                        photo["url"] = photo.get("source_url", "")
                        # Previous migrations guessed numbered slots. Discard guesses.
                        photo["kind"] = "floorplan" if "間取り" in str(photo.get("local_path", "")) else "photo"
                    ordered = KinoshitaAdapter._order_photo_sources(photos)
                    for sort_order, photo in enumerate(ordered):
                        self.db.conn.execute(
                            "UPDATE photos SET kind=?,sort_order=? WHERE id=?",
                            (photo.get("kind", "photo"), sort_order, int(photo["id"])),
                        )
                marks = ",".join("?" for _ in ids)
                self.db.conn.execute(
                    f"UPDATE properties SET wp_sync_state='pending',wp_asset_state='',wp_asset_error='',zip_path='',wp_package_state='pending' WHERE id IN ({marks})",
                    ids,
                )
                self.db.conn.executemany(
                    "INSERT INTO sync_queue(property_id,action,sync_status,retry_count,last_error,updated_at) VALUES(?,'upsert_candidate','pending',0,'',?) "
                    "ON CONFLICT(property_id,action) DO UPDATE SET sync_status='pending',retry_count=0,last_error='',updated_at=excluded.updated_at",
                    [(pid, now) for pid in ids],
                )
        self.db.set_bool("kin_photo_order_039_refreshed", True)

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)

        title_row = QHBoxLayout()
        title = QLabel(f"<b style='font-size:20px'>TAKURO Collector</b> <span style='color:#666'>v{__version__}</span>")
        title_row.addWidget(title)
        title_row.addStretch(1)
        self.wp_state = QLabel("WordPress: 확인 전")
        title_row.addWidget(self.wp_state)
        self.settings_btn = QPushButton("설정")
        self.settings_btn.clicked.connect(self.open_settings)
        title_row.addWidget(self.settings_btn)
        self.update_btn = QPushButton("업데이트 확인")
        self.update_btn.clicked.connect(self.check_for_updates)
        title_row.addWidget(self.update_btn)
        outer.addLayout(title_row)

        buttons = QHBoxLayout()
        self.collect_btn = QPushButton("수집 시작")
        self.collect_btn.setMinimumHeight(44)
        self.collect_btn.clicked.connect(self.collect_all)
        self.url_btn = QPushButton("URL 수집")
        self.url_btn.clicked.connect(self.collect_urls)
        self.sync_btn = QPushButton("TAKURO 동기화")
        self.sync_btn.clicked.connect(self.sync_wordpress)
        self.cancel_btn = QPushButton("작업 취소")
        self.cancel_btn.clicked.connect(self.cancel_task)
        self.cancel_btn.setEnabled(False)
        self.login_btn = QPushButton("브라우저 열기 / 로그인")
        self.login_btn.clicked.connect(self.open_login)
        self.refresh_btn = QPushButton("새로고침")
        self.refresh_btn.clicked.connect(self.refresh_all)
        for b in (self.collect_btn, self.url_btn, self.sync_btn, self.login_btn, self.refresh_btn, self.cancel_btn):
            buttons.addWidget(b)
        buttons.addStretch(1)
        outer.addLayout(buttons)

        automation_row = QHBoxLayout()
        self.automation_state = QLabel("")
        automation_row.addWidget(self.automation_state, 1)
        self.pause_btn = QPushButton("자동수집 일시정지")
        self.pause_btn.clicked.connect(self.toggle_automation)
        automation_row.addWidget(self.pause_btn)
        self.run_now_btn = QPushButton("지금 자동수집 실행")
        self.run_now_btn.clicked.connect(lambda: self.collect_all(automatic=True))
        automation_row.addWidget(self.run_now_btn)
        outer.addLayout(automation_row)

        self.status = QLabel("준비")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        status_row = QHBoxLayout()
        status_row.addWidget(self.status, 3)
        status_row.addWidget(self.progress, 2)
        outer.addLayout(status_row)

        splitter = QSplitter(Qt.Horizontal)
        outer.addWidget(splitter, 1)

        left = QWidget()
        left.setMinimumWidth(430)
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel("<b>사이트 상태</b>"))
        self.site_table = QTableWidget(0, 4)
        self.site_table.setHorizontalHeaderLabels(["사이트", "상태", "마지막 확인", "신규"])
        self.site_table.verticalHeader().setVisible(False)
        self.site_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.site_table.setSelectionMode(QTableWidget.SingleSelection)
        self.site_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.site_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.site_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.site_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        left_layout.addWidget(self.site_table, 1)
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumBlockCount(500)
        self.log_box.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.log_box.setMinimumHeight(210)
        left_layout.addWidget(QLabel("<b>작업 로그</b>"))
        left_layout.addWidget(self.log_box, 1)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("<b>수집 매물</b>"))
        search_row.addStretch(1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("건물명 · 호실 · 주소 · 관리회사 검색")
        self.search.returnPressed.connect(self.refresh_properties)
        search_row.addWidget(self.search)
        search_btn = QPushButton("검색")
        search_btn.clicked.connect(self.refresh_properties)
        search_row.addWidget(search_btn)
        right_layout.addLayout(search_row)

        self.table = QTableWidget(0, 11)
        self.table.setHorizontalHeaderLabels(["상태", "관리회사", "매물명", "호실", "주소", "월세", "관리비", "사진", "PDF", "ZIP", "TAKURO"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSortingEnabled(False)
        self.table.doubleClicked.connect(lambda _index: self.open_details())
        header = self.table.horizontalHeader()
        header.setSortIndicatorShown(False)
        header.sectionClicked.connect(self.sort_property_table)
        # ResizeToContents repeatedly scans a whole column as rows are inserted and
        # becomes very expensive around 500 rows. Stable widths keep refresh cost
        # linear while the two text-heavy columns continue to use remaining space.
        for col, width in {0: 95, 1: 150, 3: 75, 5: 90, 6: 90, 7: 100, 8: 75, 9: 75, 10: 120}.items():
            header.setSectionResizeMode(col, QHeaderView.Interactive)
            self.table.setColumnWidth(col, width)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        right_layout.addWidget(self.table, 1)

        # 메인 화면에서도 표 셀 단위가 아니라 글자 단위로 드래그 선택/복사 가능.
        self.copy_panel = QPlainTextEdit()
        self.copy_panel.setReadOnly(True)
        self.copy_panel.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        self.copy_panel.setFocusPolicy(Qt.StrongFocus)
        self.copy_panel.setMaximumHeight(115)
        self.copy_panel.setPlaceholderText("매물을 선택하면 여기에서 주소·매물명·호실·교통 등을 마우스로 드래그해 Ctrl+C로 복사할 수 있습니다.")
        self.table.itemSelectionChanged.connect(self.refresh_copy_panel)
        right_layout.addWidget(QLabel("<b>선택 매물 복사용 텍스트</b>"))
        right_layout.addWidget(self.copy_panel)

        actions = QHBoxLayout()
        action_defs = [
            ("상세보기", self.open_details),
            ("매물명 복사", self.copy_building_name),
            ("원본 열기", self.open_source),
            ("사진 다운로드", self.download_photos),
            ("PDF 추가", self.add_pdf),
            ("ZIP 만들기", self.make_zip),
            ("매물 폴더 열기", self.open_folder),
            ("ZIP 폴더 열기", self.open_export_folder),
            ("TAKURO ZIP 업로드 열기", self.open_wp_zip),
        ]
        for text, slot in action_defs:
            b = QPushButton(text)
            b.clicked.connect(slot)
            actions.addWidget(b)
        actions.addStretch(1)
        right_layout.addLayout(actions)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 8)
        splitter.setSizes([470, 980])

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.task and self.task.isRunning():
            if QMessageBox.question(self, "종료", "수집/동기화 작업이 진행 중입니다. 그래도 종료할까요?") != QMessageBox.Yes:
                event.ignore()
                return
        QSettings().setValue("main_window_geometry", self.saveGeometry())
        self.db.close()
        event.accept()

    def configure_timer(self) -> None:
        minutes = int(self.db.get_setting("auto_collect_interval_minutes", "0") or 0)
        self.auto_timer.stop()
        self.wp_ready_timer.stop()
        paused = self.db.get_bool("automation_paused", False)
        if minutes > 0 and not paused:
            self.auto_timer.start(minutes * 60 * 1000)
            next_run = datetime.now() + timedelta(minutes=minutes)
            self.automation_state.setText(f"자동수집 작동 중 · {minutes}분 간격 · 다음 실행 {next_run:%H:%M}")
            self.pause_btn.setText("자동수집 일시정지")
        elif paused:
            self.automation_state.setText("자동수집 일시정지됨")
            self.pause_btn.setText("자동수집 다시 시작")
        else:
            self.automation_state.setText("자동수집 사용 안 함")
            self.pause_btn.setText("자동수집 다시 시작")
        # WordPress now combines the already-staged TXT/photos with the PDF and
        # creates the draft server-side. No PDF polling/download loop is needed.
        self.wp_ready_timer.stop()

    def toggle_automation(self) -> None:
        paused = not self.db.get_bool("automation_paused", False)
        self.db.set_bool("automation_paused", paused)
        if not paused and int(self.db.get_setting("auto_collect_interval_minutes", "0") or 0) <= 0:
            self.db.set_setting("auto_collect_interval_minutes", "60")
        self.configure_timer()

    def schedule_retry(self, stage: str) -> None:
        if self.db.get_bool("automation_paused", False):
            return
        minutes = int(self.db.get_setting("auto_retry_interval_minutes", "10") or 10)
        self.automation_state.setText(f"{stage} 실패 · {minutes}분 후 자동 재시도")
        self.append_log(f"↻ {stage} 실패 · {minutes}분 후 자동 재시도")
        QTimer.singleShot(minutes * 60 * 1000, lambda: self.collect_all(automatic=True))

    def schedule_sync_retry(self) -> None:
        """Retry only durable WordPress queues; do not repeat site discovery."""
        if self.db.get_bool("automation_paused", False):
            return
        minutes = int(self.db.get_setting("auto_retry_interval_minutes", "10") or 10)
        self.automation_state.setText(f"TAKURO 전송 실패 · {minutes}분 후 전송만 재시도")
        self.append_log(f"↻ TAKURO 전송 실패 · {minutes}분 후 outbox/후보 전송만 재시도")
        QTimer.singleShot(minutes * 60 * 1000, lambda: self.sync_wordpress(automatic=True))

    @staticmethod
    def _compact_log_value(value: Any, *, depth: int = 0) -> Any:
        """Bound expensive result rendering before it reaches QPlainTextEdit."""
        if depth >= 2:
            if isinstance(value, (list, tuple, set, dict)):
                return f"<{type(value).__name__} {len(value)} items>"
            text = str(value)
            return text if len(text) <= 240 else text[:240] + "…"
        if isinstance(value, dict):
            out = {}
            for i, (key, item) in enumerate(value.items()):
                if i >= 16:
                    out["…"] = f"{len(value) - 16} keys omitted"
                    break
                out[key] = MainWindow._compact_log_value(item, depth=depth + 1)
            return out
        if isinstance(value, (list, tuple, set)):
            items = list(value)
            compact = [MainWindow._compact_log_value(x, depth=depth + 1) for x in items[:8]]
            if len(items) > 8:
                compact.append(f"… {len(items) - 8} items omitted")
            return compact
        text = str(value)
        return text if len(text) <= 500 else text[:500] + "…"

    def append_log(self, text: str) -> None:
        self.log_box.appendPlainText(text)

    def set_busy(self, busy: bool) -> None:
        # Keep harmless UI actions available so Windows keeps receiving normal
        # interaction/repaint traffic while a worker is busy. Only operations that
        # can start a second long-running task are disabled.
        for b in (self.collect_btn, self.url_btn, self.sync_btn, self.settings_btn):
            b.setEnabled(not busy)
        self.cancel_btn.setEnabled(busy)
        if not busy:
            self.cancel_btn.setText("작업 취소")
            self.progress.setValue(0)

    def cancel_task(self) -> None:
        if not self.task or not self.task.isRunning():
            return
        self.task.requestInterruption()
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setText("취소 요청됨")
        self.status.setText("현재 요청이 끝나는 즉시 안전하게 취소합니다…")
        self.append_log("⚠ 작업 취소 요청 · 현재 HTTP/페이지 처리 단위가 끝난 뒤 중단합니다.")

    def _task_cancelled(self, message: str) -> None:
        self.set_busy(False)
        self.status.setText("사용자 취소")
        self.append_log(f"⚠ {message}")
        self.refresh_sites()
        self.refresh_wp_label()
        QTimer.singleShot(0, self.refresh_properties)
        self.task = None

    def start_task(self, fn, *, done_message: str = "완료", after=None, on_fail=None, refresh_on_finish: bool = True, quiet_fail: bool = False) -> None:
        if self.task and self.task.isRunning():
            QMessageBox.information(self, "작업 중", "현재 작업이 끝난 후 다시 시도해 주세요.")
            return
        self.set_busy(True)
        self.task = TaskThread(fn, self)
        self.task.progress.connect(self.on_progress)

        def finish(value):
            self.set_busy(False)
            self.status.setText(done_message)
            self.append_log(f"✅ {done_message}: {self._compact_log_value(value)}")
            # Let Windows/Qt paint the completed state before rebuilding hundreds
            # of table cells.  This avoids a final UI stall after a large sync.
            if refresh_on_finish:
                self.refresh_sites()
                self.refresh_wp_label()
                QTimer.singleShot(0, self.refresh_properties)
            if after:
                after(value)
            self.task = None

        def fail(message):
            self.set_busy(False)
            self.status.setText("오류")
            self.append_log(f"❌ {message}")
            if not quiet_fail:
                QMessageBox.warning(self, "작업 오류", message.split("\n\n", 1)[0])
            self.refresh_sites()
            self.refresh_wp_label()
            QTimer.singleShot(0, self.refresh_properties)
            if on_fail:
                on_fail(message)
            self.task = None

        self.task.result.connect(finish)
        self.task.failed.connect(fail)
        self.task.cancelled.connect(self._task_cancelled)
        self.task.start()

    def on_progress(self, code: str, message: str, current: int, total: int) -> None:
        self.status.setText(f"{code} · {message}")
        if total:
            self.progress.setValue(min(100, max(0, int(current * 100 / total))))
        self.append_log(f"{code}: {message}")

    def collect_all(self, automatic: bool = False) -> None:
        if automatic and self.db.get_bool("automation_paused", False):
            return
        if self.task and self.task.isRunning():
            if automatic:
                self.append_log("↷ 자동수집 시각이지만 다른 작업이 진행 중이어서 이번 실행을 건너뜁니다.")
                return
        visible = self.db.get_bool("visible_browser", False)
        db_path = self.db.path

        def job(progress):
            db = Database(db_path)
            try:
                result = CollectorEngine(db, visible_browser=visible).scan_all(progress)
                return result.to_dict()
            finally:
                db.close()

        def after(value):
            if self.db.get_bool("wp_enabled", False) and self.db.get_bool("wp_auto_sync", True):
                QTimer.singleShot(300, lambda: self.sync_wordpress(automatic=automatic))
            else:
                self.db.set_setting("automation_last_success", datetime.now().isoformat(timespec="seconds"))
                self.configure_timer()

        self.start_task(
            job,
            done_message="전체 수집 완료",
            after=after,
            on_fail=(lambda _message: self.schedule_retry("수집")) if automatic else None,
            quiet_fail=automatic,
        )

    def collect_urls(self) -> None:
        dlg = UrlDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        urls = dlg.urls()
        if not urls:
            return
        visible = self.db.get_bool("visible_browser", False)
        db_path = self.db.path

        def job(progress):
            db = Database(db_path)
            try:
                return CollectorEngine(db, visible_browser=visible).collect_urls(urls, progress).to_dict()
            finally:
                db.close()

        self.start_task(job, done_message="URL 수집 완료")

    def selected_property(self) -> dict | None:
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "매물 선택", "먼저 매물 한 건을 선택해 주세요.")
            return None
        item = self.table.item(row, 0)
        if not item:
            return None
        pid = item.data(Qt.UserRole)
        return self.db.property(int(pid)) if pid else None

    def _current_property_id(self) -> int | None:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        value = item.data(Qt.UserRole) if item else None
        return int(value) if value else None

    def _select_property_id(self, property_id: int | None) -> None:
        if not property_id:
            return
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.data(Qt.UserRole) == property_id:
                self.table.selectRow(row)
                return

    def _apply_property_sort(self) -> None:
        if self._property_sort_column is None:
            return
        PropertyTableItem.descending = self._property_sort_order == Qt.DescendingOrder
        self.table.sortItems(self._property_sort_column, self._property_sort_order)

    def sort_property_table(self, column: int) -> None:
        selected_pid = self._current_property_id()
        if self._property_sort_column == column:
            self._property_sort_order = (
                Qt.DescendingOrder if self._property_sort_order == Qt.AscendingOrder else Qt.AscendingOrder
            )
        else:
            self._property_sort_column = column
            self._property_sort_order = Qt.AscendingOrder
        header = self.table.horizontalHeader()
        header.setSortIndicatorShown(True)
        header.setSortIndicator(column, self._property_sort_order)
        self._apply_property_sort()
        self._select_property_id(selected_pid)

    def refresh_all(self) -> None:
        self.refresh_sites()
        self.refresh_properties()
        self.refresh_wp_label()

    def refresh_sites(self) -> None:
        state = self.db.site_statuses()
        rows = adapters()
        self.site_table.setRowCount(len(rows))
        labels = {"ok": "● 정상", "login_required": "⚠ 로그인 필요", "error": "✕ 오류", "never": "- 미확인"}
        for r, adapter in enumerate(rows):
            s = state.get(adapter.code, {})
            values = [adapter.code, labels.get(str(s.get("status", "never")), str(s.get("status", "미확인"))), str(s.get("last_checked_at", ""))[:19], str(s.get("last_new_count", 0))]
            for c, value in enumerate(values):
                item = QTableWidgetItem(value)
                if c == 0:
                    item.setData(Qt.UserRole, adapter.code)
                self.site_table.setItem(r, c, item)

    def refresh_properties(self, batch_size: int = 40) -> None:
        """Refresh the property grid in small event-loop-friendly chunks.

        This path is used at startup and after collection. Avoid full JSON decoding,
        synchronous filesystem stats, and one giant 5,500-cell GUI transaction.
        """
        rows = self.db.list_property_summaries(self.search.text().strip(), 500)
        self._property_refresh_generation += 1
        generation = self._property_refresh_generation
        batch_size = max(20, int(batch_size))

        selected_pid = self._current_property_id()

        self.table.horizontalHeader().setSectionsClickable(False)
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        self.table.blockSignals(False)

        def update_batch(start: int = 0) -> None:
            if generation != self._property_refresh_generation:
                return
            end = min(len(rows), start + batch_size)
            self.table.setUpdatesEnabled(False)
            self.table.blockSignals(True)
            try:
                for r in range(start, end):
                    p = rows[r]
                    # A stored path is written only after a successful attach/package.
                    # Do not stat up to 1,000 files on the GUI thread every refresh.
                    pdf_ok = bool(str(p.get("pdf_path") or "").strip())
                    zip_ok = bool(str(p.get("zip_path") or "").strip())
                    values = [
                        _property_status_label(str(p.get("status", "new"))),
                        str(p.get("management_company", "")),
                        str(p.get("building_name", "")),
                        str(p.get("room", "")),
                        display_address_to_chome(str(p.get("address", ""))),
                        f"{int(p.get('rent') or 0):,}円" if int(p.get("rent") or 0) else "-",
                        f"{int(p.get('management_fee') or 0):,}円" if int(p.get("management_fee") or 0) else "-",
                        _photo_state_label(str(p.get("photo_state", "-"))),
                        "🟢 완료" if pdf_ok else "⚪ 없음",
                        "🟢 완료" if zip_ok else "⚪ 없음",
                        _package_state_label(p),
                    ]
                    raw_status = str(p.get("status", "new")).strip().lower()
                    raw_photo = str(p.get("photo_state", "-")).strip().lower()
                    raw_takuro = str(p.get("wp_package_state") or p.get("wp_sync_state") or "pending").strip().lower()
                    room_text = str(p.get("room", "")).strip()
                    rent = int(p.get("rent") or 0)
                    fee = int(p.get("management_fee") or 0)
                    sort_values = [
                        (raw_status, bool(raw_status)),
                        (_text_sort_key(values[1]), bool(values[1])),
                        (_text_sort_key(values[2]), bool(values[2])),
                        (_room_sort_key(room_text), bool(room_text)),
                        (_text_sort_key(values[4]), bool(values[4])),
                        (rent, rent > 0),
                        (fee, fee > 0),
                        (raw_photo, bool(raw_photo)),
                        (1 if pdf_ok else 0, True),
                        (1 if zip_ok else 0, True),
                        (raw_takuro, bool(raw_takuro)),
                    ]
                    for c, value in enumerate(values):
                        item = self.table.item(r, c)
                        if not isinstance(item, PropertyTableItem):
                            item = PropertyTableItem()
                            self.table.setItem(r, c, item)
                        item.setText(value)
                        item.set_sort_value(sort_values[c][0], known=sort_values[c][1])
                        if c == 0:
                            item.setData(Qt.UserRole, int(p["id"]))
                        elif c == 2:
                            item.setToolTip("더블클릭하면 로컬 상세보기를 엽니다. 원본 사이트는 아래 [원본 열기] 버튼으로 엽니다.")
                        elif c == 4:
                            item.setToolTip(str(p.get("address", "")))
            finally:
                self.table.blockSignals(False)
                self.table.setUpdatesEnabled(True)
                self.table.viewport().update()

            if end < len(rows):
                QTimer.singleShot(5, lambda: update_batch(end))
                return

            self._apply_property_sort()
            self._select_property_id(selected_pid)
            self.table.horizontalHeader().setSectionsClickable(True)
            self.refresh_copy_panel()

        QTimer.singleShot(0, update_batch)

    def refresh_property_states_chunked(self, batch_size: int = 40) -> None:
        """Refresh only state columns in small UI batches after WordPress sync.

        Full QTableWidget rebuilds across hundreds of rows can make Windows mark
        the app as not responding even though network work already finished.
        Keep the current rows/order and update only the five state columns,
        yielding to the event loop between batches.
        """
        rows = self.db.list_property_states(self.search.text().strip(), 500)
        by_id = {int(p["id"]): p for p in rows}
        total = self.table.rowCount()
        batch_size = max(10, int(batch_size))
        selected_pid = self._current_property_id()
        self.table.horizontalHeader().setSectionsClickable(False)

        def update_batch(start: int = 0) -> None:
            end = min(total, start + batch_size)
            self.table.setUpdatesEnabled(False)
            try:
                for r in range(start, end):
                    id_item = self.table.item(r, 0)
                    if not id_item:
                        continue
                    pid = id_item.data(Qt.UserRole)
                    if not pid or int(pid) not in by_id:
                        continue
                    p = by_id[int(pid)]
                    # Post-sync refresh must stay UI-only and non-blocking.  The DB path
                    # is written only after a successful local PDF/ZIP save; avoid hundreds
                    # of synchronous filesystem stat calls on the Qt GUI thread here.
                    pdf_ok = bool(str(p.get("pdf_path") or "").strip())
                    zip_ok = bool(str(p.get("zip_path") or "").strip())
                    values = {
                        0: _property_status_label(str(p.get("status", "new"))),
                        7: _photo_state_label(str(p.get("photo_state", "-"))),
                        8: "🟢 완료" if pdf_ok else "⚪ 없음",
                        9: "🟢 완료" if zip_ok else "⚪ 없음",
                        10: _package_state_label(p),
                    }
                    sort_values = {
                        0: str(p.get("status", "new")).strip().lower(),
                        7: str(p.get("photo_state", "-")).strip().lower(),
                        8: 1 if pdf_ok else 0,
                        9: 1 if zip_ok else 0,
                        10: str(p.get("wp_package_state") or p.get("wp_sync_state") or "pending").strip().lower(),
                    }
                    for c, value in values.items():
                        item = self.table.item(r, c)
                        if not isinstance(item, PropertyTableItem):
                            item = PropertyTableItem()
                            self.table.setItem(r, c, item)
                        item.setText(value)
                        item.set_sort_value(sort_values[c], known=True)
            finally:
                self.table.setUpdatesEnabled(True)
                self.table.viewport().update()
            if end < total:
                QTimer.singleShot(10, lambda: update_batch(end))
                return
            self._apply_property_sort()
            self._select_property_id(selected_pid)
            self.table.horizontalHeader().setSectionsClickable(True)

        QTimer.singleShot(0, update_batch)

    def refresh_copy_panel(self) -> None:
        row = self.table.currentRow()
        if row < 0 or not self.table.item(row, 0):
            self.copy_panel.clear()
            return
        pid = self.table.item(row, 0).data(Qt.UserRole)
        p = self.db.property(int(pid)) if pid else None
        if not p:
            self.copy_panel.clear()
            return
        transport = p.get("transport") or []
        transport_text = " / ".join(
            str(x.get("raw") if isinstance(x, dict) else x).strip() for x in transport if x
        )
        def money(key):
            value = int(p.get(key) or 0)
            return f"{value:,}円" if value else "-"
        text = (
            f"매물명\t{p.get('building_name') or '-'}\n"
            f"호실\t{p.get('room') or '-'}\n"
            f"주소\t{p.get('address') or '-'}\n"
            f"교통\t{transport_text or '-'}\n"
            f"월세\t{money('rent')}\t관리비\t{money('management_fee')}\t敷金\t{p.get('deposit') or '-'}\t礼金\t{p.get('key_money') or '-'}"
        )
        self.copy_panel.setPlainText(text)
        self.copy_panel.moveCursor(QTextCursor.Start)

    def refresh_wp_label(self) -> None:
        if not self.db.get_bool("wp_enabled", False):
            self.wp_state.setText("WordPress: 연동 안 함")
            return
        if not WordPressSync(self.db).configured():
            self.wp_state.setText("WordPress: ⚠ 키/주소 설정 필요")
            return
        self.wp_state.setText("WordPress: 연동 사용")

    def open_settings(self) -> None:
        dlg = SettingsDialog(self.db, self)
        if dlg.exec() == QDialog.Accepted:
            self.configure_timer()
            self.refresh_wp_label()

    def check_for_updates(self) -> None:
        from .updater import UPDATE_MANIFEST_URL
        manifest_url = self.db.get_setting("update_manifest_url", "").strip() or UPDATE_MANIFEST_URL
        if not manifest_url:
            QMessageBox.information(
                self,
                "업데이트 확인",
                "설정 → 일반에서 업데이트 정보 URL을 먼저 입력해 주세요.",
            )
            return
        if self.task and self.task.isRunning():
            QMessageBox.information(self, "업데이트 확인", "다른 작업이 끝난 뒤 다시 시도해 주세요.")
            return
        from .updater import check, download, launch_helper, stage

        self.update_btn.setEnabled(False)
        self.status.setText("업데이트 확인 중…")

        def job(_progress):
            info = check(manifest_url, __version__)
            if info is None:
                return {"current": True}
            return {"current": False, "info": info}

        task = TaskThread(job, self)
        self.task = task

        def finish(result):
            self.task = None
            if result.get("current"):
                self.update_btn.setEnabled(True)
                self.status.setText("최신 버전입니다.")
                QMessageBox.information(self, "업데이트 확인", f"현재 v{__version__}가 최신 버전입니다.")
                return
            info = result["info"]
            notes = "\n".join(f"• {note}" for note in info.notes)
            answer = QMessageBox.question(
                self,
                "업데이트 발견",
                f"v{info.version} 업데이트가 있습니다.\n\n현재 버전: v{__version__}\n새 버전: v{info.version}"
                + (f"\n\n{notes}" if notes else "") + "\n\n업데이트하시겠습니까?",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Yes,
            )
            if answer != QMessageBox.Yes:
                self.update_btn.setEnabled(True)
                self.status.setText("업데이트가 취소되었습니다.")
                return

            self.status.setText("업데이트 파일 다운로드 중...")

            def install_job(report):
                report("UPDATE", "업데이트 파일 다운로드 중...", 1, 4)
                archive = download(info)
                report("UPDATE", "업데이트 서명 및 SHA 검증 완료", 2, 4)
                report("UPDATE", "업데이트 설치 준비 중...", 3, 4)
                package = stage(info, archive)
                report("UPDATE", "Updater 실행 · 프로그램을 종료합니다", 4, 4)
                launch_helper(info, package)
                return {"version": info.version}

            install_task = TaskThread(install_job, self)
            self.task = install_task
            install_task.progress.connect(lambda _code, message, _current, _total: self.status.setText(message))

            def install_ready(value):
                self.task = None
                self.status.setText(f"v{value['version']} 설치를 위해 다시 시작합니다…")
                QTimer.singleShot(100, QApplication.quit)

            install_task.result.connect(install_ready)
            install_task.failed.connect(fail)
            install_task.start()

        def fail(message):
            self.update_btn.setEnabled(True)
            self.task = None
            self.status.setText("업데이트 확인 실패")
            QMessageBox.warning(self, "업데이트 오류", message.split("\n\n", 1)[0])

        task.result.connect(finish)
        task.failed.connect(fail)
        task.start()

    def open_details(self) -> None:
        p = self.selected_property()
        if not p:
            return
        dlg = PropertyDetailDialog(p, len(self.db.photos(int(p["id"]))), self)
        dlg.exec()

    def open_source(self) -> None:
        p = self.selected_property()
        if p and p.get("source_url"):
            open_url(str(p["source_url"]))

    def copy_building_name(self) -> None:
        p = self.selected_property()
        if not p:
            return
        name = str(p.get("building_name") or "").strip()
        if not name:
            QMessageBox.information(self, "매물명 복사", "복사할 매물명이 없습니다.")
            return
        QApplication.clipboard().setText(name)
        self.status.setText(f"매물명 복사 완료: {name}")
        self.append_log(f"📋 매물명 복사: {name}")

    def open_login(self) -> None:
        row = self.site_table.currentRow()
        code = None
        if row >= 0 and self.site_table.item(row, 0):
            code = self.site_table.item(row, 0).data(Qt.UserRole)
        if not code:
            QMessageBox.information(self, "사이트 선택", "왼쪽 사이트 상태에서 로그인할 사이트를 먼저 선택해 주세요.")
            return
        adapter = next((a for a in adapters() if a.code == code), None)
        if not adapter:
            return
        try:
            open_login_browser(adapter.code, adapter.seed_urls[0])
            QMessageBox.information(self, "로그인 브라우저", "전용 브라우저 프로필을 열었습니다. 필요한 경우 직접 로그인한 뒤 브라우저 창을 닫고 다시 수집해 주세요. CAPTCHA/MFA는 사람이 직접 완료해야 합니다.")
        except Exception as e:
            QMessageBox.warning(self, "브라우저 오류", str(e))

    def download_photos(self) -> None:
        p = self.selected_property()
        if not p:
            return
        pid = int(p["id"])
        db_path = self.db.path

        def job(progress4):
            db = Database(db_path)
            try:
                manager = PhotoManager(db, Fetcher(visible_browser=False))
                def p3(msg, cur, total):
                    progress4("PHOTO", msg, cur, total)
                return manager.download_for_property(pid, p3)
            finally:
                db.close()

        self.start_task(job, done_message="사진 다운로드 완료")

    def add_pdf(self) -> None:
        p = self.selected_property()
        if not p:
            return
        path, _ = QFileDialog.getOpenFileName(self, "REINS PDF 선택", "", "PDF files (*.pdf)")
        if not path:
            return

        pid = int(p["id"])
        db_path = self.db.path
        auto_upload = self.db.get_bool("wp_auto_upload_on_pdf", True)
        auto_fast = self.db.get_bool("wp_autofast_on_pdf", False)
        wp_configured = WordPressSync(self.db).configured()

        def job(progress):
            db = Database(db_path)
            try:
                progress("PDF", "PDF 확인/복사 중", 1, 4 if auto_upload and wp_configured else 1)
                dest = attach_pdf(db, pid, path)
                result: dict[str, Any] = {"pdf": str(dest)}
                if auto_upload and wp_configured:
                    progress("AUTO", "등록 ZIP 생성 중", 2, 4)
                    out = create_zip(db, pid, require_pdf=True)
                    result["zip"] = str(out)
                    progress("AUTO", f"WordPress 업로드 중: {out.name}", 3, 4)
                    upload = WordPressSync(db).auto_submit_package(pid, out, auto_fast=auto_fast)
                    result["upload"] = upload
                    label = "GPT/FAST 요청" if auto_fast else "ZIP 업로드"
                    progress("AUTO", f"{label} 완료", 4, 4)
                return result
            except Exception as exc:
                if auto_upload and wp_configured:
                    db.set_package_state(pid, state="error", error=str(exc))
                raise
            finally:
                db.close()

        def after(value):
            pdf_path = str(value.get("pdf") or "") if isinstance(value, dict) else ""
            if pdf_path:
                self.append_log(f"✅ PDF 추가: {pdf_path}")
            if auto_upload and not wp_configured:
                QMessageBox.warning(
                    self,
                    "자동 업로드 설정 필요",
                    "PDF는 추가했지만 WordPress 자동 업로드를 시작할 수 없습니다. 설정에서 TAKURO WordPress 연동 주소/키를 확인해 주세요.",
                )

        if auto_upload and wp_configured:
            done = "PDF → ZIP → WordPress 업로드 → GPT/FAST 요청 완료" if auto_fast else "PDF → ZIP → WordPress 자동 업로드 완료"
        else:
            done = "PDF 추가 완료"
        self.start_task(job, done_message=done, after=after)

    def make_zip(self) -> None:
        p = self.selected_property()
        if not p:
            return
        pid = int(p["id"])
        db_path = self.db.path

        def job(progress):
            db = Database(db_path)
            try:
                progress("ZIP", "등록 ZIP 압축 중", 0, 1)
                out = create_zip(db, pid, require_pdf=True)
                progress("ZIP", f"압축 완료: {out.name}", 1, 1)
                return str(out)
            finally:
                db.close()

        def after(value):
            out = str(value)
            QApplication.clipboard().setText(out)
            QMessageBox.information(
                self,
                "ZIP 생성 완료",
                f"TAKURO 등록용 ZIP을 만들었습니다.\n파일 경로도 클립보드에 복사했습니다.\n\n{out}",
            )

        self.start_task(job, done_message="ZIP 생성 완료", after=after)

    def open_folder(self) -> None:
        p = self.selected_property()
        if not p:
            return
        folder = PhotoManager.property_folder(p)
        open_path(folder)

    def open_export_folder(self) -> None:
        open_path(export_root())

    def poll_translation(self) -> None:
        if (self.task and self.task.isRunning()) or not self.db.get_bool("translation_enabled", False):
            return
        from .translation import process_job
        db_path = self.db.path
        def job(progress):
            db = Database(db_path)
            try:
                return process_job(db, progress)
            finally:
                db.close()
        self.start_task(job, done_message="번역 요청 확인 완료", refresh_on_finish=False, quiet_fail=True)

    def poll_ready_packages(self) -> None:
        """Lightweight poll for staff-selected PDFs; never starts a second worker."""
        if self.task and self.task.isRunning():
            return
        if not self.db.get_bool("wp_enabled", False) or not self.db.get_bool("wp_auto_sync", True):
            return
        db_path = self.db.path

        def job(progress):
            db = Database(db_path)
            try:
                sync = WordPressSync(db)
                if not sync.configured():
                    return {"remote_ready": 0, "configured": False}
                progress("WP", "신규매물 PDF 자동 등록 확인", 0, 1)
                result = sync.pull_ready_drawings(auto_package=True, auto_fast=True)
                progress("WP", f"PDF 자동 등록 확인 완료 · {result}", 1, 1)
                return result
            finally:
                db.close()

        def after(value):
            # "matched" is true on every idle poll for already-known ready rows and
            # is not a UI mutation.  Treat only actual local/package changes as a
            # refresh trigger, and use the lightweight state-only refresh path.
            changed = any(int(value.get(k, 0) or 0) > 0 for k in ("downloaded", "packaged", "submitted")) if isinstance(value, dict) else False
            if changed:
                self.refresh_sites()
                self.refresh_wp_label()
                self.refresh_property_states_chunked()

        self.start_task(
            job,
            done_message="신규매물 PDF → FAST draft 자동 등록 확인 완료",
            after=after,
            refresh_on_finish=False,
        )

    def sync_wordpress(self, automatic: bool = False) -> None:
        if not self.db.get_bool("wp_enabled", False):
            if not automatic:
                QMessageBox.information(self, "TAKURO 연동", "설정에서 [TAKURO WordPress 연동 사용]을 켜면 사용할 수 있습니다. 로컬 수집/ZIP 생성은 연동 없이 그대로 사용할 수 있습니다.")
            return
        db_path = self.db.path

        def job(progress):
            db = Database(db_path)
            try:
                sync = WordPressSync(db)
                client = sync.client()
                status = client.status()
                progress("WP", f"TAKURO {status.get('version','?')} 연결됨", 0, 1)

                def sync_progress(done: int, total: int, message: str) -> None:
                    progress("WP", message, done, max(total, 1))

                sent = sync.sync_pending(batch_size=25, progress_callback=sync_progress)
                inventory = {"pending_snapshots": 0, "synced_snapshots": 0, "halted": False, "results": []}
                if sent.get("halted"):
                    code = sent.get("http_status") or "?"
                    progress("WP", f"후보 동기화 중단 HTTP {code} · 미전송/재시도 {sent.get('remaining', 0)}건 · {sent.get('error','')}", 0, 1)
                else:
                    progress("WP", f"후보 동기화 완료 · {sent.get('synced', 0)}건 · {sent.get('transports', {})}", 1, 1)
                    inventory = sync.sync_inventory_snapshots(batch_size=100, progress_callback=sync_progress)
                    if inventory.get("halted"):
                        code = inventory.get("http_status") or "?"
                        progress("WP", f"나간매물 재고 동기화 중단 HTTP {code} · {inventory.get('error','')}", 0, 1)
                    elif inventory.get("synced_snapshots"):
                        progress("WP", f"나간매물 재고 동기화 완료 · {inventory.get('results', [])}", 1, 1)
                if sent.get("halted") or inventory.get("halted"):
                    reason = sent.get("error") or inventory.get("error") or "WordPress 전송이 중단되었습니다."
                    raise RuntimeError(str(reason))
                return {"server": status, "sent": sent, "inventory": inventory, "ready": {"server_side": True}}
            finally:
                db.close()

        def after(value):
            self.refresh_sites()
            self.refresh_wp_label()
            self.refresh_property_states_chunked()
            if automatic:
                self.db.set_setting("automation_last_success", datetime.now().isoformat(timespec="seconds"))
                self.configure_timer()

        self.start_task(
            job,
            done_message="TAKURO 동기화 완료",
            after=after,
            on_fail=(lambda _message: self.schedule_sync_retry()) if automatic else None,
            refresh_on_finish=False,
            quiet_fail=automatic,
        )

    def open_wp_zip(self) -> None:
        site = self.db.get_setting("wp_site_url", "https://homes.takuro.tech/kr").rstrip("/")
        open_url(site + "/wp-admin/admin.php?page=takuro-photos-zip")


def run_app() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("TAKURO TECHNOLOGY")
    window = MainWindow()
    window.show()
    marker = os.environ.pop("TAKURO_UPDATE_HEALTH_MARKER", "")
    token = os.environ.pop("TAKURO_UPDATE_HEALTH_TOKEN", "")
    if marker and token:
        try:
            Path(marker).write_text(token, encoding="utf-8")
        except OSError:
            pass
    return app.exec()
