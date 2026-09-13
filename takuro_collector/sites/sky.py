from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from ..extractor import clean_text
from ..fetcher import FetchFailed
from ..models import PropertyCandidate
from ..utils import canonical_host, extract_prefecture, normalize_room, parse_area, parse_yen
from .base import BaseAdapter, DiscoveryResult, ListingInactive


_DETAIL_PATH = re.compile(r"^/build-(?P<building_id>\d+)/room-(?P<room_id>\d+)\.html$")
_PAGE_PATH = re.compile(r"^/search-result/page-(?P<page>\d+)\.html$")
_ROOM_IMAGE = re.compile(r"^cl_img/room_other_img_\d+/room_\d+_(?P<room_id>\d+)_\d+\.(?:jpe?g|png|webp)$", re.I)
_LAYOUT_IMAGE = re.compile(r"^cl_img/room_layout_img/(?:layout|room)_\d+_(?P<room_id>\d+)(?:_\d+)?\.(?:jpe?g|png|webp)$", re.I)
_BUILD_IMAGE = re.compile(r"^cl_img/build_photo_img/build_\d+_(?P<building_id>\d+)\.(?:jpe?g|png|webp)$", re.I)
_BUILD_OTHER_IMAGE = re.compile(r"^cl_img/build_other_img_\d+/build_\d+_(?P<building_id>\d+)_\d+\.(?:jpe?g|png|webp)$", re.I)
_COMMON_WORDS = ("エントランス", "ロビー", "廊下", "メールボックス", "宅配ボックス", "エレベーター", "駐輪場", "共用")


def _identity(url: str) -> tuple[str, str] | None:
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() != "www.skyc-chintai.jp":
        return None
    match = _DETAIL_PATH.fullmatch(parsed.path)
    return (match.group("building_id"), match.group("room_id")) if match else None


def _canonical_detail(url: str) -> str:
    identity = _identity(url)
    return f"https://www.skyc-chintai.jp/build-{identity[0]}/room-{identity[1]}.html" if identity else ""


def _pairs(root) -> dict[str, str]:
    result: dict[str, str] = {}
    if not root:
        return result
    for item in root.select(".detail-list-item"):
        title = item.select_one(".detail-list-item-title")
        content = item.select_one(".detail-list-item-content")
        if title and content:
            result[clean_text(title.get_text(" ", strip=True))] = clean_text(content.get_text(" ", strip=True))
    return result


def _parts(value: str) -> list[str]:
    return [clean_text(item) for item in re.split(r"[/／]", value or "")]


def _transport(root) -> list[dict]:
    if not root:
        return []
    item = next((node for node in root.select(".detail-list-item")
                 if clean_text((node.select_one(".detail-list-item-title") or node).get_text(" ", strip=True)) == "交通"), None)
    content = item.select_one(".detail-list-item-content") if item else None
    if not content:
        return []
    for br in content.select("br"):
        br.replace_with(" ||| ")
    lines = [clean_text(line) for line in content.get_text(" ", strip=True).split("|||") if clean_text(line)]
    links = content.select("a")
    result: list[dict] = []
    for index, raw in enumerate(lines):
        route: dict = {"raw": raw}
        walk = re.search(r"徒歩\s*([0-9０-９]+)\s*分", raw)
        if walk and len(links) >= (index + 1) * 2:
            route.update({
                "line": clean_text(links[index * 2].get_text(" ", strip=True)),
                "station": clean_text(links[index * 2 + 1].get_text(" ", strip=True)).removesuffix("駅"),
                "walk_minutes": int(unicodedata.normalize("NFKC", walk.group(1))),
                "bus_minutes": None,
            })
        result.append(route)
    return result[:10]


def _original_image(raw_url: str) -> str:
    parsed = urlsplit(raw_url)
    if parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() != "image.reblo.net":
        return ""
    query = parse_qs(parsed.query)
    if parsed.path == "/img_thumb.php":
        path = unquote(query.get("f", [""])[0])
    elif parsed.path == "/img_thumbnail.php":
        directory = unquote(query.get("dir", [""])[0])
        filename = unquote(query.get("nm", [""])[0])
        path = f"{directory.rstrip('/')}/{filename}" if directory and filename else ""
    else:
        path = parsed.path
    path = path.removeprefix("./").lstrip("/")
    if not path.startswith("cl_img/") or ".." in path.split("/"):
        return ""
    return f"https://image.reblo.net/{path}"


