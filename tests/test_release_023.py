from pathlib import Path

from takuro_collector import __version__
from takuro_collector.fetcher import Fetcher, FetchResult
from takuro_collector.sites.kinoshita import KinoshitaAdapter

FIX = Path(__file__).with_name('fixtures')


def test_release_version_023_or_newer():
    assert tuple(map(int, __version__.split('.'))) >= (0, 2, 3)


def test_kin_real_fixture_is_recognized_as_search_result():
    html = (FIX / 'kin_tokyo_page1.html').read_text(encoding='utf-8')
    assert Fetcher._looks_like_kin_search_result(html)
    total, rows = KinoshitaAdapter._parse_result_page(html)
    assert total == 121
    assert len(rows) == 10


class _Response:
    def __init__(self, html, status=200, url='https://kinoshita-chintai.com/search_result.html'):
        self.text = html
        self.status_code = status
        self.url = url
        self.headers = {}
        self.content = html.encode('utf-8')
        self.ok = 200 <= status < 400


class _Session:
    def __init__(self, response):
        self.response = response
        self.cookies = type('C', (), {'get_dict': lambda self: {}})()
    def post(self, *args, **kwargs):
        return self.response


def test_http_200_without_kin_result_marker_falls_back_to_browser(monkeypatch):
    f = Fetcher()
    f.session = _Session(_Response('<html><title>redirect shell</title><body>ok</body></html>'))
    expected = FetchResult('https://kinoshita-chintai.com/search_result.html', '<div id="search_result_housing_list"></div>', 200, True)
    called = {}
    def fake_browser(url, site_code, data, **kwargs):
        called['kwargs'] = kwargs
        return expected
    monkeypatch.setattr(f, 'browser_post_form', fake_browser)
    got = f.post_form(
        KinoshitaAdapter.SEARCH_URL,
        'KIN',
        [('area_key', '1')],
        referer=KinoshitaAdapter.AREA_URL,
        success_marker='search_result_housing_list',
    )
    assert got is expected
    assert called['kwargs']['referer'] == KinoshitaAdapter.AREA_URL


def test_http_200_with_kin_result_marker_is_accepted_without_length_heuristic(monkeypatch):
    html = '<html><div id="search_result_housing_list"></div></html>'
    f = Fetcher()
    f.session = _Session(_Response(html))
    def should_not_run(*args, **kwargs):
        raise AssertionError('browser fallback should not run')
    monkeypatch.setattr(f, 'browser_post_form', should_not_run)
    got = f.post_form(
        KinoshitaAdapter.SEARCH_URL,
        'KIN',
        [('area_key', '1')],
        referer=KinoshitaAdapter.AREA_URL,
        success_marker='search_result_housing_list',
    )
    assert got.status_code == 200
    assert got.html == html
