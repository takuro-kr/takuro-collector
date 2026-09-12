from pathlib import Path

import pytest

from takuro_collector import __version__
from takuro_collector.db import Database
from takuro_collector.models import PropertyCandidate
from takuro_collector.wordpress import WordPressClient, WordPressError, WordPressSync


def _candidate(i: int) -> PropertyCandidate:
    return PropertyCandidate(
        source_site='kinoshita-chintai.com',
        source_property_id=f'id-{i}',
        management_company='株式会社木下の賃貸',
        building_name=f'建物{i}',
        room=str(100 + i),
        prefecture='東京都',
        address=f'東京都新宿区テスト{i}-1',
        source_url=f'https://kinoshita-chintai.com/details/{i}_1details.html',
        rent=60000 + i,
    )


def test_release_version_025():
    assert tuple(map(int, __version__.split('.'))) >= (0, 2, 5)


class _Html403:
    status_code = 403
    ok = False
    text = '<html><body><h1>Forbidden</h1><p>Access denied by security policy.</p></body></html>'
    content = text.encode('utf-8')

    def json(self):
        raise ValueError('not json')


def test_non_json_403_error_contains_safe_body_excerpt():
    client = object.__new__(WordPressClient)
    with pytest.raises(WordPressError) as exc:
        client._json(_Html403())
    msg = str(exc.value)
    assert 'HTTP 403' in msg
    assert 'Forbidden' in msg
    assert 'security policy' in msg
    assert '<html>' not in msg


def test_batch_transport_failure_keeps_all_rows_retryable(tmp_path, monkeypatch):
    db = Database(tmp_path / 'db.sqlite3')
    for i in range(120):
        db.upsert_property(_candidate(i))

    class Client:
        def send_candidates(self, items):
            raise WordPressError('HTTP 403 (non-JSON): Forbidden')

    sync = WordPressSync(db)
    monkeypatch.setattr(sync, 'client', lambda: Client())
    result = sync.sync_pending(batch_size=50, max_rows=500)

    assert result['halted'] is True
    assert result['http_status'] == 403
    assert result['sent'] == 0
    assert result['synced'] == 0
    assert result['errors'] == 0
    assert result['remaining'] == 120
    states = [r[0] for r in db.conn.execute('SELECT wp_sync_state FROM properties ORDER BY id')]
    assert states == ['pending'] * 120
    retries = [r[0] for r in db.conn.execute("SELECT retry_count FROM sync_queue ORDER BY property_id")]
    assert retries == [0] * 120
    db.close()


def test_successful_batches_sync_all_rows(tmp_path, monkeypatch):
    db = Database(tmp_path / 'db.sqlite3')
    for i in range(120):
        db.upsert_property(_candidate(i))

    calls = []

    class Client:
        def send_candidates(self, items):
            calls.append(len(items))
            return {'results': [{'status': 'created', 'id': 1000 + n} for n, _ in enumerate(items)]}

    sync = WordPressSync(db)
    monkeypatch.setattr(sync, 'client', lambda: Client())
    result = sync.sync_pending(batch_size=50, max_rows=500)

    assert calls == [50, 50, 20]
    assert result['halted'] is False
    assert result['sent'] == 120
    assert result['synced'] == 120
    assert result['errors'] == 0
    assert result['remaining'] == 0
    states = [r[0] for r in db.conn.execute('SELECT wp_sync_state FROM properties ORDER BY id')]
    assert states == ['synced'] * 120
    db.close()


def test_later_batch_failure_preserves_unsent_rows(tmp_path, monkeypatch):
    db = Database(tmp_path / 'db.sqlite3')
    for i in range(120):
        db.upsert_property(_candidate(i))

    calls = 0

    class Client:
        def send_candidates(self, items):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise WordPressError('HTTP 403 (non-JSON): Forbidden')
            return {'results': [{'status': 'created', 'id': 2000 + n} for n, _ in enumerate(items)]}

    sync = WordPressSync(db)
    monkeypatch.setattr(sync, 'client', lambda: Client())
    result = sync.sync_pending(batch_size=50, max_rows=500)

    assert result['halted'] is True
    assert result['sent'] == 50
    assert result['synced'] == 50
    assert result['remaining'] == 70
    states = [r[0] for r in db.conn.execute('SELECT wp_sync_state FROM properties ORDER BY id')]
    assert states[:50] == ['synced'] * 50
    assert states[50:] == ['pending'] * 70
    db.close()


def test_legacy_batch_403_errors_are_repaired_to_pending(tmp_path, monkeypatch):
    db = Database(tmp_path / 'db.sqlite3')
    ids = []
    for i in range(3):
        pid, _ = db.upsert_property(_candidate(i))
        ids.append(pid)
        db.update_wp_state(pid, state='error', error='TAKURO 응답이 JSON이 아닙니다. HTTP 403')

    class Client:
        def send_candidates(self, items):
            raise WordPressError('HTTP 403 (non-JSON): Forbidden')

    sync = WordPressSync(db)
    monkeypatch.setattr(sync, 'client', lambda: Client())
    result = sync.sync_pending(batch_size=50, max_rows=500)

    assert result['repaired_legacy_403'] == 3
    states = [r[0] for r in db.conn.execute('SELECT wp_sync_state FROM properties ORDER BY id')]
    assert states == ['pending'] * 3
    db.close()
