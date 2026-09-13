from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from ..extractor import clean_text
from ..fetcher import FetchFailed
from ..models import PropertyCandidate
from ..utils import extract_prefecture, normalize_room, parse_area, parse_yen
from .base import BaseAdapter, DiscoveryResult, ListingInactive


_HOST = "www.goodcomasset-gc.co.jp"
_SITE = "goodcomasset-gc.co.jp"
_SEARCH_PATH = "/search/index/"
_DETAIL_PATH = re.compile(r"^/bkndetail/(?P<building_id>\d+)/room(?P<room_id>\d+)/$")
_BUILDING_PATH = re.compile(r"^/bkndetail/(?P<building_id>\d+)/$")
_LAYOUT = re.compile(r"[0-9]+(?:SLDK|SDK|SK|LDK|DK|K|R)", re.I)
_REFERENCE_PHOTO = re.compile(r"(?:類似タイプ.*写真参考|別(?:部屋|住戸).*参考写真|他(?:室|住戸).*参考写真)")
_COMMON_PHOTO_WORDS = ("共用", "エントランス", "ロビー", "廊下", "メールボックス", "宅配ボックス", "エレベーター", "駐輪", "駐車")
_INTERIOR_PHOTO_WORDS = ("玄関", "居間", "リビング", "洋室", "和室", "キッチン", "浴室", "バルコニー", "トイレ", "洗面", "収納")


