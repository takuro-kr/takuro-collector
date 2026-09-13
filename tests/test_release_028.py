import base64
import json
import re
from pathlib import Path

from takuro_collector import __version__
from takuro_collector.collector import CollectorEngine
from takuro_collector.db import Database
from takuro_collector.models import PropertyCandidate
from takuro_collector.sites.base import DiscoveryResult
from takuro_collector.sites.kinoshita import KinoshitaAdapter
from takuro_collector.wordpress import WordPressClient, WordPressSync

FIX = Path(__file__).with_name('fixtures')


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


def test_release_version_028():
    assert tuple(map(int, __version__.split('.'))) >= (0, 2, 8)


def test_kin_complete_inventory_requires_all_four_areas_to_match_counts():
    area_html = (FIX / 'kin_area.html').read_text(encoding='utf-8')
    real = (FIX / 'kin_tokyo_page1.html').read_text(encoding='utf-8')

    class FetchResult:
        def __init__(self, html, url):
            self.html = html
            self.url = url
            self.via_browser = False

    class Fetcher:
        def fetch(self, url, site_code, **kwargs):
            return FetchResult(area_html, url)

        def post_form(self, url, site_code, data, **kwargs):
            area_key = next(v for k, v in list(data) if k == 'area_key')
            html = re.sub(r'121</span>件中', '10</span>件中', real)
            html = re.sub(
                r'/details/([^/?#]+?)details\.html',
                lambda m: f"/details/{area_key}-{m.group(1)}details.html",
                html,
            )
            return FetchResult(html, url)

    result = KinoshitaAdapter().discover(Fetcher())
    assert result.inventory_complete is True
    assert result.inventory_site == 'kinoshita-chintai.com'
    assert result.listed_count == 40
    assert len(result.urls) == 40
    assert len(result.hints) == 40


def test_complete_inventory_saved_before_registered_duplicate_filter(tmp_path, monkeypatch):
    db = Database(tmp_path / 'db.sqlite3')
    u1 = 'https://kinoshita-chintai.com/details/keep_1details.html'
    u2 = 'https://kinoshita-chintai.com/details/new_2details.html'
    hints = {
        u1: {'url': u1, 'source_site': 'kinoshita-chintai.com', 'source_property_id': 'keep_1', 'management_company': '株式会社木下の賃貸', 'building_name': '既存', 'room': '101', 'address': '東京都新宿区1-1', 'prefecture': '東京都'},
        u2: {'url': u2, 'source_site': 'kinoshita-chintai.com', 'source_property_id': 'new_2', 'management_company': '株式会社木下の賃貸', 'building_name': '新規', 'room': '202', 'address': '東京都新宿区2-2', 'prefecture': '東京都'},
    }

    class Adapter:
        code = 'KIN'
        label = '木下の賃貸'

        def discover(self, fetcher):
            return DiscoveryResult(
                [u1, u2],
                hints=hints,
                listed_count=2,
                inventory_complete=True,
                inventory_site='kinoshita-chintai.com',
            )

        def collect_url(self, fetcher, url):
            h = hints[url]
            return PropertyCandidate(
                source_site=h['source_site'], source_property_id=h['source_property_id'],
                management_company=h['management_company'], building_name=h['building_name'], room=h['room'],
                prefecture='東京都', address=h['address'], source_url=url, rent=60000, management_fee=3000,
            )

    class Client:
        def duplicate_check(self, items):
            return {'results': [
                {'status': 'registered', 'matches': [{'post_id': 10}]},
                {'status': 'not_registered', 'matches': []},
            ]}

    import takuro_collector.collector as collector_module
    monkeypatch.setattr(collector_module, 'adapters', lambda: [Adapter()])
    monkeypatch.setattr(WordPressSync, 'configured', lambda self: True)
    monkeypatch.setattr(WordPressSync, 'client', lambda self: Client())
    result = CollectorEngine(db).scan_all()
    snapshots = db.pending_inventory_snapshots()
    assert result.inventory_snapshots == 1
    assert result.duplicate_filtered == 1
    assert result.detail_attempted == 1
    assert len(snapshots) == 1
    assert snapshots[0]['expected_count'] == 2
    assert {x['source_property_id'] for x in snapshots[0]['items']} == {'keep_1', 'new_2'}
    db.close()


def test_inventory_snapshot_sends_atomic_source_id_set():
    s = Session([
        Resp(200, {'accepted': True, 'baseline_only': True, 'inventory_count': 2}),
    ])
    c = client_with(s)
    out = c.send_inventory_snapshot(
        source_site='kinoshita-chintai.com',
        source_property_ids=['2_2', '1_1', '1_1'],
        complete=True,
    )
    assert out['accepted'] is True
    assert len(s.calls) == 1
    assert s.calls[0][0].endswith('/wp-json/takuro/v1/collection/inventory-snapshot')
    payload = s.calls[0][1]['json']
    assert payload['complete'] is True
    assert payload['errors'] == 0
    assert payload['parse_errors'] == 0
    assert payload['source_property_ids'] == ['1_1', '2_2']
    assert 'items' not in payload