def _facility_photo_urls(soup: BeautifulSoup) -> list[str]:
    """Return only images categorized by SKY's explicit nearby-facilities DOM."""
    result: list[str] = []
    for image in soup.select("#facilities .facilities-photo img"):
        url = _original_image(str(image.get("data-src") or image.get("src") or "").strip())
        if url and "/cl_img/build_facility_img_" in url and url not in result:
            result.append(url)
    return result


def _exclude_facility_copies(soup: BeautifulSoup, photos: list[dict], download) -> list[dict]:
    """Remove gallery aliases that are byte-identical to explicit facility images.

    SKY sometimes copies a #facilities image into room_other_img with the generic
    caption その他画像.  The two URLs have no shared ID, so exact source-byte identity
    is the only page-backed relation. Failed/ambiguous comparisons retain the photo.
    """
    facility_hashes: set[str] = set()
    for url in _facility_photo_urls(soup):
        try:
            facility_hashes.add(hashlib.sha256(download(url)).hexdigest())
        except Exception:
            continue
    if not facility_hashes:
        return photos
    result: list[dict] = []
    for photo in photos:
        if photo.get("alt") != "その他画像":
            result.append(photo)
            continue
        try:
            digest = hashlib.sha256(download(str(photo["url"]))).hexdigest()
        except Exception:
            result.append(photo)
            continue
        if digest not in facility_hashes:
            result.append(photo)
    return result


def _photo_sources(soup: BeautifulSoup, building_id: str, room_id: str) -> list[dict]:
    result: list[dict] = []
    seen: set[str] = set()
    # #facilities uses the separate build_facility_img_* family and must never
    # be treated as listing photography. The room plan is rendered separately
    # from the gallery in the live detail template.
    images = [*soup.select("#photo-gallery img"), *soup.select("section#room-detail #room-layout-photo img")]
    for image in images:
        if image.find_parent(id="facilities"):
            continue
        url = _original_image(str(image.get("data-src") or image.get("src") or "").strip())
        if not url or url in seen:
            continue
        path = urlsplit(url).path.lstrip("/")
        label = clean_text(str(image.get("alt") or image.get("title") or ""))
        if "周辺" in label:
            continue
        room_match, layout_match = _ROOM_IMAGE.fullmatch(path), _LAYOUT_IMAGE.fullmatch(path)
        build_match, other_match = _BUILD_IMAGE.fullmatch(path), _BUILD_OTHER_IMAGE.fullmatch(path)
        if room_match and room_match.group("room_id") == room_id:
            kind = "interior"
        elif layout_match and layout_match.group("room_id") == room_id:
            kind = "floorplan"
        elif build_match and build_match.group("building_id") == building_id:
            kind = "exterior" if "外観" in label else "other"
        elif other_match and other_match.group("building_id") == building_id:
            kind = "common_area" if any(word in label for word in _COMMON_WORDS) else "other"
        else:
            continue
        seen.add(url)
        result.append({"url": url, "alt": label, "kind": kind})
    return result


