from __future__ import annotations

import re
import unicodedata
import logging
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from ..extractor import clean_text
from ..models import PropertyCandidate
from ..utils import canonical_host, extract_prefecture, normalize_room, parse_area, parse_yen
from .base import BaseAdapter, DiscoveryResult

logger = logging.getLogger(__name__)


_DETAIL_PATH = re.compile(r"^/.+/room(?P<room_id>\d+)\.html$")
_CDN_PHOTO_PATH = re.compile(
    r"^/bkn/(?P<room_id>\d+)_(?P<image_id>\d+)_(?P<width>\d+)_(?P<height>\d+)_\d+\.(?:jpe?g|png|webp)$",
    re.I,
)
_COMMON_PHOTO_WORDS = (
    "エントランス", "エレベーターホール", "メールボックス", "宅配ボックス",
    "駐輪場", "駐輪スペース", "共用", "ロビー", "廊下", "集合ポスト",
)
_INTERIOR_PHOTO_WORDS = (
    "居間", "リビング", "洋室", "和室", "キッチン", "浴室", "バス", "トイレ",
    "洗面", "収納", "バルコニー", "玄関", "室内", "エアコン", "インターホン",
    "インターフォン", "洗濯機", "コンロ", "眺望", "下駄箱",
)


def _amm_detail_id(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() != "www.otoku-chintai.com":
        return ""
    match = _DETAIL_PATH.fullmatch(parsed.path)
    return match.group("room_id") if match else ""


def _summary_pairs(soup: BeautifulSoup) -> OrderedDict[str, str]:
    """Read only the current room's summary table, including four-cell rows."""
    pairs: OrderedDict[str, str] = OrderedDict()
    root = soup.select_one("#bkndetail .bknsummary")
    if not root:
        return pairs
    for row in root.select("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if len(cells) < 2:
            continue
        if len(cells) % 2 == 0:
            for index in range(0, len(cells), 2):
                label = clean_text(cells[index].get_text(" ", strip=True))
                value = clean_text(cells[index + 1].get_text(" ", strip=True))
                if label and value:
                    pairs[label] = value
        else:
            label = clean_text(cells[0].get_text(" ", strip=True))
            value = clean_text(" ".join(cell.get_text(" ", strip=True) for cell in cells[1:]))
            if label and value:
                pairs[label] = value
    return pairs


def _value(pairs: OrderedDict[str, str], label: str) -> str:
    if label in pairs:
        return pairs[label]
    for key, value in pairs.items():
        if label in key:
            return value
    return ""


def _slash_parts(value: str) -> list[str]:
    return [clean_text(part) for part in re.split(r"[/／]", value or "")]


def _transport(soup: BeautifulSoup) -> list[dict]:
    root = soup.select_one("#bkndetail .bknsummary")
    if not root:
        return []
    result: list[dict] = []
    for row in root.select("tr"):
        label = row.find("th", recursive=False)
        if not label or clean_text(label.get_text(" ", strip=True)) != "交通":
            continue
        for item in row.select("td li"):
            raw = clean_text(item.get_text(" ", strip=True))
            if not raw:
                continue
            links = item.select("a")
            walk = re.search(r"徒歩\s*([0-9０-９]+)\s*分", raw)
            if len(links) >= 2 and walk:
                result.append({
                    "line": clean_text(links[0].get_text(" ", strip=True)),
                    "station": clean_text(links[1].get_text(" ", strip=True)).removesuffix("駅"),
                    "walk_minutes": int(unicodedata.normalize("NFKC", walk.group(1))),
                    "bus_minutes": None,
                    "raw": raw,
                })
            else:
                result.append({"raw": raw})
        break
    return result[:10]


def _equipment(soup: BeautifulSoup) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in soup.select("#bkndetail .bknsummary #condition .bkn-option > li"):
        value = clean_text(item.get_text(" ", strip=True))
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result[:100]


def _photo_sources(html: str, room_id: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    result: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for anchor in soup.select("#thumbs.navigation a.thumb[href]"):
        raw_url = str(anchor.get("href") or "").strip()
        parsed = urlsplit(raw_url)
        if parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() != "cdn.img-asp.jp":
            continue
        match = _CDN_PHOTO_PATH.fullmatch(parsed.path)
        if not match or match.group("room_id") != room_id:
            continue
        if int(match.group("width")) <= 200 or int(match.group("height")) <= 200:
            continue
        image = anchor.find("img")
        label = clean_text(str(anchor.get("title") or "") or (str(image.get("alt") or "") if image else ""))
        parent = anchor.find_parent("li")
        caption_node = parent.select_one(".image-desc") if parent else None
        caption = clean_text(caption_node.get_text(" ", strip=True) if caption_node else "")
        evidence = clean_text(f"{label} {caption}")
        if "周辺" in evidence:
            continue
        identity = (room_id, match.group("image_id"))
        if identity in seen:
            continue
        seen.add(identity)
        if "間取り" in evidence:
            kind = "floorplan"
        elif "外観" in evidence:
            kind = "exterior"
        elif any(word in evidence for word in _COMMON_PHOTO_WORDS):
            kind = "common_area"
        elif any(word in evidence for word in _INTERIOR_PHOTO_WORDS):
            kind = "interior"
        else:
            kind = "other"
        result.append({"url": raw_url, "alt": evidence, "kind": kind})
    return result


class AMMAdapter(BaseAdapter):
    code = "AMM"
    label = "アムス / otoku-chintai"
    domains = ("otoku-chintai.com",)
    management_company = "アムス"
    detail_patterns = (r"/room(\d+)\.html(?:$|[?#])",)
    force_browser = False
    login_expected = False
    detail_refresh_ttl = timedelta(days=30)

    TARGET_ADDRESS_IDS = (
        "13112", "13116", "13119", "13114", "13115", "13206", "13229", "13120",
        "13201", "13111", "13202", "13110", "13102", "13104", "13118", "13107",
        "13123", "13106", "13108", "13109", "13117", "13209", "13224", "13121",
        "13122", "13212", "13211", "13222", "13103", "13113", "13203", "13204",
        "13208", "13210", "13101", "13105", "13221", "14104", "14131", "14101",
        "14133", "14105", "14108", "14109", "14205", "14153", "14211", "14218",
        "14102", "14103", "14111", "14134", "14152", "11203", "11108", "11102",
        "11107", "11201", "11221", "11208", "11222", "11224", "11239", "12204",
        "12103", "12101", "12102", "12217",
    )
    SEARCH_URL = "https://www.otoku-chintai.com/search/index/"
    seed_urls = (
        SEARCH_URL + "?" + urlencode(
            [("address[]", value) for value in TARGET_ADDRESS_IDS]
            + [("class[]", "c1"), ("selectFlg", "1")]
        ),
    )

    @staticmethod
    def _page_links(html: str, page_url: str) -> tuple[list[str], list[str], int]:
        soup = BeautifulSoup(html, "html.parser")
        details: list[str] = []
        for anchor in soup.select(".list_area .list_detail2 td.detail.btn > a[href]"):
            url = urljoin(page_url, str(anchor.get("href") or ""))
            if _amm_detail_id(url):
                parsed = urlsplit(url)
                details.append(urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")))

        pages: list[str] = []
        for anchor in soup.select(".pager a[href*='pg=']"):
            url = urljoin(page_url, str(anchor.get("href") or ""))
            parsed = urlsplit(url)
            if (
                parsed.scheme.lower() == "https"
                and (parsed.hostname or "").lower() == "www.otoku-chintai.com"
                and parsed.path == "/search/index/"
                and any(key == "pg" and value.isdigit() for key, value in parse_qsl(parsed.query))
            ):
                pages.append(urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, "")))
        text = clean_text(soup.get_text(" ", strip=True))
        count = re.search(r"空き数\s*([0-9０-９,，]+)\s*件", unicodedata.normalize("NFKC", text))
        listed = int(re.sub(r"[^0-9]", "", count.group(1))) if count else 0
        return details, pages, listed

    @staticmethod
    def _inventory_items(html: str, page_url: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        items: list[dict] = []
        for row in soup.select(".list_area .list_detail2 tr[name]"):
            anchor = row.select_one("td.detail.btn > a[href]")
            url = urljoin(page_url, str(anchor.get("href") or "")) if anchor else ""
            room_id = _amm_detail_id(url)
            if not room_id:
                continue
            parsed = urlsplit(url)
            canonical = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
            cells = row.find_all("td", recursive=False)
            facts: dict[str, object] = {}
            if len(cells) >= 7:
                rent = parse_yen(cells[1].get_text(" ", strip=True))
                fee = parse_yen(cells[2].get_text(" ", strip=True))
                layout_match = re.search(
                    r"[0-9]+(?:SLDK|LDK|DK|K|R)", clean_text(cells[5].get_text(" ", strip=True)), re.I
                )
                area = parse_area(cells[6].get_text(" ", strip=True))
                if rent:
                    facts["rent"] = rent
                # Zero is a valid explicit management fee on AMM listings.
                if clean_text(cells[2].get_text(" ", strip=True)):
                    facts["management_fee"] = fee
                if layout_match:
                    facts["layout"] = layout_match.group(0).upper()
                if area is not None:
                    facts["area"] = area
            items.append({"source_property_id": room_id, "source_url": canonical, "change_facts": facts})
        return items

    def discover(self, fetcher) -> DiscoveryResult:
        queue = list(self.seed_urls)
        seen_pages: set[int] = set()
        seen_details: set[str] = set()
        details: list[str] = []
        inventory_items: dict[str, dict] = {}
        listed_count = 0
        any_browser = False
        while queue:
            page_url = queue.pop(0)
            page_number = int(dict(parse_qsl(urlsplit(page_url).query)).get("pg", "1"))
            if page_number in seen_pages:
                continue
            seen_pages.add(page_number)
            page = fetcher.fetch(
                page_url, self.code, force_browser=False, login_expected=False, browser_fallback=False
            )
            any_browser = any_browser or page.via_browser
            found, pagination, reported = self._page_links(page.html, page.url)
            listed_count = max(listed_count, reported)
            for url in found:
                if url not in seen_details:
                    seen_details.add(url)
                    details.append(url)
            for item in self._inventory_items(page.html, page.url):
                inventory_items.setdefault(item["source_url"], item)
            for url in pagination:
                next_page = int(dict(parse_qsl(urlsplit(url).query)).get("pg", "1"))
                canonical_page = self.seed_urls[0] + (f"&pg={next_page}" if next_page > 1 else "")
                if next_page not in seen_pages and canonical_page not in queue:
                    queue.append(canonical_page)
        messages = [f"AMM 페이지 {len(seen_pages)} / 고유 상세URL {len(details)}"]
        if listed_count and len(details) != listed_count:
            messages.append(f"AMM 표시 공실 {listed_count}건과 상세URL {len(details)}건 차이 확인 필요")
        complete = bool(listed_count and len(details) == listed_count and len(inventory_items) == len(details))
        return DiscoveryResult(
            details, any_browser, listed_count=listed_count, messages=messages,
            inventory_complete=complete, inventory_site="otoku-chintai.com",
            inventory_items=inventory_items,
        )

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
        try:
            detail = fetcher.fetch(
                url, self.code, force_browser=False, login_expected=False, browser_fallback=False
            )
        except Exception as exc:
            logger.warning("AMM detail failed url=%s phase=detail error=%s", url, exc)
            raise
        candidate = self.parse(detail.html, detail.url)
        room_id = _amm_detail_id(detail.url)
        if not room_id:
            return candidate
        gallery_url = f"https://www.otoku-chintai.com/bkn/ajax/library/?{urlencode({'roomId': room_id})}"
        try:
            gallery = fetcher.fetch(
                gallery_url, self.code, force_browser=False, login_expected=False, browser_fallback=False
            )
            parsed = urlsplit(gallery.url)
            if parsed.scheme.lower() == "https" and (parsed.hostname or "").lower() == "www.otoku-chintai.com":
                candidate.photo_sources = _photo_sources(gallery.html, room_id)
            else:
                candidate.scrape_warnings.append("AMM 사진 gallery 최종 host 확인 실패")
        except Exception as exc:
            logger.warning("AMM gallery failed url=%s phase=gallery error=%s", gallery_url, exc)
            candidate.scrape_warnings.append(f"AMM 사진 gallery 수집 실패: {exc}")
        return candidate

    def parse(self, html: str, url: str) -> PropertyCandidate:
        room_id = _amm_detail_id(url)
        if not room_id:
            raise ValueError("AMM 상세 URL 형식 확인 실패")
        soup = BeautifulSoup(html, "html.parser")
        pairs = _summary_pairs(soup)
        if not pairs:
            raise ValueError("AMM 현재 호실 상세 영역 추출 실패")

        room_floor = _slash_parts(_value(pairs, "部屋/所在階/階建"))
        raw_room = room_floor[0] if room_floor else ""
        room = normalize_room(raw_room)
        heading = soup.select_one(".h1_section h1")
        heading_text = clean_text(heading.get_text(" ", strip=True) if heading else "")
        building_name = re.sub(rf"\s*{re.escape(raw_room)}\s*$", "", heading_text).strip() if raw_room else ""

        dom_id = clean_text(_value(pairs, "物件番号"))
        if dom_id and dom_id != room_id:
            raise ValueError(f"AMM source property ID 불일치: URL {room_id} / DOM {dom_id}")

        address = clean_text(_value(pairs, "所在地"))
        prefecture = extract_prefecture(address)
        rent_raw = _value(pairs, "賃料")
        fee_raw = _value(pairs, "管理費・共益費")
        terms = _slash_parts(_value(pairs, "敷金/礼金/保証金"))
        layout_detail = _value(pairs, "間取り/詳細")
        layout = clean_text(layout_detail.split(" ", 1)[0])
        area_parts = _slash_parts(_value(pairs, "面積/バルコニー面積"))
        area = parse_area(area_parts[0] if area_parts else "")
        built_date = re.split(r"[（(]", _value(pairs, "築年月(築年数)"), maxsplit=1)[0].strip()
        type_structure = _slash_parts(_value(pairs, "種別/構造"))
        status_move_in = _slash_parts(_value(pairs, "現況/入居可能日"))
        structure = type_structure[1] if len(type_structure) >= 2 else ""
        floor = room_floor[1] if len(room_floor) >= 2 else ""
        total_floors = room_floor[2] if len(room_floor) >= 3 else ""
        move_in = status_move_in[1] if len(status_move_in) >= 2 else ""
        rent = parse_yen(rent_raw)
        management_fee = parse_yen(fee_raw)

        missing = []
        for label, value in (
            ("건물명", building_name), ("호실", room), ("주소", address), ("월세", rent),
            ("관리비", fee_raw), ("방 구조", layout), ("전용면적", area), ("건물 구조", structure),
            ("1도3현 주소", prefecture),
        ):
            if value is None or value == "" or value == 0:
                missing.append(label)
        if missing:
            raise ValueError("AMM 필수 매물 정보 누락: " + ", ".join(missing))

        parsed_url = urlsplit(url)
        return PropertyCandidate(
            source_site=canonical_host(url), source_property_id=room_id,
            management_company=self.management_company, building_name=building_name, room=room,
            prefecture=prefecture, address=address,
            source_url=urlunsplit((parsed_url.scheme, parsed_url.netloc, parsed_url.path, "", "")),
            rent=rent, management_fee=management_fee,
            deposit=terms[0] if terms else "", key_money=terms[1] if len(terms) >= 2 else "",
            area=area, layout=layout, built_date=built_date, floor=floor, total_floors=total_floors,
            structure=structure, orientation=_value(pairs, "向き"), move_in_date=move_in,
            transport=_transport(soup), equipment=_equipment(soup), photo_sources=[],
            source_id_kind="site", scrape_warnings=[],
        )
