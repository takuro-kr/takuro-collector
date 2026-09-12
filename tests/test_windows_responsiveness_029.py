from pathlib import Path

from takuro_collector.db import Database
from takuro_collector.models import PropertyCandidate


def _candidate(i: int) -> PropertyCandidate:
    return PropertyCandidate(
        source_site="https://example.test",
        source_property_id=f"room-{i}",
        management_company="TEST",
        building_name="Building",
        room=str(i),
        prefecture="東京都",
        address="東京都新宿区",
        source_url=f"https://example.test/{i}",
    )


def test_collection_batch_upsert_uses_one_outer_transaction(tmp_path):
    db = Database(tmp_path / "collector.sqlite3")
    try:
        statements = []
        db.conn.set_trace_callback(statements.append)
        saved = db.upsert_properties_batch([_candidate(i) for i in range(30)])
        assert len(saved) == 30
        commits = [s for s in statements if s.strip().upper() == "COMMIT"]
        begins = [s for s in statements if s.strip().upper().startswith("BEGIN")]
        assert len(begins) == 1
        assert len(commits) == 1
    finally:
        db.close()


def test_ui_has_cooperative_cancel_and_worker_thread():
    source = Path("takuro_collector/ui.py").read_text(encoding="utf-8")
    assert "class TaskThread(QThread)" in source
    assert "requestInterruption()" in source
    assert "isInterruptionRequested()" in source
    assert 'QPushButton("작업 취소")' in source
    assert "현재 HTTP/페이지 처리 단위가 끝난 뒤 중단" in source


def test_sync_updates_stay_batched():
    source = Path("takuro_collector/wordpress.py").read_text(encoding="utf-8")
    db_source = Path("takuro_collector/db.py").read_text(encoding="utf-8")
    assert "update_wp_states_batch(state_updates)" in source
    assert "def update_wp_states_batch" in db_source
