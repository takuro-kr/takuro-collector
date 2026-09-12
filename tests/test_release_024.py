from pathlib import Path

from takuro_collector import __version__
from takuro_collector.fetcher import Fetcher, FetchResult
from takuro_collector.sites.kinoshita import KinoshitaAdapter

FIX = Path(__file__).with_name('fixtures')


def test_release_version_024_or_newer():
    assert tuple(map(int, __version__.split('.'))) >= (0, 2, 4)


def test_real_kin_search_fixture_with_cloudflare_cdn_is_not_blocked():
    html = (FIX / 'kin_tokyo_page1.html').read_text(encoding='utf-8')
    assert 'cdnjs.cloudflare.com' in html.lower()
    assert Fetcher._looks_like_kin_search_result(html)
    assert not Fetcher._looks_blocked(200, html)
    total, rows = KinoshitaAdapter._parse_result_page(html)
    assert total == 121
    assert len(rows) == 10


def test_real_cloudflare_challenge_markers_are_blocked():
    samples = [
        '<html><body><div id="cf-chl-widget">challenge</div></body></html>',
        '<html><script src="/cdn-cgi/challenge-platform/h/g/orchestrate"></script></html>',
        '<html><body>Cloudflare Ray ID: abc123</body></html>',
        '<html><title>Just a moment...</title><body>Checking your browser</body></html>',
    ]
    for html in samples:
        assert Fetcher._looks_blocked(200, html)


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


def test_kin_valid_marker_and_cloudflare_cdn_is_accepted_without_browser(monkeypatch):
    html = (FIX / 'kin_tokyo_page1.html').read_text(encoding='utf-8')
    f = Fetcher()
    f.session = _Session(_Response(html))

    def should_not_run(*args, **kwargs):
        raise AssertionError('browser fallback should not run for a valid KIN result page')

    monkeypatch.setattr(f, 'browser_post_form', should_not_run)
    got = f.post_form(
        KinoshitaAdapter.SEARCH_URL,
        'KIN',
        [('area_key', '1')],
        referer=KinoshitaAdapter.AREA_URL,
        success_marker='search_result_housing_list',
    )
    assert got.status_code == 200
    total, rows = KinoshitaAdapter._parse_result_page(got.html)
    assert total == 121
    assert len(rows) == 10