def test_inventory_snapshot_refuses_incomplete_or_unsafe_before_http():
    s = Session([])
    c = client_with(s)
    import pytest
    from takuro_collector.wordpress import WordPressError
    with pytest.raises(WordPressError):
        c.send_inventory_snapshot(source_site='kinoshita-chintai.com', source_property_ids=['1_1'], complete=False)
    with pytest.raises(WordPressError):
        c.send_inventory_snapshot(source_site='kinoshita-chintai.com', source_property_ids=['1_1'], complete=True, parse_errors=1)
    with pytest.raises(WordPressError):
        c.send_inventory_snapshot(source_site='kinoshita-chintai.com', source_property_ids=['1_1'], complete=True, blockers=['timeout'])
    assert s.calls == []


def test_inventory_sync_sends_382_ids_once_and_marks_snapshot_synced(tmp_path, monkeypatch):
    db = Database(tmp_path / 'db.sqlite3')
    items = [
        {'source_property_id': f'id-{i}', 'building_name': f'建物{i}', 'room': str(i), 'source_url': f'https://example.test/{i}'}
        for i in range(382)
    ]
    db.save_inventory_snapshot('KIN', 'kinoshita-chintai.com', items)
    calls = []

    class Client:
        def send_inventory_snapshot(self, **kwargs):
            calls.append(kwargs)
            return {
                'accepted': True, 'transport': 'rest_json',
            }

    sync = WordPressSync(db)
    monkeypatch.setattr(sync, 'client', lambda: Client())
    progress = []
    out = sync.sync_inventory_snapshots(progress_callback=lambda d, t, m: progress.append((d, t, m)))
    assert len(calls) == 1
    assert len(calls[0]['source_property_ids']) == 382
    assert calls[0]['complete'] is True
    assert calls[0]['errors'] == 0
    assert calls[0]['parse_errors'] == 0
    assert calls[0]['blockers'] == []
    assert out['synced_snapshots'] == 1
    assert out['results'][0]['destination'] == 'legacy_connect'
    assert db.pending_inventory_snapshots() == []
    assert any(d == 382 and t == 382 for d, t, _ in progress)
    db.close()


def test_inventory_sync_does_not_send_malformed_local_snapshot(tmp_path, monkeypatch):
    db = Database(tmp_path / 'db.sqlite3')
    db.save_inventory_snapshot('KIN', 'kinoshita-chintai.com', [
        {'source_property_id': 'id-1'}, {'source_property_id': 'id-2'},
    ])
    db.conn.execute("UPDATE inventory_delivery_outbox SET expected_count=3 WHERE site_code='KIN'")
    db.conn.commit()
    class Client:
        def send_inventory_snapshot(self, **kwargs):
            raise AssertionError('malformed snapshot must not be sent')
    sync = WordPressSync(db)
    monkeypatch.setattr(sync, 'client', lambda: Client())
    out = sync.sync_inventory_snapshots()
    assert out['synced_snapshots'] == 0
    assert out['halted'] is False
    assert out['results'][0]['ok'] is False
    db.close()


def test_inventory_sync_http_error_keeps_snapshot_unsynced(tmp_path, monkeypatch):
    db = Database(tmp_path / 'db.sqlite3')
    db.save_inventory_snapshot('KIN', 'kinoshita-chintai.com', [{'source_property_id': 'id-1'}])
    from takuro_collector.wordpress import WordPressError
    class Client:
        def send_inventory_snapshot(self, **kwargs):
            raise WordPressError('HTTP 503: upstream unavailable')
    sync = WordPressSync(db)
    monkeypatch.setattr(sync, 'client', lambda: Client())
    out = sync.sync_inventory_snapshots()
    assert out['synced_snapshots'] == 0
    assert out['halted'] is True
    assert out['http_status'] == 503
    assert len(db.pending_inventory_snapshots()) == 1
    db.close()


