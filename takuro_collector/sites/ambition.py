import re
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from ..extractor import extract_generic
from ..models import PropertyCandidate
from ..utils import canonical_host
from .base import BaseAdapter, DiscoveryResult, ListingInactive


def _yen(value: str) -> int:
    match = re.search(r"([\d,]+)\s*円", value or "")
    return int(match.group(1).replace(",", "")) if match else 0


def _zero_or_text(value: str) -> str:
    text = (value or "").strip()
    return "0" if text in {"なし", "無し", "無", "0円", "-"} else text


def _transport_routes(soup: BeautifulSoup) -> list[dict]:
    """Parse every AMB route from its comma-separated 交通 table cell."""
    result: list[dict] = []
    seen: set[str] = set()
    for row in soup.find_all("tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) < 2 or cells[0].get_text(" ", strip=True) != "交通":
            continue
        for raw_cell in cells[1:]:
            for segment in re.split(r"\s*[、]\s*", raw_cell.get_text(" ", strip=True)):
                raw = segment.strip()
                if not raw or raw in seen:
                    continue
                seen.add(raw)
                match = re.fullmatch(
                    r"(?P<line>.+?)/(?P<station>.+?)\s+(?:徒歩|歩)(?P<walk>[0-9０-９]+)分",
                    raw,
                )
                if match:
                    result.append({
                        "line": match.group("line").strip(),
                        "station": match.group("station").strip(),
                        "walk_minutes": int(unicodedata.normalize("NFKC", match.group("walk"))),
                        "raw": raw,
                    })
                else:
                    # Preserve the source verbatim when AMB introduces a new
                    # expression; never invent missing line/station/time data.
                    result.append({"raw": raw})
    return result[:10]


_AMB_DETAIL_PATH = re.compile(r"^/rent/(?P<building_id>\d+)/(?P<room_id>\d+)/?$")
_CACHE_IMAGE_KEY = re.compile(r"(?:^|/)(?:\d+x\d+_)?(?P<key>[0-9a-f]{32})\.(?:jpe?g|png|webp)(?:$|[?#])", re.I)
_BUILDING_COMMON_LABELS = (
    "メールボックス", "宅配ボックス", "エレベーター", "エレベーターホール",
    "エントランス", "エントランスホール", "ロビー", "廊下", "共用", "集合ポスト",
    "オートロック", "電子ロック",
)


def _detail_identity(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() != "pm.am-bition.jp":
        return None
    match = _AMB_DETAIL_PATH.fullmatch(parsed.path)
    if not match:
        return None
    return match.group("building_id"), match.group("room_id")


def _parent_building_url(url: str) -> str | None:
    identity = _detail_identity(url)
    if not identity:
        return None
    building_id, _ = identity
    return f"https://pm.am-bition.jp/rent/{building_id}/"


def _cache_image_key(url: str) -> str:
    match = _CACHE_IMAGE_KEY.search(url or "")
    return match.group("key").lower() if match else ""


def _allowed_image_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme.lower() == "https" and (parsed.hostname or "").lower() == "pm.am-bition.jp"


def _room_photo_sources(soup: BeautifulSoup, page_url: str, room_id: str) -> list[dict]:
    side_keys: set[str] = set()
    for node in soup.select("#side_roomplan img, #side_roomplan a[href]"):
        value = node.get("src") or node.get("href") or ""
        key = _cache_image_key(urljoin(page_url, str(value)))
        if key:
            side_keys.add(key)

    result: list[dict] = []
    seen: set[str] = set()
    expected = re.compile(rf"^/img/upload/rent_room/\d+/{re.escape(room_id)}/[^/]+$")
    for anchor in soup.select("#room_photo #room_photo_album .photo_view a[href]"):
        url = urljoin(page_url, str(anchor.get("href") or ""))
        if not _allowed_image_url(url) or not expected.fullmatch(urlparse(url).path) or url in seen:
            continue
        image = anchor.find("img")
        label = ""
        cache_key = ""
        if image:
            label = str(image.get("alt") or image.get("title") or "").strip()
            cache_key = _cache_image_key(urljoin(page_url, str(image.get("src") or "")))
        seen.add(url)
        result.append({
            "url": url,
            "alt": label,
            "kind": "floorplan" if cache_key and cache_key in side_keys else "interior",
        })
    return result


def _building_photo_sources(soup: BeautifulSoup, page_url: str, building_id: str) -> list[dict]:
    result: list[dict] = []
    seen: set[str] = set()
    expected = re.compile(rf"^/img/upload/rent_mansion/\d+/{re.escape(building_id)}/[^/]+$")
    for anchor in soup.select("#room_photo #room_photo_album .photo_view a[href]"):
        url = urljoin(page_url, str(anchor.get("href") or ""))
        if not _allowed_image_url(url) or not expected.fullmatch(urlparse(url).path) or url in seen:
            continue
        image = anchor.find("img")
        label = ""
        if image:
            label = str(image.get("alt") or image.get("title") or "").strip()
        if "外観" in label:
            kind = "exterior"
        elif any(term in label for term in _BUILDING_COMMON_LABELS):
            kind = "common_area"
        else:
            kind = "other"
        seen.add(url)
        result.append({"url": url, "alt": label, "kind": kind})
    return result


class AmbitionAdapter(BaseAdapter):
    code = "AMB"
    label = "アンビション"
    domains = ("pm.am-bition.jp",)
    management_company = "アンビション"
    seed_urls = (
        "https://pm.am-bition.jp/rent_search/%E6%9D%B1%E4%BA%AC%E9%83%BD",
        "https://pm.am-bition.jp/rent_search/%E5%8D%83%E8%91%89%E7%9C%8C",
        "https://pm.am-bition.jp/rent_search/%E5%9F%BC%E7%8E%89%E7%9C%8C",
        "https://pm.am-bition.jp/rent_search/%E7%A5%9E%E5%A5%88%E5%B7%9D%E7%9C%8C",
    )
    detail_patterns = (r"/rent/\d+/(\d+)(?:$|[/?#])",)
    force_browser = True
    login_expected = True
    detail_refresh_ttl = timedelta(days=30)

    def discover(self, fetcher) -> DiscoveryResult:
        """Walk only AMB's live room tables and their explicit next links."""
        urls: list[str] = []
        inventory_items: dict[str, dict] = {}
        seen_urls: set[str] = set()
        seen_pages: set[str] = set()
        any_browser = False
        for seed in self.seed_urls:
            page_url = seed
            page_number = 0
            seed_path = unquote(urlparse(seed).path).rstrip("/")
            while page_url and page_url not in seen_pages:
                cancel_check = getattr(self, "cancel_check", None)
                if cancel_check:
                    cancel_check(page_number)
                seen_pages.add(page_url)
                page_number += 1
                result = fetcher.fetch(page_url, self.code, force_browser=self.force_browser,
                                       login_expected=self.login_expected)
                any_browser = any_browser or result.via_browser
                soup = BeautifulSoup(result.html, "html.parser")
                for anchor in soup.select(".item_room_table table.check_table a[href]"):
                    target = urljoin(result.url, str(anchor.get("href") or ""))
                    identity = _detail_identity(target)
                    if not identity:
                        continue
                    canonical = f"https://pm.am-bition.jp/rent/{identity[0]}/{identity[1]}"
                    if canonical not in seen_urls:
                        seen_urls.add(canonical)
                        urls.append(canonical)
                    row = anchor.find_parent("tr")
                    cells = row.find_all("td", recursive=False) if row else []
                    facts: dict[str, object] = {}
                    if len(cells) >= 5:
                        floor_match = re.search(r"(\d+)\s*階", cells[1].get_text(" ", strip=True))
                        layout_area = cells[2].get_text(" ", strip=True)
                        layout_match = re.search(r"([0-9]+(?:LDK|DK|K|R|SLDK))", layout_area, re.I)
                        area_match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(?:m²|㎡)", layout_area, re.I)
                        prices = [_yen(x) for x in cells[3].stripped_strings]
                        prices = [x for x in prices if x]
                        if floor_match:
                            facts["floor"] = f"{int(floor_match.group(1))}階"
                        if layout_match:
                            facts["layout"] = layout_match.group(1).upper()
                        if area_match:
                            facts["area"] = float(area_match.group(1))
                        if prices:
                            facts["rent"] = prices[0]
                        if len(prices) > 1:
                            facts["management_fee"] = prices[1]
                    inventory_items[canonical] = {
                        "source_property_id": identity[1], "source_url": canonical,
                        "change_facts": facts,
                    }
                next_anchor = soup.select_one("a.next[href]")
                next_url = urljoin(result.url, str(next_anchor.get("href") or "")) if next_anchor else ""
                parsed_next = urlparse(next_url)
                next_path = unquote(parsed_next.path).rstrip("/")
                if ((parsed_next.hostname or "").lower() != "pm.am-bition.jp"
                        or not re.fullmatch(rf"{re.escape(seed_path)}(?:/page:\d+)?", next_path)):
                    next_url = ""
                page_url = next_url
        return DiscoveryResult(
            urls, any_browser, listed_count=len(urls), inventory_complete=True,
            inventory_site="pm.am-bition.jp", inventory_items=inventory_items,
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
                old_layout = re.search(r"[0-9]+(?:LDK|DK|K|R|SLDK)", str(old or ""), re.I)
                if not old_layout or old_layout.group(0).upper() != str(new).upper():
                    return "changed"
            elif str(old or "").strip() != str(new or "").strip():
                return "changed"
        try:
            refreshed = datetime.fromisoformat(str(existing.get("last_seen_at") or ""))
            if refreshed.tzinfo is None:
                refreshed = refreshed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return "ttl"
        current = now or datetime.now(timezone.utc).astimezone()
        return "ttl" if current - refreshed >= self.detail_refresh_ttl else "unchanged"

    @staticmethod
    def _is_inactive_detail(status_code: int, html: str) -> bool:
        if status_code == 404:
            return True
        text = BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)
        return "404 ERROR PAGE NOT FOUND" in text and "ページが見つかりませんでした" in text

    def collect_url(self, fetcher, url: str) -> PropertyCandidate:
        detail = fetcher.fetch(
            url,
            self.code,
            force_browser=self.force_browser,
            login_expected=self.login_expected,
        )
        if self._is_inactive_detail(detail.status_code, detail.html):
            raise ListingInactive("AMB 삭제/비공개 매물")
        candidate = self.parse(detail.html, detail.url)
        identity = _detail_identity(detail.url)
        parent_url = _parent_building_url(detail.url)
        if not identity or not parent_url or not self.matches_url(parent_url):
            return candidate
        building_id, _ = identity
        try:
            building = fetcher.fetch(
                parent_url,
                self.code,
                force_browser=self.force_browser,
                login_expected=self.login_expected,
            )
            if self.matches_url(building.url) and urlparse(building.url).path == urlparse(parent_url).path:
                candidate.photo_sources.extend(
                    _building_photo_sources(BeautifulSoup(building.html, "html.parser"), building.url, building_id)
                )
        except Exception as exc:
            candidate.scrape_warnings.append(f"AMB 건물 사진 수집 실패: {exc}")
        return candidate

    def parse(self, html: str, url: str):
        soup = BeautifulSoup(html, "html.parser")
        source_id, source_id_kind = self.source_id(url)
        data = extract_generic(
            html,
            url,
            management_company=self.management_company,
            source_property_id=source_id,
            source_id_kind=source_id_kind,
        )

        # AMB puts the reliable building/room pair in the document title, while
        # its generic page heading contains badges and repeated building names.
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        identity = re.match(r"\s*(.+?)\s*(\d+)\s*[（(]", title)
        building_name = identity.group(1).strip() if identity else str(data.get("building_name") or "").strip()
        room = identity.group(2) if identity else str(data.get("room") or "").strip()
        rent = int(data.get("rent") or 0)
        management_fee = int(data.get("management_fee") or 0)
        deposit = str(data.get("deposit") or "")
        key_money = str(data.get("key_money") or "")

        # AMB advertises multiple price plans in a dedicated table.
        # Generic extraction cannot associate these column headers with values.
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue
            headers = [cell.get_text(" ", strip=True) for cell in rows[0].find_all(["th", "td"])]
            if "賃料" not in headers or not any("管理費" in h or "共益費" in h for h in headers):
                continue
            value_rows = [
                [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
                for row in rows[1:]
            ]
            value_rows = [values for values in value_rows if len(values) == len(headers) and _yen(values[headers.index("賃料")])]
            if not value_rows:
                continue
            # Register the cheapest advertised monthly-rent plan. min() is
            # stable, so equal-price plans retain the site's display order.
            rent_index = headers.index("賃料")
            values = min(value_rows, key=lambda item: _yen(item[rent_index]))
            row = dict(zip(headers, values))
            rent = _yen(row.get("賃料", ""))
            fee_key = next((h for h in headers if "管理費" in h or "共益費" in h), "")
            management_fee = _yen(row.get(fee_key, ""))
            terms_key = next((h for h in headers if "敷金" in h and "礼金" in h), "")
            terms = re.split(r"[/／]", row.get(terms_key, ""), maxsplit=1)
            if terms:
                deposit = _zero_or_text(terms[0])
            if len(terms) == 2:
                key_money = _zero_or_text(terms[1])
            break

        if not room.isdigit():
            raise ValueError("AMB 호실 추출 실패")
        if rent <= 0:
            raise ValueError("AMB 임대료 추출 실패")
        address = str(data.get("address") or "").strip()
        prefecture = str(data.get("prefecture") or "").strip()
        if not building_name or not address or not prefecture:
            raise ValueError("AMB 건물명/주소 추출 실패")
        transport = _transport_routes(soup) or list(data.get("transport") or [])
        identity = _detail_identity(url)
        room_photos = _room_photo_sources(soup, url, identity[1]) if identity else []
        return PropertyCandidate(
            source_site=canonical_host(url),
            source_property_id=source_id,
            management_company=self.management_company,
            building_name=building_name,
            room=room,
            prefecture=prefecture,
            address=address,
            source_url=url,
            rent=rent,
            management_fee=management_fee,
            deposit=deposit,
            key_money=key_money,
            area=data.get("area"),
            layout=str(data.get("layout") or ""),
            built_date=str(data.get("built_date") or ""),
            floor=str(data.get("floor") or ""),
            total_floors=str(data.get("total_floors") or ""),
            structure=str(data.get("structure") or ""),
            orientation=str(data.get("orientation") or ""),
            move_in_date=str(data.get("move_in_date") or ""),
            transport=transport,
            equipment=list(data.get("equipment") or []),
            photo_sources=room_photos,
            source_id_kind=source_id_kind,
            scrape_warnings=[],
        )
