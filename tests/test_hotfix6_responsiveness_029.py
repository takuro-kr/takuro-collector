from pathlib import Path

from takuro_collector.db import Database, SCHEMA_VERSION
from takuro_collector.models import PropertyCandidate


def _candidate(i: int) -> PropertyCandidate:
    return PropertyCandidate(
        source_site="example.test",
        source_property_id=f"id-{i}",
        management_company="TEST",
        building_name=f"Building {i}",
        room=str(100 + i),
        prefecture="東京都",
        address=f"東京都新宿区西新宿{i % 9 + 1}-1-1",
        source_url=f"https://example.test/{i}",
        rent=100000 + i,
        management_fee=5000,
    )


def _block(source: str, start: str, end: str) -> str:
    return source.split(start, 1)[1].split(end, 1)[0]


def test_progress_signals_are_rate_limited_without_weakening_cancel_checks():
    source = Path("takuro_collector/ui.py").read_text(encoding="utf-8")
    block = _block(source, "class TaskThread(QThread)", "class UrlDialog")
    assert "time.monotonic()" in block
    assert "now - last_emit >= 0.08" in block
    assert "pending" in block
    assert "isInterruptionRequested()" in block
    assert "self.progress.emit(*pending)" in block


def test_settings_connection_test_is_not_on_gui_thread():
    source = Path("takuro_collector/ui.py").read_text(encoding="utf-8")
    block = _block(source, "    def _test_wp", "    def _save")
    assert "TaskThread(job, self)" in block
    assert "task.start()" in block
    assert "WordPressClient(site, key).status()" in block
    assert "client.status()" not in block


def test_full_property_refresh_is_summary_only_chunked_and_has_no_file_stats():
    source = Path("takuro_collector/ui.py").read_text(encoding="utf-8")
    block = _block(source, "    def refresh_properties", "    def refresh_property_states_chunked")
    assert "list_property_summaries" in block
    assert "QTimer.singleShot(5" in block
    assert ".is_file()" not in block
    assert ".stat()" not in block
    assert "list_properties(" not in block


def test_main_property_table_does_not_use_resize_to_contents():
    source = Path("takuro_collector/ui.py").read_text(encoding="utf-8")
    block = _block(source, "self.table = QTableWidget(0, 11)", "right_layout.addWidget(self.table, 1)")
    assert "QHeaderView.ResizeToContents" not in block
    assert "QHeaderView.Interactive" in block


def test_pdf_attach_and_manual_zip_are_worker_tasks():
    source = Path("takuro_collector/ui.py").read_text(encoding="utf-8")
    add_pdf = _block(source, "    def add_pdf", "    def make_zip")
    make_zip = _block(source, "    def make_zip", "    def open_folder")
    assert "self.start_task(job" in add_pdf
    assert "attach_pdf(db, pid, path)" in add_pdf
    assert "attach_pdf(self.db" not in add_pdf
    assert "self.start_task(job" in make_zip
    assert "create_zip(db, pid" in make_zip
    assert "create_zip(self.db" not in make_zip


def test_large_result_logging_is_bounded_before_plain_text_edit():
    source = Path("takuro_collector/ui.py").read_text(encoding="utf-8")
    assert "def _compact_log_value" in source
    assert 'self.append_log(f"✅ {done_message}: {self._compact_log_value(value)}")' in source
    assert 'self.append_log(f"✅ {done_message}: {value}")' not in source


def test_summary_query_does_not_decode_large_json_payloads(tmp_path):
    db = Database(tmp_path / "collector.sqlite3")
    try:
        saved = db.upsert_properties_batch([_candidate(i) for i in range(50)])
        huge = '{"blob":"' + ('x' * 20000) + '"}'
        with db.conn:
            db.conn.execute("UPDATE properties SET raw_payload=?", (huge,))
        rows = db.list_property_summaries(limit=50)
        assert len(rows) == len(saved) == 50
        assert "raw_payload" not in rows[0]
        assert "transport" not in rows[0]
        assert set(rows[0]).issuperset({"id", "building_name", "room", "pdf_path", "zip_path"})
    finally:
        db.close()


def test_current_schema_reopen_skips_migration_write_path(tmp_path, monkeypatch):
    path = tmp_path / "collector.sqlite3"
    db = Database(path)
    try:
        assert int(db.get_setting("schema_version", "0")) == SCHEMA_VERSION
    finally:
        db.close()

    def fail_install(_self):
        raise AssertionError("current-schema worker connection should not rerun install()")

    monkeypatch.setattr(Database, "install", fail_install)
    db2 = Database(path)
    db2.close()


def test_schema_has_recent_order_index(tmp_path):
    db = Database(tmp_path / "collector.sqlite3")
    try:
        indexes = {row[1] for row in db.conn.execute("PRAGMA index_list(properties)")}
        assert "idx_properties_seen" in indexes
    finally:
        db.close()