class SKYAdapter(BaseAdapter):
    code = "SKY"
    label = "スカイコート"
    domains = ("skyc-chintai.jp",)
    management_company = "スカイコート"
    seed_urls = ("https://www.skyc-chintai.jp/search-result/page-1.html?page_disp=30",)
    detail_patterns = (r"/build-\d+/room-(\d+)\.html(?:$|[?#])",)
    force_browser = False
    login_expected = False
    detail_refresh_ttl = timedelta(days=30)

    @staticmethod
    def _list_page(html: str, page_url: str) -> tuple[list[dict], list[str], int]:
        soup = BeautifulSoup(html, "html.parser")
        items: list[dict] = []
        for card in soup.select("article.room-card"):
            anchor = card.select_one(".article-card-title a[href]") or card.select_one("a[href*='/room-']")
            url = _canonical_detail(urljoin(page_url, str(anchor.get("href") or ""))) if anchor else ""
            identity = _identity(url)
            if not identity:
                continue
            facts: dict[str, object] = {}
            price, fee = card.select_one(".price"), card.select_one(".maint_fee")
            layout, area = card.select_one(".layout"), card.select_one(".exc_area")
            if price:
                price_text = clean_text(price.get_text(" ", strip=True))
                fee_text = clean_text(fee.get_text(" ", strip=True)) if fee else ""
                facts["rent"] = parse_yen(price_text.replace(fee_text, ""))
            if fee:
                facts["management_fee"] = parse_yen(fee.get_text(" ", strip=True))
            if layout:
                match = re.search(r"[0-9]+(?:SLDK|LDK|DK|K|R)", clean_text(layout.get_text(" ", strip=True)), re.I)
                if match:
                    facts["layout"] = match.group(0).upper()
            parsed_area = parse_area(area.get_text(" ", strip=True)) if area else None
            if parsed_area is not None:
                facts["area"] = parsed_area
            items.append({"source_property_id": identity[1], "source_url": url, "change_facts": facts})

        pages: list[str] = []
        for anchor in soup.select("a[href*='/search-result/page-']"):
            target = urljoin(page_url, str(anchor.get("href") or ""))
            parsed, match = urlsplit(target), _PAGE_PATH.fullmatch(urlsplit(target).path)
            if parsed.scheme.lower() == "https" and (parsed.hostname or "").lower() == "www.skyc-chintai.jp" and match:
                pages.append(f"https://www.skyc-chintai.jp/search-result/page-{int(match.group('page'))}.html?page_disp=30")
        text = clean_text(soup.get_text(" ", strip=True))
        count = re.search(r"([0-9０-９,，]+)\s*件中", unicodedata.normalize("NFKC", text))
        listed = int(re.sub(r"\D", "", count.group(1))) if count else 0
        return items, pages, listed

    def discover(self, fetcher) -> DiscoveryResult:
        queue = list(self.seed_urls)
        seen_pages: set[int] = set()
        items_by_url: dict[str, dict] = {}
        listed_count = 0
        any_browser = False
        while queue:
            page_url = queue.pop(0)
            match = _PAGE_PATH.fullmatch(urlsplit(page_url).path)
            page_number = int(match.group("page")) if match else 1
            if page_number in seen_pages:
                continue
            cancel_check = getattr(self, "cancel_check", None)
            if cancel_check:
                cancel_check(page_number - 1)
            page = fetcher.fetch(page_url, self.code, force_browser=False, login_expected=False)
            seen_pages.add(page_number)
            any_browser = any_browser or page.via_browser
            found, links, reported = self._list_page(page.html, page.url)
            listed_count = max(listed_count, reported)
            for item in found:
                items_by_url.setdefault(item["source_url"], item)
            for target in links:
                target_match = _PAGE_PATH.fullmatch(urlsplit(target).path)
                target_number = int(target_match.group("page")) if target_match else 0
                if target_number and target_number not in seen_pages and target not in queue:
                    queue.append(target)
        urls = list(items_by_url)
        complete = bool(listed_count and len(urls) == listed_count)
        messages = [f"SKY 페이지 {len(seen_pages)} / 고유 상세URL {len(urls)}"]
        if listed_count and not complete:
            messages.append(f"SKY 표시 공실 {listed_count}건과 상세URL {len(urls)}건 차이 확인 필요")
        return DiscoveryResult(urls, any_browser, listed_count=listed_count, messages=messages,
                               inventory_complete=complete, inventory_site="skyc-chintai.jp",
                               inventory_items=items_by_url)

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
                match = re.search(r"[0-9]+(?:SLDK|LDK|DK|K|R)", str(old or ""), re.I)
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
            raise ValueError("SKY 상세 URL 형식 확인 실패")
        try:
            detail = fetcher.fetch(url, self.code, force_browser=False, login_expected=False)
        except FetchFailed as exc:
            if re.search(r"HTTP\s+(?:404|410)\b", str(exc)):
                raise ListingInactive("SKY 삭제/비공개 매물") from exc
            raise
        if detail.status_code in {404, 410}:
            raise ListingInactive("SKY 삭제/비공개 매물")
        if _identity(detail.url) != requested:
            parsed = urlsplit(detail.url)
            if re.fullmatch(rf"/build-{re.escape(requested[0])}/?", parsed.path):
                raise ListingInactive("SKY 모집 종료 후 건물 페이지 이동")
            raise ValueError("SKY 상세 최종 URL 확인 실패")
        candidate = self.parse(detail.html, detail.url)
        soup = BeautifulSoup(detail.html, "html.parser")
        candidate.photo_sources = _exclude_facility_copies(
            soup,
            candidate.photo_sources,
            lambda photo_url: fetcher.download(
                photo_url, self.code, referer=detail.url, cookies=detail.cookies,
            )[0],
        )
        return candidate

    def parse(self, html: str, url: str) -> PropertyCandidate:
        identity = _identity(url)
        if not identity:
            raise ValueError("SKY 상세 URL 형식 확인 실패")
        building_id, room_id = identity
        soup = BeautifulSoup(html, "html.parser")
        room_root, build_root = soup.select_one("#room-detail-list"), soup.select_one("#room-info #build-detail")
        room_pairs, build_pairs = _pairs(room_root), _pairs(build_root)
        if not room_pairs:
            raise ValueError("SKY 현재 호실 상세 영역 추출 실패")

        heading = soup.select_one("#room-page-buildName-number")
        heading_text = clean_text(heading.get_text(" ", strip=True) if heading else "")
        heading_match = re.match(r"(.+?)(?:\s*[｜|]\s*|\s+)([0-9０-９]+)\s*号室\s*$", heading_text)
        building_name = clean_text(heading_match.group(1)) if heading_match else ""
        room = normalize_room(heading_match.group(2)) if heading_match else ""
        rent_fee = _parts(room_pairs.get("賃料/管理費", ""))
        layout_area = _parts(room_pairs.get("間取り/専有面積", ""))
        # The live detail template places these two headline values above
        # #room-detail-list; fixtures also cover the older in-list form.
        if not rent_fee or not rent_fee[0]:
            price, fee = soup.select_one(".price"), soup.select_one(".maint_fee")
            price_text = clean_text(price.get_text(" ", strip=True)) if price else ""
            fee_text = clean_text(fee.get_text(" ", strip=True)) if fee else ""
            rent_fee = [price_text.replace(fee_text, ""), fee_text]
        if not layout_area or not layout_area[0]:
            layout, area_node = soup.select_one(".layout"), soup.select_one(".exc_area")
            layout_text = clean_text(layout.get_text(" ", strip=True)) if layout else ""
            area_text = clean_text(area_node.get_text(" ", strip=True)) if area_node else ""
            layout_match = re.search(r"[0-9]+(?:SLDK|LDK|DK|K|R)", layout_text, re.I)
            layout_area = [layout_match.group(0) if layout_match else "", area_text]
        terms, floors = _parts(room_pairs.get("敷金/礼金", "")), _parts(room_pairs.get("階数", ""))
        address = room_pairs.get("住所", "")
        equipment: list[str] = []
        for node in soup.select("#equipments .equipments-list-item-content"):
            for value in re.split(r"[、,]", clean_text(node.get_text(" ", strip=True))):
                value = clean_text(value)
                if value and value not in equipment:
                    equipment.append(value)

        rent = parse_yen(rent_fee[0] if rent_fee else "")
        layout = layout_area[0] if layout_area else ""
        area = parse_area(layout_area[1] if len(layout_area) > 1 else "")
        prefecture = extract_prefecture(address)
        missing = [label for label, value in (("건물명", building_name), ("호실", room), ("주소", address),
                                                ("월세", rent), ("방 구조", layout), ("전용면적", area),
                                                )
                   if value is None or value == "" or value == 0]
        if missing:
            raise ValueError("SKY 필수 매물 정보 누락: " + ", ".join(missing))

        return PropertyCandidate(
            source_site=canonical_host(url), source_property_id=room_id,
            management_company=self.management_company, building_name=building_name, room=room,
            prefecture=prefecture, address=address, source_url=_canonical_detail(url),
            rent=rent, management_fee=parse_yen(rent_fee[1] if len(rent_fee) > 1 else ""),
            deposit=terms[0] if terms else "", key_money=terms[1] if len(terms) > 1 else "",
            layout=layout, area=area, built_date=room_pairs.get("築年月", ""),
            floor=floors[0] if floors else "", total_floors=floors[1] if len(floors) > 1 else "",
            orientation=room_pairs.get("方角", ""), move_in_date=build_pairs.get("入居可能予定", ""),
            structure=build_pairs.get("建物構造", ""), transport=_transport(room_root), equipment=equipment,
            photo_sources=_photo_sources(soup, building_id, room_id), source_id_kind="site", scrape_warnings=[],
        )
