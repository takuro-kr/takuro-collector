from datetime import datetime, timedelta, timezone

import requests

import takuro_collector.collector as collector_module
from takuro_collector.collector import CollectorEngine
from takuro_collector.db import Database
from takuro_collector.fetcher import FetchFailed, FetchResult, Fetcher
from takuro_collector.models import PropertyCandidate
from takuro_collector.sites.amm import AMMAdapter
from takuro_collector.sites.base import DiscoveryResult


def candidate(room_id: str, *, rent: int = 33000) -> PropertyCandidate:
    return PropertyCandidate(
        source_site="otoku-chintai.com", source_property_id=room_id,
        management_company="アムス", building_name=f"AMM {room_id}", room="101",
        prefecture="東京都", address="東京都日野市", rent=rent,
        management_fee=4000, layout="1R", area=15.0,
        source_url=f"https://www.otoku-chintai.com/x/room{room_id}.html",
    )


def item(room_id: str, *, rent: int = 33000) -> dict:
    url = f"https://www.otoku-chintai.com/x/room{room_id}.html"
    return {"source_property_id": room_id, "source_url": url,
            "change_facts": {"rent": rent, "management_fee": 4000, "layout": "1R", "area": 15.0}}


class InventoryAMM(AMMAdapter):
    def __init__(self, items, failures=()):
        self.items = items
        self.failures = set(failures)
        self.detail_calls = []

    def discover(self, _fetcher):
        mapped = {row["source_url"]: row for row in self.items}
        return DiscoveryResult(
            list(mapped), listed_count=len(mapped), inventory_complete=True,
            inventory_site="otoku-chintai.com", inventory_items=mapped,
        )

    def collect_url(self, _fetcher, url):
        self.detail_calls.append(url)
        room_id = self.source_id(url)[0]
        if room_id in self.failures:
            raise FetchFailed(f"AMM: {url} HTTP 수집 실패 (timeout)")
        return candidate(room_id)


def run(monkeypatch, db, adapter, progress=None):
    monkeypatch.setattr(collector_module, "adapters", lambda: [adapter])
    return CollectorEngine(db).scan_all(progress)


