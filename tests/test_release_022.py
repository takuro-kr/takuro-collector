from pathlib import Path

from takuro_collector import __version__
from takuro_collector.sites.kinoshita import KinoshitaAdapter

FIX = Path(__file__).with_name("fixtures")


def test_release_version_022_or_newer():
    assert tuple(map(int, __version__.split("."))) >= (0, 2, 2)


def test_kin_area_extracts_tokyo_all_localities_from_real_html():
    html = (FIX / "kin_area.html").read_text(encoding="utf-8")
    ids = KinoshitaAdapter._search_locality_ids(html, "13__")
    assert len(ids) == 42
    assert "13__101" in ids
    assert "13__123" in ids
    assert "13__303" in ids


def test_kin_real_tokyo_result_reports_121_and_room_rows():
    html = (FIX / "kin_tokyo_page1.html").read_text(encoding="utf-8")
    total, rows = KinoshitaAdapter._parse_result_page(html)
    assert total == 121
    assert len(rows) == 10
    first = rows[0]
    assert first["building_name"] == "アムール TODORI"
    assert first["room"] == "102"
    assert first["address"] == "東京都八王子市廿里町21-1"
    assert first["source_property_id"] == "9449_2"
    assert first["rent"] == 59000
    assert first["management_fee"] == 3500


def test_kin_result_rows_are_unique_detail_urls():
    html = (FIX / "kin_tokyo_page1.html").read_text(encoding="utf-8")
    _, rows = KinoshitaAdapter._parse_result_page(html)
    urls = [r["url"] for r in rows]
    assert len(urls) == len(set(urls))
    assert all("/details/" in u and u.endswith("details.html") for u in urls)

class _FakeFetchResult:
    def __init__(self, html, url):
        self.html = html
        self.url = url
        self.via_browser = False


class _FakeFetcher:
    def __init__(self, area_html, pages):
        self.area_html = area_html
        self.pages = pages
        self.posts = []

    def fetch(self, url, site_code, **kwargs):
        return _FakeFetchResult(self.area_html, url)

    def post_form(self, url, site_code, data, **kwargs):
        pairs = list(data)
        page_num = next((v for k, v in pairs if k == "page_num"), "1")
        page_cnt = next((v for k, v in pairs if k == "page_cnt"), "10")
        area_key = next((v for k, v in pairs if k == "area_key"), "")
        self.posts.append((area_key, page_num, page_cnt, pairs))
        # Only Tokyo returns fixtures; other areas return an empty valid result page.
        if area_key != "1":
            return _FakeFetchResult("<html><body><div class='change_list_count'>0件中</div></body></html>", url)
        if page_cnt == "1000000":
            # Simulate server ignoring ALL and returning page 1 only.
            return _FakeFetchResult(self.pages[1], url)
        return _FakeFetchResult(self.pages[int(page_num)], url)


def _mutate_page(html: str, page_num: int, total: int = 21) -> str:
    # Create distinct detail URLs while preserving real current KIN structure.
    import re
    html = re.sub(r"1～10/<span class=\"red\">121</span>件中", f"1～10/<span class=\"red\">{total}</span>件中", html)
    return re.sub(r"/details/(\d+_\d+)details\.html", lambda m: f"/details/{page_num}{m.group(1)}details.html", html)


def test_kin_discover_falls_back_to_post_pagination_when_all_is_ignored():
    area_html = (FIX / "kin_area.html").read_text(encoding="utf-8")
    real = (FIX / "kin_tokyo_page1.html").read_text(encoding="utf-8")
    pages = {1: _mutate_page(real, 1), 2: _mutate_page(real, 2), 3: _mutate_page(real, 3)}
    fetcher = _FakeFetcher(area_html, pages)
    result = KinoshitaAdapter().discover(fetcher)
    # 21 expected -> 3 POST pages at 10/page. The third fixture still contains 10 rows,
    # which is fine for discovery dedupe; this test verifies traversal, not server truncation.
    tokyo_pages = [(a, p, c) for a, p, c, _ in fetcher.posts if a == "1"]
    assert ("1", "1", "1000000") in tokyo_pages
    assert ("1", "1", "10") in tokyo_pages
    assert ("1", "2", "10") in tokyo_pages
    assert ("1", "3", "10") in tokyo_pages
    assert len(result.urls) == 30
