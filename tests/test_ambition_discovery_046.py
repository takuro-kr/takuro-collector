from types import SimpleNamespace

import pytest

import takuro_collector.collector as collector_module
from takuro_collector.collector import CollectorEngine
from takuro_collector.sites.ambition import AmbitionAdapter
from takuro_collector.sites.base import ListingInactive


def result(url, html, status=200):
    return SimpleNamespace(url=url, html=html, status_code=status, via_browser=False)


class Fetcher:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def fetch(self, url, *_args, **_kwargs):
        self.calls.append(url)
        return self.pages[url]


def room_table(links, extra="", next_url=""):
    rows = "".join(f'<tr><td><a href="{url}">物件詳細</a></td></tr>' for url in links)
    next_link = f'<a class="next" href="{next_url}">次へ</a>' if next_url else ""
    return f'<div class="item_room_table"><table class="check_table">{rows}</table></div>{extra}{next_link}'


def test_ambition_uses_only_four_supported_prefecture_seeds():
    assert AmbitionAdapter.seed_urls == (
        "https://pm.am-bition.jp/rent_search/%E6%9D%B1%E4%BA%AC%E9%83%BD",
        "https://pm.am-bition.jp/rent_search/%E5%8D%83%E8%91%89%E7%9C%8C",
        "https://pm.am-bition.jp/rent_search/%E5%9F%BC%E7%8E%89%E7%9C%8C",
        "https://pm.am-bition.jp/rent_search/%E7%A5%9E%E5%A5%88%E5%B7%9D%E7%9C%8C",
    )


def test_live_room_scope_paginates_over_100_dedupes_and_excludes_other_areas():
    adapter = AmbitionAdapter()
    tokyo, page2 = adapter.seed_urls[0], adapter.seed_urls[0] + "/page:2"
    first = [f"/rent/{1000 + i}/{2000 + i}" for i in range(101)]
    excluded = """
      <div id="empty_rooms"><a href="/rent/9/9001">過去物件</a></div>
      <div class="like_item"><a href="/rent/9/9002">おすすめ</a></div>
      <template><a href="/rent/9/9003">hidden</a></template>
    """
    pages = {tokyo: result(tokyo, room_table(first, excluded, page2)),
             page2: result(page2, room_table([first[-1], "/rent/3000/4000"]))}
    for seed in adapter.seed_urls[1:]:
        pages[seed] = result(seed, room_table([]))
    fetcher = Fetcher(pages)

    found = adapter.discover(fetcher)

    assert len(found.urls) == 102
    assert found.urls[-1] == "https://pm.am-bition.jp/rent/3000/4000"
    assert not any(url.endswith(("/9001", "/9002", "/9003")) for url in found.urls)
    assert fetcher.calls == [tokyo, page2, *adapter.seed_urls[1:]]


@pytest.mark.parametrize("status,html", [
    (404, "<html><body>not found</body></html>"),
    (200, "<html><body>404 ERROR PAGE NOT FOUND / ページが見つかりませんでした</body></html>"),
])
def test_http_and_custom_404_are_inactive_not_parser_errors(status, html):
    url = "https://pm.am-bition.jp/rent/2211/28667"
    with pytest.raises(ListingInactive, match="삭제/비공개"):
        AmbitionAdapter().collect_url(Fetcher({url: result(url, html, status)}), url)


class FakeDB:
    def __init__(self): self.finished = None
    def start_scan(self, _kind): return 1
    def get_bool(self, *_args): return True
    def set_site_status(self, *_args, **_kwargs): pass
    def finish_scan(self, _scan_id, values): self.finished = values
    def save_inventory_snapshot(self, *_args, **_kwargs): return {"expected_count": 1}
    def properties_by_source_site(self, _site): return {}


def test_full_scan_counts_404_as_inactive_skip(monkeypatch):
    adapter = AmbitionAdapter()
    url = "https://pm.am-bition.jp/rent/2211/28667"
    pages = {seed: result(seed, room_table([url]) if i == 0 else room_table([]))
             for i, seed in enumerate(adapter.seed_urls)}
    pages[url] = result(url, "404 ERROR PAGE NOT FOUND / ページが見つかりませんでした", 404)
    db = FakeDB()
    engine = CollectorEngine(db)
    engine.fetcher = Fetcher(pages)
    monkeypatch.setattr(collector_module, "adapters", lambda: [adapter])

    scan = engine.scan_all()

    assert scan.inactive_skipped == 1
    assert scan.skipped_region_or_parse == 0
    assert scan.errors == 0
    assert scan.discovered == 0