def test_amm_unchanged_existing_skips_detail_gallery_and_photos(monkeypatch, tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.upsert_property(candidate("100"))
    adapter = InventoryAMM([item("100")])
    downloads = []
    monkeypatch.setattr(collector_module.PhotoManager, "download_for_property",
                        lambda self, pid: downloads.append(pid))
    result = run(monkeypatch, db, adapter)
    assert adapter.detail_calls == []
    assert downloads == []
    assert result.detail_attempted == 0 and result.existing_count == 1
    assert len(db.pending_inventory_snapshots()) == 1


def test_amm_changed_and_new_rooms_fetch_detail(monkeypatch, tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.upsert_property(candidate("101"))
    adapter = InventoryAMM([item("101", rent=34000), item("102")])
    result = run(monkeypatch, db, adapter)
    assert adapter.detail_calls == [item("101")["source_url"], item("102")["source_url"]]
    assert result.detail_attempted == 2


def test_amm_uses_same_thirty_day_ttl_as_other_inventory_adapters():
    adapter = AMMAdapter()
    now = datetime.now(timezone.utc)
    existing = {**candidate("103").to_dict(), "last_seen_at": (now - timedelta(days=31)).isoformat()}
    assert adapter.existing_inventory_action(existing, item("103"), now=now) == "ttl"
    existing["last_seen_at"] = (now - timedelta(days=29)).isoformat()
    assert adapter.existing_inventory_action(existing, item("103"), now=now) == "unchanged"


def test_amm_list_row_exposes_only_reliable_change_facts():
    html = '''<div class="list_area"><div class="list_detail2"><table>
    <tr name="151"><td>2階</td><td><span class="price">12.4</span>万円</td>
    <td>8,000円</td><td>1ヶ月</td><td>1ヶ月</td><td>1LDK</td><td>33.62㎡</td>
    <td class="detail btn"><a href="/x/room151.html">詳細</a></td></tr>
    </table></div></div>'''
    rows = AMMAdapter._inventory_items(html, "https://www.otoku-chintai.com/search/index/")
    assert rows == [{
        "source_property_id": "151",
        "source_url": "https://www.otoku-chintai.com/x/room151.html",
        "change_facts": {"rent": 124000, "management_fee": 8000, "layout": "1LDK", "area": 33.62},
    }]


def test_timeout_room_does_not_prevent_following_room(monkeypatch, tmp_path):
    db = Database(tmp_path / "db.sqlite")
    adapter = InventoryAMM([item("151"), item("152")], failures={"151"})
    result = run(monkeypatch, db, adapter)
    assert adapter.detail_calls == [item("151")["source_url"], item("152")["source_url"]]
    assert result.errors == 1 and result.new_count == 1
    assert item("151")["source_url"] in result.messages[1]


def test_amm_batch_photo_progress_reports_property_and_sampled_photo_position(monkeypatch, tmp_path):
    db = Database(tmp_path / "db.sqlite")
    adapter = InventoryAMM([item(str(room_id)) for room_id in range(1, 26)])
    photo_calls = []

    def download(_self, property_id, progress=None):
        photo_calls.append(property_id)
        for photo_index in range(1, 13):
            progress(f"사진 {photo_index}/12 다운로드", photo_index, 12)
        return {"downloaded": 12, "failed": 0}

    monkeypatch.setattr(collector_module.PhotoManager, "download_for_property", download)
    messages = []
    result = run(monkeypatch, db, adapter, lambda _code, message, *_args: messages.append(message))

    assert len(photo_calls) == 25
    assert result.new_count == 25
    assert any("batch 1/25" in message and "AMM 1 101" in message for message in messages)
    assert any("batch 25/25" in message and "AMM 25 101" in message for message in messages)
    assert any("사진 5/12 다운로드" in message for message in messages)
    assert any("사진 12/12 다운로드" in message for message in messages)
    assert not any("사진 2/12 다운로드" in message for message in messages)


def test_cancel_is_checked_immediately_after_timed_out_room(monkeypatch, tmp_path):
    class Cancelled(RuntimeError):
        user_cancelled = True

    db = Database(tmp_path / "db.sqlite")
    adapter = InventoryAMM([item("151"), item("152")], failures={"151"})
    progress_calls = []

    def progress(*args):
        progress_calls.append(args)
        if len(progress_calls) == 3:  # search start, room 151, then room 152 boundary
            raise Cancelled("cancelled")

    try:
        run(monkeypatch, db, adapter, progress)
    except Cancelled:
        pass
    else:
        raise AssertionError("cancellation must escape the site loop")
    assert adapter.detail_calls == [item("151")["source_url"]]


def test_fetch_without_browser_fallback_has_finite_http_timeout(monkeypatch):
    fetcher = Fetcher(timeout=7)
    calls = []

    def timeout(_url, **kwargs):
        calls.append(kwargs["timeout"])
        raise requests.Timeout("read timed out")

    monkeypatch.setattr(fetcher.session, "get", timeout)
    monkeypatch.setattr(fetcher, "browser_fetch",
                        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("browser fallback used")))
    try:
        fetcher.fetch("https://www.otoku-chintai.com/x/room151.html", "AMM", browser_fallback=False)
    except FetchFailed as exc:
        assert "read timed out" in str(exc)
    else:
        raise AssertionError("timeout must fail this room")
    assert calls == [7]


def test_amm_detail_and_gallery_disable_browser_fallback():
    class StaticFetcher:
        def __init__(self): self.calls = []
        def fetch(self, url, *_args, **kwargs):
            self.calls.append((url, kwargs))
            if "ajax/library" in url:
                return FetchResult(url, "<html><body>no photos</body></html>", 200)
            html = '''<div id="bkndetail"><div class="bknsummary"><table>
            <tr><th>部屋/所在階/階建</th><td>101 / 1階 / 3階建</td></tr>
            <tr><th>物件番号</th><td>151</td></tr><tr><th>所在地</th><td>東京都日野市</td></tr>
            <tr><th>賃料</th><td>3.3万円</td></tr><tr><th>管理費・共益費</th><td>4,000円</td></tr>
            <tr><th>間取り/詳細</th><td>1R</td></tr><tr><th>面積/バルコニー面積</th><td>15.00㎡ / -</td></tr>
            <tr><th>種別/構造</th><td>マンション / 鉄骨造</td></tr>
            </table></div></div><div class="h1_section"><h1>Test 101</h1></div>'''
            return FetchResult(url, html, 200)

    fetcher = StaticFetcher()
    AMMAdapter().collect_url(fetcher, "https://www.otoku-chintai.com/x/room151.html")
    assert len(fetcher.calls) == 2
    assert all(call[1]["browser_fallback"] is False for call in fetcher.calls)