def test_candidate_sync_uses_batch_db_update_not_per_row_commits(tmp_path, monkeypatch):
    db = Database(tmp_path / 'db.sqlite3')
    for i in range(25):
        db.upsert_property(PropertyCandidate(
            source_site='kinoshita-chintai.com', source_property_id=f'id-{i}', management_company='株式会社木下の賃貸',
            building_name=f'建物{i}', room=str(i + 100), prefecture='東京都', address=f'東京都新宿区{i}-1',
            source_url=f'https://kinoshita-chintai.com/details/{i}_1details.html', rent=60000, management_fee=3000,
        ))

    class Client:
        def send_candidates(self, items):
            return {'transport': 'rest_json', 'results': [{'status': 'new', 'id': 5000 + i} for i in range(len(items))]}

    sync = WordPressSync(db)
    monkeypatch.setattr(sync, 'client', lambda: Client())
    monkeypatch.setattr(db, 'update_wp_state', lambda *a, **k: (_ for _ in ()).throw(AssertionError('per-row commit used')))
    out = sync.sync_pending(batch_size=25)
    assert out['synced'] == 25
    assert out['remaining'] == 0
    db.close()


def test_kin_inventory_completeness_uses_building_cards_not_room_url_count():
    area_html = (FIX / 'kin_area.html').read_text(encoding='utf-8')
    # One search-result building card can expose two currently vacant rooms.
    result_html = '''
    <html><body><span class="change_list_count">1件中</span>
    <ul id="search_result_housing_list"><li class="main_box">
      <h3>■複数空室ビル</h3><dl><dt>住所</dt><dd>東京都新宿区1-1</dd></dl>
      <table><tbody>
        <tr><td>x</td><td>x</td><td>101</td><td>60,000円</td><td></td><td>1K</td><td><a href="/details/500_1details.html">詳細</a></td></tr>
        <tr><td>x</td><td>x</td><td>202</td><td>70,000円</td><td></td><td>1K</td><td><a href="/details/500_2details.html">詳細</a></td></tr>
      </tbody></table>
    </li></ul></body></html>'''

    class FetchResult:
        def __init__(self, html, url):
            self.html = html
            self.url = url
            self.via_browser = False

    class Fetcher:
        def fetch(self, url, site_code, **kwargs):
            return FetchResult(area_html, url)

        def post_form(self, url, site_code, data, **kwargs):
            area_key = next(v for k, v in list(data) if k == 'area_key')
            html = result_html.replace('/details/500_', f'/details/{area_key}-500_')
            return FetchResult(html, url)

    result = KinoshitaAdapter().discover(Fetcher())
    assert result.inventory_complete is True
    assert result.listed_count == 4  # building-card total across four target areas
    assert len(result.urls) == 8     # room-level inventory can legitimately be larger
    assert len(result.hints) == 8


def test_kin_inventory_rejects_duplicate_pagination_page_even_when_counts_add_up():
    area_html = (FIX / 'kin_area.html').read_text(encoding='utf-8')
    # Site reports two building cards, but pagination mistakenly returns the same
    # first card for both pages. Unique card identity must prevent false completeness.
    result_html = '''
    <html><body><span class="change_list_count">2件中</span>
    <ul id="search_result_housing_list"><li class="main_box">
      <h3>■同じカード</h3><dl><dt>住所</dt><dd>東京都新宿区1-1</dd></dl>
      <table><tbody><tr><td>x</td><td>x</td><td>101</td><td>60,000円</td><td></td><td>1K</td><td><a href="/details/700_1details.html">詳細</a></td></tr></tbody></table>
    </li></ul></body></html>'''

    class FetchResult:
        def __init__(self, html, url):
            self.html = html
            self.url = url
            self.via_browser = False

    class Fetcher:
        def fetch(self, url, site_code, **kwargs):
            return FetchResult(area_html, url)

        def post_form(self, url, site_code, data, **kwargs):
            area_key = next(v for k, v in list(data) if k == 'area_key')
            return FetchResult(result_html.replace('/details/700_', f'/details/{area_key}-700_'), url)

    result = KinoshitaAdapter().discover(Fetcher())
    assert result.inventory_complete is False


def test_kin_room_parser_12318_1_letter_only_room_fixture():
    html = (FIX / 'kin_detail_12318_1_room_structure.html').read_text(encoding='utf-8')
    soup = __import__('bs4').BeautifulSoup(html, 'html.parser')
    pairs = KinoshitaAdapter._table_fields(soup)
    assert KinoshitaAdapter._room(soup, pairs, '弥生台キャッスル', '') == 'A'


def test_kin_room_parser_11983_4_letter_only_room_fixture():
    html = (FIX / 'kin_detail_11983_4_room_structure.html').read_text(encoding='utf-8')
    soup = __import__('bs4').BeautifulSoup(html, 'html.parser')
    pairs = KinoshitaAdapter._table_fields(soup)
    assert KinoshitaAdapter._room(soup, pairs, 'ワイズスクエア', '') == 'F'


def test_kin_letter_only_room_requires_explicit_dom_evidence():
    soup = __import__('bs4').BeautifulSoup('<html><h1>物件名</h1></html>', 'html.parser')
    assert KinoshitaAdapter._room(soup, {}, '物件名', '') == ''