def _canonical_search(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() != _HOST or parsed.path != _SEARCH_PATH:
        return ""
    query = dict(parse_qsl(parsed.query))
    if query.get("class[]") != "c1" and query.get("class[0]") != "c1":
        return ""
    page = int(query.get("pg", "1")) if query.get("pg", "1").isdigit() else 0
    if page < 1:
        return ""
    values = [("class[]", "c1")]
    if page > 1:
        values.append(("pg", str(page)))
    return urlunsplit(("https", _HOST, _SEARCH_PATH, urlencode(values), ""))


def _identity(url: str) -> tuple[str, str] | None:
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() != _HOST:
        return None
    match = _DETAIL_PATH.fullmatch(parsed.path)
    return (match.group("building_id"), match.group("room_id")) if match else None


def _canonical_detail(url: str) -> str:
    identity = _identity(url)
    return f"https://{_HOST}/bkndetail/{identity[0]}/room{identity[1]}/" if identity else ""


def _building_url(url: str) -> str:
    parsed = urlsplit(urljoin(f"https://{_HOST}/", url))
    if parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() != _HOST:
        return ""
    match = _BUILDING_PATH.fullmatch(parsed.path)
    return f"https://{_HOST}/bkndetail/{match.group('building_id')}/" if match else ""


def _label(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", clean_text(value or "")))


def _pairs(soup: BeautifulSoup) -> tuple[dict[str, str], dict[str, object]]:
    values: dict[str, str] = {}
    nodes: dict[str, object] = {}
    for row in soup.select("dl.md-spec__list, dl.summary__list"):
        title, content = row.select_one("dt"), row.select_one("dd")
        if not title or not content:
            continue
        key = _label(title.get_text(" ", strip=True))
        if key and key not in values:
            # Text nodes such as <span>18.8</span>万円 are one value, not two
            # slash-separated fields. Literal slashes already present in the DOM
            # remain available for combined labels such as floor / total floors.
            values[key] = clean_text(content.get_text(" ", strip=True))
            nodes[key] = content
    return values, nodes


def _parts(value: str) -> list[str]:
    return [clean_text(part) for part in re.split(r"\s*/\s*", value or "")]


def _list_room_facts(row) -> dict[str, object]:
    cells = row.find_all("td", recursive=False)
    facts: dict[str, object] = {}
    if len(cells) < 9:
        return facts
    rent, fee = parse_yen(cells[3].get_text(" ", strip=True)), parse_yen(cells[4].get_text(" ", strip=True))
    layout_match = _LAYOUT.search(clean_text(cells[7].get_text(" ", strip=True)))
    area = parse_area(cells[8].get_text(" ", strip=True))
    if rent:
        facts["rent"] = rent
    if clean_text(cells[4].get_text(" ", strip=True)):
        facts["management_fee"] = fee
    if layout_match:
        facts["layout"] = layout_match.group(0).upper()
    if area is not None:
        facts["area"] = area
    return facts


def _search_page(html: str, page_url: str) -> tuple[dict[str, int], dict[str, dict], list[str], int]:
    soup = BeautifulSoup(html, "html.parser")
    buildings: dict[str, int] = {}
    visible_rooms: dict[str, dict] = {}
    for anchor in soup.select("a[href*='/bkndetail/']"):
        building = _building_url(str(anchor.get("href") or ""))
        if not building:
            continue
        match = re.search(r"すべて見る\s*[（(]\s*全\s*([0-9０-９,，]+)\s*件", clean_text(anchor.get_text(" ", strip=True)))
        buildings[building] = max(buildings.get(building, 0), int(re.sub(r"\D", "", unicodedata.normalize("NFKC", match.group(1)))) if match else 0)
    for row in soup.select(".empty-list .empty-list__table tr[name]"):
        anchor = row.select_one("a[href*='/room']")
        url = _canonical_detail(urljoin(page_url, str(anchor.get("href") or ""))) if anchor else ""
        identity = _identity(url)
        if identity:
            facts = _list_room_facts(row)
            visible_rooms[url] = {
                "source_property_id": identity[1], "source_url": url, "change_facts": facts,
                "active_looking": bool(row.select_one("input[name='bknId[]']")) and bool(facts.get("rent")),
            }
    pages: list[str] = []
    for anchor in soup.select(".pager a[href*='pg='], a[href*='/search/index/'][href*='pg=']"):
        target = _canonical_search(urljoin(page_url, str(anchor.get("href") or "")))
        if target and target not in pages:
            pages.append(target)
    count = soup.select_one(".result__cnt")
    numbers = [int(unicodedata.normalize("NFKC", node.get_text(" ", strip=True)).replace(",", ""))
               for node in count.select("span") if node.get_text(" ", strip=True).replace(",", "").isdigit()] if count else []
    reported_rooms = numbers[1] if len(numbers) >= 2 else 0
    return buildings, visible_rooms, pages, reported_rooms


def _building_rooms(html: str, page_url: str) -> tuple[list[dict], int, int]:
    soup = BeautifulSoup(html, "html.parser")
    result: list[dict] = []
    cards = soup.select("#target-allview li.detail-bkn__items")
    malformed = 0
    page_building = _building_url(page_url)
    page_identity = _BUILDING_PATH.fullmatch(urlsplit(page_building).path) if page_building else None
    building_id = page_identity.group("building_id") if page_identity else ""
    for card in cards:
        anchor = card.select_one("a.detail-bkn__link[href*='/room']")
        url = _canonical_detail(urljoin(page_url, str(anchor.get("href") or ""))) if anchor else ""
        identity = _identity(url)
        text_nodes = card.select(".detail-bkn__text")
        room_display = normalize_room(text_nodes[0].get_text(" ", strip=True)) if text_nodes else ""
        if not identity or identity[0] != building_id or not room_display:
            malformed += 1
            continue
        facts: dict[str, object] = {}
        price = card.select_one(".detail-bkn__price")
        rent = parse_yen(price.get_text(" ", strip=True)) if price else 0
        area = parse_area(text_nodes[1].get_text(" ", strip=True)) if len(text_nodes) > 1 else None
        if rent:
            facts["rent"] = rent
        if area is not None:
            facts["area"] = area
        result.append({"source_property_id": identity[1], "source_url": url, "change_facts": facts,
                       "_building_id": building_id, "_room_display": room_display})
    return result, len(cards), malformed


def _transport(node) -> list[dict]:
    result: list[dict] = []
    if not node:
        return result
    for paragraph in node.select("p") or [node]:
        raw = clean_text(paragraph.get_text(" ", strip=True))
        if not raw:
            continue
        route: dict = {"raw": raw}
        links = paragraph.select("a")
        walk = re.search(r"徒歩\s*([0-9０-９]+)\s*分", raw)
        bus = re.search(r"バス\s*([0-9０-９]+)\s*分", raw)
        if links:
            route["line"] = clean_text(links[0].get_text(" ", strip=True))
        if len(links) > 1:
            route["station"] = clean_text(links[1].get_text(" ", strip=True)).removesuffix("駅")
        route["walk_minutes"] = int(unicodedata.normalize("NFKC", walk.group(1))) if walk else None
        route["bus_minutes"] = int(unicodedata.normalize("NFKC", bus.group(1))) if bus else None
        result.append(route)
    return result[:10]


def _photo_sources(soup: BeautifulSoup, room_id: str) -> list[dict]:
    result: list[dict] = []
    seen: set[str] = set()
    for anchor in soup.select("#js-gallery a.js-gallery-thum[data-note]"):
        image = anchor.select_one("img[src]")
        if not image:
            continue
        parsed = urlsplit(urljoin(f"https://{_HOST}/", str(image.get("src") or "").strip()))
        if parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() != "cdn.img-asp.jp":
            continue
        if not re.fullmatch(rf"/bkn/{re.escape(room_id)}_\d+_\d+_\d+_\d+\.(?:jpe?g|png|webp)", parsed.path, re.I):
            continue
        url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
        if url in seen:
            continue
        note_html = str(anchor.get("data-note") or "")
        note = clean_text(BeautifulSoup(note_html, "html.parser").get_text(" ", strip=True))
        alt = clean_text(str(image.get("alt") or image.get("title") or ""))
        evidence = clean_text(" | ".join(value for value in (note, alt) if value))
        if "間取り" in evidence or "間取" in evidence:
            kind = "floorplan"
        elif "外観" in evidence:
            kind = "exterior"
        elif _REFERENCE_PHOTO.search(evidence):
            kind = "reference_photo"
        elif any(word in evidence for word in _COMMON_PHOTO_WORDS):
            kind = "common_area"
        elif any(word in evidence for word in _INTERIOR_PHOTO_WORDS):
            kind = "interior"
        else:
            kind = "other"
        seen.add(url)
        result.append({"url": url, "alt": evidence, "kind": kind})
    return result


class GoodComAdapter(BaseAdapter):
    code = "GOO"
    label = "Goodcom / 株式会社グッドコム"
    domains = (_SITE,)
    management_company = "goo"
    seed_urls = ("https://www.goodcomasset-gc.co.jp/search/index/?class%5B%5D=c1",)
    detail_patterns = (r"/bkndetail/\d+/room(\d+)/?(?:$|[?#])",)
    force_browser = False
    login_expected = False
    detail_refresh_ttl = timedelta(days=30)

    def discover(self, fetcher) -> DiscoveryResult:
        queue = list(self.seed_urls)
        pages_seen: set[str] = set()
        buildings: dict[str, int] = {}
        visible_rooms: dict[str, dict] = {}
        reported_rooms = 0
        any_browser = False
        while queue:
            page_url = queue.pop(0)
            if page_url in pages_seen:
                continue
            page = fetcher.fetch(page_url, self.code, force_browser=False, login_expected=False, browser_fallback=False)
            pages_seen.add(page_url)
            any_browser = any_browser or page.via_browser
            found_buildings, found_rooms, pagination, reported = _search_page(page.html, page.url)
            reported_rooms = max(reported_rooms, reported)
            for url, count in found_buildings.items():
                buildings[url] = max(buildings.get(url, 0), count)
            visible_rooms.update(found_rooms)
            for target in pagination:
                if target not in pages_seen and target not in queue:
                    queue.append(target)

        inventory: dict[str, dict] = {}
        building_failures = 0
        malformed_cards = 0
        raw_cards = 0
        room_ids: list[str] = []
        canonical_urls: list[str] = []
        building_rooms: list[tuple[str, str]] = []
        for index, building_url in enumerate(buildings, start=1):
            cancel_check = getattr(self, "cancel_check", None)
            if cancel_check:
                cancel_check(index - 1)
            try:
                page = fetcher.fetch(building_url, self.code, force_browser=False, login_expected=False, browser_fallback=False)
            except Exception:
                building_failures += 1
                continue
            any_browser = any_browser or page.via_browser
            found, card_count, malformed = _building_rooms(page.html, page.url)
            raw_cards += card_count
            malformed_cards += malformed
            if not page.html or not BeautifulSoup(page.html, "html.parser").select_one("#target-allview"):
                building_failures += 1
                continue
            for item in found:
                richer = visible_rooms.get(item["source_url"])
                if richer:
                    item["change_facts"].update(richer.get("change_facts") or {})
                room_ids.append(str(item["source_property_id"]))
                canonical_urls.append(str(item["source_url"]))
                building_rooms.append((str(item.pop("_building_id")), str(item.pop("_room_display"))))
                inventory.setdefault(item["source_url"], item)

        urls = list(inventory)
        active_search_urls = {url for url, item in visible_rooms.items() if item.get("active_looking")}
        duplicate_ids = len(room_ids) != len(set(room_ids))
        duplicate_urls = len(canonical_urls) != len(set(canonical_urls))
        duplicate_building_rooms = len(building_rooms) != len(set(building_rooms))
        active_missing = active_search_urls - set(urls)
        complete = bool(
            buildings and not building_failures and raw_cards and not malformed_cards
            and not duplicate_ids and not duplicate_urls and not duplicate_building_rooms
            and not active_missing and len(urls) == raw_cards
        )
        stale_search_only = set(visible_rooms) - set(urls)
        messages = [
            f"GOO 검색 {len(pages_seen)}페이지 / 건물 {len(buildings)}개 / 현재 모집 호실 {len(urls)}개",
            f"GOO 검색 표시 {reported_rooms}건 / 검색 전용 비선택 행 {len(stale_search_only)}건",
        ]
        if not complete:
            messages.append(
                f"GOO 완전 재고 보류 - 건물 실패 {building_failures}, 비정상 card {malformed_cards}, "
                f"ID중복 {duplicate_ids}, URL중복 {duplicate_urls}, 건물+호실중복 {duplicate_building_rooms}, "
                f"검색 active 누락 {len(active_missing)}"
            )
        return DiscoveryResult(urls, any_browser, listed_count=len(urls), messages=messages,
                               inventory_complete=complete, inventory_site=_SITE, inventory_items=inventory)

    def existing_inventory_action(self, existing: dict, item: dict, *, now: datetime | None = None) -> str:
        facts = dict(item.get("change_facts") or {})
        for key in ("rent", "management_fee", "layout", "area"):
            if key not in facts:
                continue
            old, new = existing.get(key), facts[key]
            if key == "area":
                if old is None or abs(float(old) - float(new)) > 0.001:
                    return "changed"
            elif key == "layout":
                match = _LAYOUT.search(str(old or ""))
                if not match or match.group(0).upper() != str(new).upper():
                    return "changed"
            elif int(old or 0) != int(new or 0):
                return "changed"
        try:
            refreshed = datetime.fromisoformat(str(existing.get("last_seen_at") or ""))
            if refreshed.tzinfo is None:
                refreshed = refreshed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return "ttl"
        current = now or datetime.now(timezone.utc).astimezone()
        return "ttl" if current - refreshed >= self.detail_refresh_ttl else "unchanged"

    def collect_url(self, fetcher, url: str) -> PropertyCandidate:
        requested = _identity(url)
        if not requested:
            raise ValueError("GOO 상세 URL 형식 확인 실패")
        try:
            detail = fetcher.fetch(url, self.code, force_browser=False, login_expected=False, browser_fallback=False)
        except FetchFailed as exc:
            if re.search(r"HTTP\s+(?:404|410)\b", str(exc)):
                raise ListingInactive("GOO 삭제/비공개 매물") from exc
            raise
        if detail.status_code in {404, 410}:
            raise ListingInactive("GOO 삭제/비공개 매물")
        if _identity(detail.url) != requested:
            if _building_url(detail.url) == f"https://{_HOST}/bkndetail/{requested[0]}/":
                raise ListingInactive("GOO 모집 종료 후 건물 페이지 이동")
            raise ValueError("GOO 상세 최종 URL 확인 실패")
        return self.parse(detail.html, detail.url)

    def parse(self, html: str, url: str) -> PropertyCandidate:
        identity = _identity(url)
        if not identity:
            raise ValueError("GOO 상세 URL 형식 확인 실패")
        building_id, room_id = identity
        soup = BeautifulSoup(html, "html.parser")
        pairs, nodes = _pairs(soup)
        if not pairs:
            raise ValueError("GOO 현재 호실 상세 영역 추출 실패")
        building = next((clean_text(a.get_text(" ", strip=True)) for a in soup.select("a.breadcrumbs__link[href]")
                         if _building_url(str(a.get("href") or "")) == f"https://{_HOST}/bkndetail/{building_id}/"), "")
        current = soup.select_one("strong.breadcrumbs__link")
        room = normalize_room(current.get_text(" ", strip=True) if current else "")
        address, rent = pairs.get("所在地", ""), parse_yen(pairs.get("賃料", ""))
        layout_text = pairs.get("間取り/詳細", pairs.get("間取り", ""))
        layout_match = _LAYOUT.search(layout_text)
        layout = layout_match.group(0).upper() if layout_match else ""
        area = parse_area(pairs.get("専有面積", ""))
        displayed_id = re.sub(r"\D", "", unicodedata.normalize("NFKC", pairs.get("物件番号", "")))
        if displayed_id and displayed_id != room_id:
            raise ValueError("GOO URL과 물건번호 불일치")
        missing = [name for name, value in (("건물명", building), ("호실", room), ("주소", address),
                                               ("월세", rent), ("방 구조", layout), ("전용면적", area))
                   if value is None or value == "" or value == 0]
        if missing:
            raise ValueError("GOO 필수 매물 정보 누락: " + ", ".join(missing))
        floors = _parts(pairs.get("所在階/階建て", ""))
        deposit = pairs.get("敷金", "") or _parts(pairs.get("敷金(保証金)", ""))[0]
        equipment: list[str] = []
        for node in soup.select(".detail__facility .md-facility__note"):
            value = clean_text(node.get_text(" ", strip=True))
            if value and value not in equipment:
                equipment.append(value)
        for key in ("設備条件", "設備(その他)"):
            for value in re.split(r"[、,/／\n]", pairs.get(key, "")):
                value = clean_text(value)
                if value and value not in equipment:
                    equipment.append(value)
        return PropertyCandidate(
            source_site=_SITE, source_property_id=room_id, management_company=self.management_company,
            building_name=building, room=room, prefecture=extract_prefecture(address), address=address,
            source_url=_canonical_detail(url), rent=rent,
            management_fee=parse_yen(pairs.get("管理/共益費", "")), deposit=deposit,
            key_money=pairs.get("礼金", ""), layout=layout, area=area, built_date=pairs.get("築年月", ""),
            floor=floors[0] if floors else "", total_floors=floors[1] if len(floors) > 1 else "",
            orientation=pairs.get("向き", ""), move_in_date=pairs.get("入居可能時期", ""),
            structure=pairs.get("建物構造", ""), transport=_transport(nodes.get("交通")), equipment=equipment,
            photo_sources=_photo_sources(soup, room_id), source_id_kind="site", scrape_warnings=[],
            collected_info={"site_property_number": displayed_id or room_id},
        )
