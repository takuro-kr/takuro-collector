import base64
import json

from takuro_collector import __version__
from takuro_collector.db import Database
from takuro_collector.models import PropertyCandidate
from takuro_collector.wordpress import WordPressClient, WordPressError, WordPressSync


class Resp:
    def __init__(self, status, data=None, text=''):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._data = data
        self.text = text
        self.content = text.encode('utf-8')

    def json(self):
        if self._data is None:
            raise ValueError('not json')
        return self._data


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def client_with(session):
    c = object.__new__(WordPressClient)
    c.site_url = 'https://example.test'
    c.key = 'a' * 64
    c.timeout = 20
    c.session = session
    return c


def decode_form(call):
    encoded = call[1]['data']['payload_b64']
    encoded += '=' * ((4 - len(encoded) % 4) % 4)
    return json.loads(base64.urlsafe_b64decode(encoded.encode('ascii')).decode('utf-8'))


def candidate(i: int, raw_noise: str = '') -> PropertyCandidate:
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
        management_fee=3000,
        collected_info={'table_fields': {'noise': raw_noise}},
        photo_sources=[{'url': 'https://example.test/photo.jpg', 'alt': raw_noise}],
    )


def test_release_version_027_or_newer():
    assert tuple(map(int, __version__.split('.'))) >= (0, 2, 7)


def test_registration_v2_rest_success():
    s = Session([Resp(200, {'results': [{'status': 'new', 'id': 7}]})])
    c = client_with(s)
    result = c.send_candidates([{'building_name': '建物', 'room': '101'}])
    assert result['transport'] == 'registration_v2_rest'
    assert len(s.calls) == 1
    assert s.calls[0][0].endswith('/wp-json/takuro-registration/v1/candidates')


def test_candidate_sync_payload_excludes_large_scrape_noise(tmp_path):
    db = Database(tmp_path / 'db.sqlite3')
    db.upsert_property(candidate(1, raw_noise='X' * 300_000))
    row = db.pending_sync(1)[0]
    payload = WordPressSync.payload_from_row(row)
    encoded = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    assert len(encoded) < 5000
    assert 'collected_info' not in payload
    assert 'photo_sources' not in payload
    assert payload['building_name'] == '建物1'
    db.close()


def test_380_candidates_sync_in_25_row_batches_with_progress(tmp_path, monkeypatch):
    db = Database(tmp_path / 'db.sqlite3')
    for i in range(380):
        db.upsert_property(candidate(i))
    calls = []
    progress = []

    class Client:
        def send_candidates(self, items):
            calls.append(len(items))
            return {
                'transport': 'front_form_b64',
                'results': [{'status': 'new', 'id': 10000 + n} for n, _ in enumerate(items)],
            }

    sync = WordPressSync(db)
    monkeypatch.setattr(sync, 'client', lambda: Client())
    result = sync.sync_pending(batch_size=25, progress_callback=lambda d, t, m: progress.append((d, t, m)))
    assert calls == [25] * 15 + [5]
    assert result['synced'] == 380
    assert result['remaining'] == 0
    assert result['transports'] == {'front_form_b64': 16}
    assert any(d == 380 and t == 380 for d, t, _ in progress)
    db.close()


def test_request_failure_still_keeps_pending_rows_retryable(tmp_path, monkeypatch):
    db = Database(tmp_path / 'db.sqlite3')
    for i in range(30):
        db.upsert_property(candidate(i))

    class Client:
        def send_candidates(self, items):
            raise WordPressError('HTTP 403 (non-JSON): SiteGuard')

    sync = WordPressSync(db)
    monkeypatch.setattr(sync, 'client', lambda: Client())
    result = sync.sync_pending(batch_size=25)
    assert result['halted'] is True
    assert result['remaining'] == 30
    assert result['errors'] == 0
    states = [r[0] for r in db.conn.execute('SELECT wp_sync_state FROM properties ORDER BY id')]
    assert states == ['pending'] * 30
    db.close()
