from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .base import BaseAdapter, DiscoveryResult
from ..extractor import clean_text, extract_generic, pair_value, _pairs, _images
from ..models import PropertyCandidate
from ..verified_photos import VERIFIED_PHOTOS
from ..utils import ALLOWED_PREFECTURES, canonical_host, extract_prefecture, normalize_room, parse_yen


class KinoshitaAdapter(BaseAdapter):
    code = "KIN"
    label = "木下の賃貸"
    domains = ("kinoshita-chintai.com",)
    management_company = "株式会社木下の賃貸"
    detail_refresh_ttl = timedelta(days=30)
    seed_urls = ("https://kinoshita-chintai.com/",)
    detail_patterns = (r"/details/([^/?#]+?)details\.html(?:$|[?#])",)

    @staticmethod
    def _nfkc(value: str) -> str:
        return unicodedata.normalize("NFKC", clean_text(value))

    SEARCH_URL = "https://kinoshita-chintai.com/search_result.html"
    AREA_URL = "https://kinoshita-chintai.com/area/"
    # Kinoshita's area_key values, confirmed by the current area form.
    TARGET_AREAS = (
        ("東京", "東京都", "13__", "1"),
        ("神奈川", "神奈川県", "14__", "2"),
        ("埼玉", "埼玉県", "11__", "3"),
        ("千葉", "千葉県", "12__", "4"),
    )

    @classmethod
    def _search_locality_ids(cls, area_html: str, prefix: str) -> list[str]:
        soup = BeautifulSoup(area_html, "html.parser")
        values: list[str] = []
        seen: set[str] = set()
        for node in soup.find_all("input", attrs={"name": "state_locality_id[]"}):
            value = clean_text(str(node.get("value") or ""))
            if value.startswith(prefix) and value not in seen:
                seen.add(value)
                values.append(value)
        return values

    @classmethod
    def _result_card_stats(cls, html: str, base_url: str = SEARCH_URL) -> tuple[int, int, int, set[str]]:
        """Return (reported_total, card_count, cards_with_detail, unique_card_keys).

        KIN's ``N件中`` counter is a building-card count, not a room/detail-link
        count.  One card may legitimately expose multiple room-level detail URLs.
        Card identity therefore uses the card's room-detail URLs when available,
        falling back to building+address text only for duplicate-page detection.
        """
        soup = BeautifulSoup(html, "html.parser")
        text = clean_text(soup.get_text(" ", strip=True))
        total = 0
        count_node = soup.select_one(".change_list_count")
        count_text = clean_text(count_node.get_text(" ", strip=True)) if count_node else text[:5000]
        m = re.search(r"(?:\d+\s*[～~-]\s*\d+\s*/\s*)?([0-9０-９,，]+)\s*件中", cls._nfkc(count_text))
        if m:
            total = int(re.sub(r"[^0-9]", "", cls._nfkc(m.group(1))) or 0)

        cards = soup.select("#search_result_housing_list > li.main_box, li.main_box")
        keys: set[str] = set()
        with_detail = 0
        for card in cards:
            urls = []
            for a in card.find_all("a", href=re.compile(r"/details/[^?#]+details\.html", re.I)):
                u = urljoin(base_url, str(a.get("href") or ""))
                if u and u not in urls:
                    urls.append(u)
            if urls:
                with_detail += 1
                keys.add("urls:" + "|".join(sorted(urls)))
                continue
            h3 = card.find("h3")
            building = clean_text(h3.get_text(" ", strip=True) if h3 else "")
            address = ""
            for dt in card.find_all("dt"):
                if clean_text(dt.get_text(" ", strip=True)) == "住所":
                    dd = dt.find_next_sibling("dd")
                    address = clean_text(dd.get_text(" ", strip=True) if dd else "")
                    break
            keys.add("text:" + cls._nfkc(building + "|" + address))
        return total, len(cards), with_detail, keys

    @classmethod
    def _parse_result_page(cls, html: str, base_url: str = SEARCH_URL) -> tuple[int, list[dict]]:
        """Parse one Kinoshita search result page at room/detail-link granularity.

        A building card can contain more than one available room, so the canonical
        unit is the table row containing a ``/details/...details.html`` link.
        """
        soup = BeautifulSoup(html, "html.parser")
        text = clean_text(soup.get_text(" ", strip=True))
        total = 0
        count_node = soup.select_one(".change_list_count")
        count_text = clean_text(count_node.get_text(" ", strip=True)) if count_node else text[:5000]
        m = re.search(r"(?:\d+\s*[～~-]\s*\d+\s*/\s*)?([0-9０-９,，]+)\s*件中", cls._nfkc(count_text))
        if m:
            total = int(re.sub(r"[^0-9]", "", cls._nfkc(m.group(1))) or 0)

        out: list[dict] = []
        seen: set[str] = set()
        for card in soup.select("#search_result_housing_list > li.main_box, li.main_box"):
            h3 = card.find("h3")
            building = clean_text(h3.get_text(" ", strip=True) if h3 else "").lstrip("■□・ ")
            address = ""
            for dt in card.find_all("dt"):
                if clean_text(dt.get_text(" ", strip=True)) == "住所":
                    dd = dt.find_next_sibling("dd")
                    if dd:
                        address = clean_text(dd.get_text(" ", strip=True))
                    break
            pref = extract_prefecture(address)
            table = card.find("table")
            if not table:
                continue
            for tr in table.find_all("tr"):
                link = tr.find("a", href=re.compile(r"/details/[^?#]+details\.html", re.I))
                if not link:
                    continue
                detail_url = urljoin(base_url, str(link.get("href") or ""))
                if detail_url in seen:
                    continue
                cells = [clean_text(td.get_text(" ", strip=True)) for td in tr.find_all("td", recursive=False)]
                # Current KIN table columns: checkbox, floorplan, room, rent, fees, layout, detail.
                room = normalize_room(cells[2] if len(cells) >= 3 else "")
                rent = parse_yen(cells[3]) if len(cells) >= 4 else 0
                mgmt = 0
                mgmt_known = False
                if len(cells) >= 4:
                    rm = re.search(r"[（(]\s*([0-9０-９,，]+)\s*円?\s*[）)]", cls._nfkc(cells[3]))
                    if rm:
                        mgmt = parse_yen(rm.group(1))
                        mgmt_known = True
                layout = ""
                area = None
                if len(cells) >= 6:
                    layout_area = cls._nfkc(cells[5])
                    layout_match = re.search(r"[0-9]+(?:SLDK|LDK|DK|K|R)", layout_area, re.I)
                    area_match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(?:m2|m²|㎡)", layout_area, re.I)
                    if layout_match:
                        layout = layout_match.group(0).upper()
                    if area_match:
                        area = float(area_match.group(1))
                source_id, _ = cls().source_id(detail_url)
                if not building or not room:
                    continue
                seen.add(detail_url)
                change_facts = {}
                if rent:
                    change_facts["rent"] = rent
                if mgmt_known:
                    change_facts["management_fee"] = mgmt
                if layout:
                    change_facts["layout"] = layout
                if area is not None:
                    change_facts["area"] = area
                out.append({
                    "url": detail_url,
                    "source_site": canonical_host(detail_url),
                    "source_property_id": source_id,
                    "management_company": cls.management_company,
                    "building_name": building,
                    "room": room,
                    "address": address,
                    "prefecture": pref,
                    "rent": rent,
                    "management_fee": mgmt,
                    "layout": layout,
                    "area": area,
                    "change_facts": change_facts,
                })
        return total, out

    def discover(self, fetcher) -> DiscoveryResult:
        """Enumerate all currently advertised KIN rooms in the 1都3県 search UI.

        The site uses POST-backed search state and POST-backed pagination.  Prefer the
        built-in ALL page size, then fall back to explicit page_num traversal when the
        server ignores ALL or returns a partial page.
        """
        area = fetcher.fetch(self.AREA_URL, self.code)
        all_urls: list[str] = []
        hints: dict[str, dict] = {}
        seen: set[str] = set()
        listed_count = 0
        messages: list[str] = []
        complete_areas = 0

        for label, pref, prefix, area_key in self.TARGET_AREAS:
            locality_ids = self._search_locality_ids(area.html, prefix)
            if not locality_ids:
                messages.append(f"KIN {label}: 지역 코드 추출 실패")
                continue
            base_pairs = [("area_key", area_key), ("search_type", "area"), ("page_num", "1"), ("page_cnt", "1000000")]
            base_pairs += [("state_locality_id[]", v) for v in locality_ids]
            first = fetcher.post_form(self.SEARCH_URL, self.code, base_pairs, referer=self.AREA_URL, success_marker="search_result_housing_list")
            total, rows = self._parse_result_page(first.html, first.url)
            _, first_card_count, first_cards_with_detail, first_card_keys = self._result_card_stats(first.html, first.url)
            listed_count += total or first_card_count

            pages_rows = rows
            area_card_keys = set(first_card_keys)
            all_cards_have_detail = first_card_count > 0 and first_cards_with_detail == first_card_count
            # If ALL was not honored, replay the same POST state with page_num.
            # IMPORTANT: KIN's reported total is a BUILDING CARD count, while rows are
            # ROOM-LEVEL detail links.  Never compare total directly with len(rows).
            if total and first_card_count < total:
                per_page = max(1, first_card_count)
                if per_page > 100 or per_page == total:
                    per_page = 10
                page_count = (total + per_page - 1) // per_page
                pages_rows = []
                area_card_keys = set()
                all_cards_have_detail = True
                for page_num in range(1, page_count + 1):
                    pairs = [("area_key", area_key), ("search_type", "area"), ("page_num", str(page_num)), ("page_cnt", str(per_page))]
                    pairs += [("state_locality_id[]", v) for v in locality_ids]
                    page = fetcher.post_form(self.SEARCH_URL, self.code, pairs, referer=self.AREA_URL, success_marker="search_result_housing_list")
                    _, pr = self._parse_result_page(page.html, page.url)
                    _, card_count, cards_with_detail, card_keys = self._result_card_stats(page.html, page.url)
                    pages_rows.extend(pr)
                    area_card_keys.update(card_keys)
                    if card_count == 0 or cards_with_detail != card_count:
                        all_cards_have_detail = False
            area_unique = 0
            for row in pages_rows:
                url = row["url"]
                if url in seen:
                    continue
                seen.add(url)
                all_urls.append(url)
                hints[url] = row
                area_unique += 1
            area_expected = total or len(area_card_keys)
            cards_complete = total > 0 and len(area_card_keys) == total and all_cards_have_detail
            if cards_complete and area_unique > 0:
                complete_areas += 1
            else:
                messages.append(
                    f"KIN {label}: 재고 불완전 - 검색카드 {area_expected} / 확인카드 {len(area_card_keys)} / 고유 상세URL {area_unique}"
                )
            messages.append(f"KIN {label}: 검색카드 {area_expected} / 고유 상세URL {area_unique}")

        inventory_complete = (
            complete_areas == len(self.TARGET_AREAS)
            and listed_count > 0
            and len(all_urls) > 0
            and len(hints) == len(all_urls)
        )
        if inventory_complete:
            messages.append(f"KIN 전체 재고 스냅샷 완료: {len(all_urls)}건")
        else:
            messages.append("KIN 전체 재고 스냅샷 보류: 지역별 검색카드 순회 또는 room-level 상세 URL 확인이 불완전합니다.")
        return DiscoveryResult(
            all_urls,
            area.via_browser,
            hints=hints,
            listed_count=listed_count,
            messages=messages,
            inventory_complete=inventory_complete,
            inventory_site=canonical_host(self.SEARCH_URL),
            inventory_items=hints,
        )

    def existing_inventory_action(self, existing: dict, item: dict, *, now: datetime | None = None) -> str:
        """Choose detail refresh using only facts explicitly present in KIN list rows."""
        facts = dict(item.get("change_facts") or {})
        for key in ("rent", "management_fee", "layout", "area"):
            if key not in facts:
                continue
            old, new = existing.get(key), facts[key]
            if key == "area":
                if old is None or abs(float(old) - float(new)) > 0.001:
                    return "changed"
            elif key == "layout":
                old_layout = re.search(r"[0-9]+(?:SLDK|LDK|DK|K|R)", str(old or ""), re.I)
                if not old_layout or old_layout.group(0).upper() != str(new).upper():
                    return "changed"
            elif int(old or 0) != int(new):
                return "changed"
        try:
            refreshed = datetime.fromisoformat(str(existing.get("last_seen_at") or ""))
            if refreshed.tzinfo is None:
                refreshed = refreshed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return "ttl"
        current = now or datetime.now(timezone.utc).astimezone()
        return "ttl" if current - refreshed >= self.detail_refresh_ttl else "unchanged"

    @classmethod
    def _combined_rent_fee(cls, pairs, body_text: str = "") -> tuple[int, int]:
        """Parse Kinoshita rent/fee variants, including split text rendered by widgets."""
        raw = pair_value(
            pairs,
            (
                "家賃（管理費）", "家賃(管理費)", "賃料（管理費）", "賃料(管理費)",
                "家賃・管理費", "賃料・管理費", "家賃", "賃料",
            ),
            contains=True,
        )
        candidates = [raw, body_text]
        patterns = (
            # 88,000円（2,000円） / 8.8万円(2,000円)
            r"(?:家賃|賃料)?\s*([0-9０-９.,，]+)\s*(万円|円)?\s*[（(]\s*(?:管理費|共益費)?\s*([0-9０-９.,，]+|[-ー―—–])\s*円?\s*[）)]",
            # 88,000円 管理費 2,000円 / 家賃 8.8万円 共益費 2,000円
            r"(?:家賃|賃料)\s*[:：]?\s*([0-9０-９.,，]+)\s*(万円|円)?[^\n]{0,80}?(?:管理費|共益費)\s*[:：]?\s*([0-9０-９.,，]+|[-ー―—–])\s*円?",
        )
        for candidate in candidates:
            text = cls._nfkc(candidate)
            if not text:
                continue
            for pat in patterns:
                m = re.search(pat, text, re.I)
                if not m:
                    continue
                rent_num, unit, fee_num = m.groups()
                rent = cls._parse_money(rent_num, unit)
                fee = 0 if re.fullmatch(r"[-ー―—–]+", fee_num or "") else parse_yen(fee_num)
                return rent, fee

        # Last resort: two numeric values inside the dedicated raw field.
        text = cls._nfkc(raw)
        nums = re.findall(r"[0-9][0-9,.]*", text)
        if len(nums) >= 2:
            return parse_yen(nums[0]), parse_yen(nums[1])
        return (parse_yen(nums[0]), 0) if nums else (0, 0)

    @staticmethod
    def _parse_money(number: str, unit: str | None) -> int:
        n = unicodedata.normalize("NFKC", number or "").replace(",", "")
        try:
            value = float(n)
        except ValueError:
            return 0
        return int(round(value * 10000)) if unit == "万円" else int(round(value))

    @classmethod
    def _room(cls, soup: BeautifulSoup, pairs, building: str, fallback: str) -> str:
        # Dedicated fields first.
        raw = pair_value(pairs, ("部屋番号", "号室", "室番号", "お部屋番号", "ROOM"), contains=False)
        raw = cls._nfkc(raw)
        m = re.search(r"([0-9]{1,5}[A-Za-z]?)\s*(?:号室|室)?$", raw)
        if m:
            return normalize_room(m.group(1))

        # Kinoshita often places "建物名 201号室" in h1/title.
        strings: list[str] = []
        if soup.find("h1"):
            strings.append(soup.find("h1").get_text(" ", strip=True))
        if soup.title:
            strings.append(soup.title.get_text(" ", strip=True))
        strings.append(building)
        for text in strings:
            text = cls._nfkc(text)
            m = re.search(r"(?:^|\s|[-_/])([0-9]{1,5}[A-Za-z]?)\s*号室(?:\s|$)", text)
            if m:
                return normalize_room(m.group(1))
        fallback = cls._nfkc(fallback)
        if re.fullmatch(r"[0-9]{1,5}[A-Za-z]?", fallback):
            return normalize_room(fallback)
        return ""

    @classmethod
    def _full_address(cls, soup: BeautifulSoup, pairs, fallback: str) -> str:
        """Return a physical street address and reject KIN SEO/title copy.

        Kinoshita pages sometimes contain title/breadcrumb strings beginning with a
        prefecture (for example "神奈川県、逗子市の賃貸物件情報...").  Those
        strings are not property addresses, so candidates are scored by physical
        address signals and title/marketing text is rejected.
        """
        explicit = pair_value(
            pairs,
            ("所在地", "物件所在地", "住所", "所在地住所", "住所所在地"),
            contains=False,
        )
        candidates: list[tuple[str, int]] = [(explicit, 100), (fallback, 10)]

        # Structured/meta address sources are safer than arbitrary page text.
        for tag in soup.find_all(attrs={"itemprop": re.compile(r"address", re.I)}):
            candidates.append((clean_text(tag.get_text(" ", strip=True)), 90))
        for meta in soup.find_all("meta"):
            key = str(meta.get("property") or meta.get("name") or "").lower()
            if "address" in key:
                candidates.append((str(meta.get("content") or ""), 90))

        for node in soup.find_all(string=re.compile(r"東京都|千葉県|埼玉県|神奈川県")):
            text = clean_text(node)
            if any(pref in text for pref in ALLOWED_PREFECTURES):
                candidates.append((text, 30))
            parent = getattr(node, "parent", None)
            if parent is not None:
                whole = clean_text(parent.get_text(" ", strip=True))
                if any(pref in whole for pref in ALLOWED_PREFECTURES) and len(whole) <= 220:
                    candidates.append((whole, 35))

        bad_tokens = (
            "賃貸物件情報", "仲介手数料", "木下の賃貸", "物件情報", "お部屋探し",
            "検索", "一覧", "詳細", "HOME", "ホーム", "不動産", "募集中",
        )
        scored: list[tuple[int, str]] = []
        for raw, base_score in candidates:
            value = cls._nfkc(raw)
            if not value:
                continue
            value = re.sub(r"^〒?\s*\d{3}[-－]?\d{4}\s*", "", value)
            m = re.search(r"(東京都|千葉県|埼玉県|神奈川県)(.*)$", value)
            if not m:
                continue
            value = clean_text(m.group(1) + m.group(2))
            value = re.split(
                r"\s+(?:交通|アクセス|家賃|賃料|管理費|共益費|敷金|礼金|間取り|専有面積|築年月|構造|物件ID|方位|向き)(?:\s|[:：])",
                value,
                maxsplit=1,
            )[0]
            value = value.strip(" |｜/・")

            if "..." in value or "…" in value:
                continue
            if any(token in value for token in bad_tokens):
                continue
            # SEO title pattern: 神奈川県、逗子市の... / 神奈川県,逗子市の...
            if re.search(r"(?:都|道|府|県)[、,，]", value) or "市の賃貸" in value or "区の賃貸" in value:
                continue
            if extract_prefecture(value) not in ALLOWED_PREFECTURES or len(value) < 7:
                continue

            score = base_score
            if re.search(r"(?:市|区|郡|町|村)", value):
                score += 20
            if re.search(r"(?:丁目|番地?|番|号|[-－]?[0-9０-９]{1,4})", value):
                score += 25
            # A physical address is normally compact. Very long prose is suspicious.
            if len(value) <= 70:
                score += 10
            elif len(value) > 110:
                score -= 30
            scored.append((score, value))

        if scored:
            scored.sort(key=lambda item: (item[0], len(item[1])), reverse=True)
            return scored[0][1]

        # Do not return a known-bad SEO fallback as an address.
        fallback_norm = cls._nfkc(fallback)
        if any(token in fallback_norm for token in bad_tokens) or re.search(r"(?:都|道|府|県)[、,，]", fallback_norm):
            return ""
        return fallback_norm


    @classmethod
    def _table_fields(cls, soup: BeautifulSoup) -> dict[str, list[str]]:
        """Preserve visible KIN table data without forcing it into FAST fields.

        KIN commonly uses 4-cell rows (label/value/label/value). Generic extraction
        historically collapsed cells 2-4 into the first value, which hid fields such
        as 礼金. Keep each visible pair independently for evidence/reprocessing.
        """
        out: dict[str, list[str]] = {}

        def add(key: str, value: str) -> None:
            key = clean_text(key)
            value = clean_text(value)
            if not key or not value or key == value:
                return
            out.setdefault(key, [])
            if value not in out[key]:
                out[key].append(value)

        for tr in soup.find_all("tr"):
            cells = tr.find_all(["th", "td"], recursive=False)
            if len(cells) >= 4 and len(cells) % 2 == 0:
                for i in range(0, len(cells), 2):
                    add(cells[i].get_text(" ", strip=True), cells[i + 1].get_text(" ", strip=True))
            elif len(cells) >= 2:
                add(cells[0].get_text(" ", strip=True), " ".join(c.get_text(" ", strip=True) for c in cells[1:]))
        for dt in soup.find_all("dt"):
            dd = dt.find_next_sibling("dd")
            if dd:
                add(dt.get_text(" ", strip=True), dd.get_text(" ", strip=True))
        return out

    @classmethod
    def _equipment_features(cls, soup: BeautifulSoup, table_fields: dict[str, list[str]], fallback: list[str]) -> list[str]:
        """Return KIN equipment from property-scoped evidence only.

        Do not scan the whole page: recommendation/footer text can mention equipment
        belonging to another property. Explicit negative values override token hits.
        Canonical Japanese labels are used so the existing WordPress feature mapper
        can translate them without creating duplicate taxonomy terms.
        """
        positive = {"○", "有", "あり", "対応"}
        negative = {"×", "無", "なし"}
        rules = (
            (("バス・トイレ別", "バストイレ別"), "バス・トイレ別"),
            (("室内洗濯機置き場", "室内洗濯機置場", "洗濯機置き場室内洗濯機置き場", "洗濯機置場室内洗濯機置場"), "室内洗濯機置場"),
            (("温水洗浄便座", "ウォシュレット"), "温水洗浄便座"),
        )

        evidence: list[str] = []
        equipment_keys = ("設備", "設備・条件", "設備条件", "こだわり", "特徴", "物件設備", "設備情報")
        for key, values in table_fields.items():
            nk = cls._nfkc(key)
            if any(token in nk for token in equipment_keys):
                evidence.extend(cls._nfkc(v) for v in values if v)

        # Current KIN pages can expose individual feature rows rather than a single
        # equipment field. Restrict this to table/dl rows that contain a target label.
        target_tokens = tuple(t for aliases, _ in rules for t in aliases)
        for node in soup.find_all(["tr", "dl", "li"]):
            text = cls._nfkc(node.get_text(" ", strip=True))
            if text and any(token in text for token in target_tokens):
                evidence.append(text)

        def state_for(text: str, aliases: tuple[str, ...]) -> bool | None:
            text = cls._nfkc(text)
            for alias in aliases:
                pos = text.find(alias)
                if pos < 0:
                    continue
                around = text[max(0, pos - 24): pos + len(alias) + 24]
                # Explicit negative close to the feature always wins.
                if any(re.search(rf"(?:{re.escape(alias)}\s*[:：]?\s*{re.escape(mark)}|{re.escape(mark)}\s*{re.escape(alias)})", around) for mark in negative):
                    return False
                if any(re.search(rf"(?:{re.escape(alias)}\s*[:：]?\s*{re.escape(mark)}|{re.escape(mark)}\s*{re.escape(alias)})", around) for mark in positive):
                    return True
                # In an equipment list, a bare feature name is affirmative evidence.
                return True
            return None

        result = [clean_text(x) for x in fallback if clean_text(x)]
        # These are property facts rendered in their own table rows, not equipment.
        result = [x for x in result if not (
            cls._nfkc(x) in {"アパート", "マンション", "一戸建て", "木造", "鉄骨造", "鉄筋コンクリート造", "RC造", "SRC造"}
            or re.fullmatch(r"\d{4}\s*[,/年]\s*\d{1,2}\s*(?:月)?\s*築", cls._nfkc(x))
            or re.fullmatch(r"\d{4}", cls._nfkc(x))
            or re.fullmatch(r"\d{1,2}\s*(?:月)?\s*築", cls._nfkc(x))
        )]
        # Remove generic-parser fragments/aliases for the KIN features that we own
        # here, then re-add only canonical labels from scoped evidence.
        owned_aliases = {cls._nfkc(a) for aliases, canonical in rules for a in (*aliases, canonical)}
        result = [x for x in result if cls._nfkc(x) not in owned_aliases]
        result = [x for x in result if cls._nfkc(x) not in {"バス", "トイレ別○", "トイレ別"}]
        for aliases, canonical in rules:
            states = [state_for(text, aliases) for text in evidence]
            states = [x for x in states if x is not None]
            if states and states[-1] is True and canonical not in result:
                result.append(canonical)
            if states and states[-1] is False:
                result = [x for x in result if cls._nfkc(x) not in {cls._nfkc(a) for a in aliases} and cls._nfkc(x) != cls._nfkc(canonical)]
        return result[:80]

    @classmethod
    def _normalized_photo_key(cls, raw_url: str) -> str:
        """Normalize obvious KIN image resize/cache variants for source dedupe.

        Keep identity-bearing query params, but ignore presentation-only dimensions,
        quality and cache-busting params. Content SHA-256 remains the final safeguard.
        """
        try:
            parts = urlsplit(raw_url)
        except Exception:
            return raw_url
        drop = {
            "w", "h", "width", "height", "size", "resize", "quality", "q",
            "cache", "cb", "ver", "v", "timestamp", "ts",
        }
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in drop]
        path = re.sub(r"/(?:thumb|thumbnail|small|medium|large)/", "/", parts.path, flags=re.I)
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))

    @classmethod
    def _dedupe_photo_sources(cls, sources: list[dict]) -> list[dict]:
        out: list[dict] = []
        seen: set[str] = set()
        for src in sources:
            url = str(src.get("url") or "").strip()
            if not url:
                continue
            key = cls._normalized_photo_key(url)
            if key in seen:
                continue
            seen.add(key)
            out.append(src)
        return out

    @classmethod
    def _kin_photo_kind(cls, source: dict) -> str:
        """Classify KIN's stable image-number convention for staff-facing order."""
        url = str(source.get("url") or "")
        name_match = re.search(r"[?&]img=([^&]+)", url, re.I)
        filename = name_match.group(1) if name_match else urlsplit(url).path.rsplit("/", 1)[-1]
        verified = VERIFIED_PHOTOS.get(filename)
        if verified and (not source.get("sha256") or source.get("sha256") == verified["sha256"]):
            return verified["kind"]
        stem = re.sub(r"\.[^.]+$", "", filename)
        parts = stem.split("_")
        lower_name = filename.lower()
        if any(word in lower_name for word in ("madori", "floorplan", "floor-plan")):
            return "floorplan"
        if any(word in lower_name for word in ("room", "living", "bedroom")):
            return "interior_room"
        if any(word in lower_name for word in ("exterior", "facade")):
            return "exterior"
        # Building-wide images omit the room identifier and are exterior/common photos.
        if name_match and len(parts) <= 3:
            return "exterior"
        # Numbered image slots are arbitrary per property, never semantic labels.
        label = str(source.get("alt_text") or source.get("alt") or "")
        for words, kind in ((('間取り',), 'floorplan'), (('居室','洋室','リビング','寝室'), 'interior_room'), (('キッチン','台所'), 'interior_kitchen'), (('トイレ',), 'interior_toilet'), (('浴室','洗面'), 'interior_bath')):
            if any(word in label for word in words):
                return kind
        if source.get("kind") == "floorplan":
            return "floorplan"
        if source.get("kind") == "exterior":
            return "exterior"
        if source.get("kind") == "interior":
            return "interior_room"
        if source.get("kind") == "common_area":
            return "other"
        return "interior_other"

    @classmethod
    def _order_photo_sources(cls, sources: list[dict]) -> list[dict]:
        classified: list[dict] = []
        for source in cls._dedupe_photo_sources(sources):
            item = dict(source)
            # KIN's type=T endpoint is only 245px. Removing it returns the 640px original.
            try:
                parts = urlsplit(str(item.get("url") or ""))
                query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not (k.lower() == "type" and v.upper() == "T")]
                item["url"] = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
            except Exception:
                pass
            item["kind"] = cls._kin_photo_kind(item)
            classified.append(item)
        priority = {
            "exterior": 7,
            "floorplan": 1,
            "interior_room": 2,
            "interior_kitchen": 3,
            "interior_toilet": 4,
            "interior_bath": 5,
            "interior_other": 6,
            "other": 8,
        }
        first_exterior = next((i for i, item in enumerate(classified) if item["kind"] == "exterior"), None)
        indexed = list(enumerate(classified))
        indexed.sort(key=lambda pair: (
            0 if pair[0] == first_exterior else priority.get(pair[1]["kind"], 8),
            pair[0],
        ))
        return [item for _, item in indexed]

    @classmethod
    def _property_photo_sources(cls, soup: BeautifulSoup, base_url: str) -> list[dict]:
        """Extract only the current KIN property's own gallery images.

        KIN detail pages can include recommendation/related-property thumbnails and
        site-wide artwork below the main property content.  Generic page-wide image
        extraction therefore over-collects. Prefer the dedicated current-property
        gallery containers and only fall back to a cleaned page when no known gallery
        container exists.
        """
        primary_selectors = (
            ".main_image_madori_eria",
            "#list_images",
            "#detail_images",
            "#property_images",
            ".detail_images",
            ".property_images",
            ".property-photo",
            ".property_photos",
            ".detail-photo",
            ".detail_photo",
            ".room-photo",
            ".room_photo",
        )
        collected: list[dict] = []
        for selector in primary_selectors:
            for node in soup.select(selector):
                found = _images(node, base_url)
                if selector == ".main_image_madori_eria":
                    found = [dict(item, kind="floorplan") for item in found]
                collected.extend(found)
        if collected:
            return cls._order_photo_sources(collected)

        # Defensive fallback for older/alternate KIN markup: remove blocks that are
        # clearly unrelated to the current property before running generic extraction.
        cleaned = BeautifulSoup(str(soup), "html.parser")
        bad_markers = (
            "オススメ", "おすすめ", "おすすめ物件", "オススメ物件",
            "関連物件", "こちらの賃貸物件", "他の部屋", "別のお部屋",
        )
        bad_name_tokens = (
            "recommend", "related", "pickup", "suggest", "banner", "bnr",
            "footer", "header", "logo", "other-room", "other_room",
        )
        for node in list(cleaned.find_all(["section", "div", "aside", "footer", "nav", "ul"])):
            name = " ".join([str(node.get("id") or ""), " ".join(node.get("class") or [])]).lower()
            text = cls._nfkc(node.get_text(" ", strip=True))[:500]
            if any(token in name for token in bad_name_tokens) or any(marker in text for marker in bad_markers):
                node.decompose()
        return cls._order_photo_sources(_images(cleaned, base_url))

    @classmethod
    def _normalize_charge(cls, raw: str, *, stop_labels: tuple[str, ...] = ()) -> str:
        text = cls._nfkc(raw)
        if not text:
            return ""
        if stop_labels:
            text = re.split(r"\s*(?:" + "|".join(re.escape(x) for x in stop_labels) + r")\s*[:：]?", text, maxsplit=1)[0]
        text = clean_text(text).strip(" :：/|")
        if not text:
            return ""
        if re.fullmatch(r"(?:無|無料|なし|無し|不要|0(?:\.0+)?(?:円|万円|ヶ月|カ月|か月|ヵ月)?|[-ー―—–])", text):
            return "0"
        # Keep a clean monetary/month value; discard labels or unrelated trailing prose.
        m = re.search(r"([0-9０-９.,，]+\s*(?:万円|円|ヶ月|カ月|か月|ヵ月|ケ月|月分))", text)
        if m:
            return cls._nfkc(m.group(1)).replace("，", ",")
        return text if len(text) <= 40 else ""

    @classmethod
    def _deposit_key_money(cls, pairs, body_text: str, fallback_deposit: str, fallback_key: str) -> tuple[str, str]:
        dep_raw = pair_value(pairs, ("敷金", "保証金"), contains=False) or fallback_deposit
        key_raw = pair_value(pairs, ("礼金",), contains=False) or fallback_key
        deposit = cls._normalize_charge(dep_raw, stop_labels=("礼金", "保証金", "敷引", "償却"))
        key_money = cls._normalize_charge(key_raw, stop_labels=("敷金", "保証金", "敷引", "償却"))

        # KIN responsive markup can collapse labels and values into one text run, such as
        # "敷金 無 礼金 無". Parse those labels independently when needed.
        if not deposit or "礼金" in cls._nfkc(dep_raw):
            m = re.search(r"敷金\s*[:：]?\s*(無(?:料)?|なし|無し|不要|[-ー―—–]|[0-9０-９.,，]+\s*(?:万円|円|ヶ月|カ月|か月|ヵ月|ケ月|月分)?)", body_text)
            if m:
                deposit = cls._normalize_charge(m.group(1))
        if not key_money or "敷金" in cls._nfkc(key_raw):
            m = re.search(r"礼金\s*[:：]?\s*(?:賃料\s*)?(無(?:料)?|なし|無し|不要|[-ー―—–]|[0-9０-９.,，]+\s*(?:万円|円|ヶ月|カ月|か月|ヵ月|ケ月|月分)?)", body_text)
            if m:
                key_money = cls._normalize_charge(m.group(1))
        return deposit, key_money

    @classmethod
    def _orientation(cls, pairs, fallback: str) -> str:
        raw = pair_value(pairs, ("向き", "方角", "方位", "主要採光面", "バルコニー方向"), contains=False) or fallback
        text = cls._nfkc(raw)
        m = re.search(r"(?<![東西南北])(北東|北西|南東|南西|東|西|南|北)(?![東西南北])", text)
        return m.group(1) if m else ""

    @classmethod
    def _move_in(cls, pairs, fallback: str) -> str:
        raw = pair_value(pairs, ("入居時期", "入居可能", "入居日", "入居予定", "入居可能日"), contains=False) or fallback
        text = cls._nfkc(raw)
        # Remove trailing IDs and widget text. Keep month-only values when supplied.
        m = re.search(r"(20\d{2})[./-](\d{1,2})(?:[./-](\d{1,2}))?", text)
        if m:
            y, mo, d = m.groups()
            return f"{y}/{int(mo):02d}/{int(d):02d}" if d else f"{y}/{int(mo):02d}"
        for word in ("即入居可", "即入居", "即日", "相談", "要相談"):
            if word in text:
                return word
        return ""

    @classmethod
    def _floor_parts(cls, pairs, fallback_floor: str, fallback_total: str) -> tuple[str, str]:
        raw = pair_value(pairs, ("所在階/階数", "所在階", "階層", "階数"), contains=False)
        text = cls._nfkc(raw or fallback_floor)
        # Example: 2階/地上2階, 2階部分(地上2階建)
        current = ""
        total = ""
        m = re.search(r"(?:^|\s)(\d+)\s*階", text)
        if m:
            current = f"{int(m.group(1))}階"
        m = re.search(r"地上\s*(\d+)\s*階", text)
        if m:
            total = f"{int(m.group(1))}階"
        if not total:
            total = cls._nfkc(fallback_total)
        return current or fallback_floor, total

    @classmethod
    def _structured_transport(cls, pairs) -> list[dict]:
        """Parse KIN's current split transport table.

        Current detail pages may render transport as three independent rows instead
        of one sentence, for example:
          路線/バス会社: 京成本線 /
          駅名/停留所名: 京成大久保駅 /
          徒歩時間: 京成大久保駅：14分 / 停留所：
        Older 0.1.7 logic only parsed values that already contained line + station +
        walk in one string, so these rows never formed a complete transport record.
        """
        line_raw = cls._nfkc(pair_value(pairs, ("路線/バス会社", "路線／バス会社"), contains=False))
        station_raw = cls._nfkc(pair_value(pairs, ("駅名/停留所名", "駅名／停留所名"), contains=False))
        walk_raw = cls._nfkc(pair_value(pairs, ("徒歩時間",), contains=False))
        if not (line_raw and station_raw and walk_raw):
            return []

        def cells(value: str) -> list[str]:
            return [clean_text(x) for x in re.split(r"[／/]", value) if clean_text(x)]

        lines = cells(line_raw)
        stations = cells(station_raw)
        walk_cells = cells(walk_raw)
        out: list[dict] = []
        # A KIN row may list a distant railway station with no time followed by
        # a bus stop with a walk time, while the route/company row contains only
        # the railway line (e.g. 戸塚駅： / 中村三叉路停留所：7分).  Walk the
        # station cells, not the shorter line cells, and keep only entries that
        # have an explicit time.  Never borrow another cell's time.
        for idx, station_token in enumerate(stations):
            line = lines[idx] if idx < len(lines) else ""
            station = re.sub(r"(?:駅|停留所)$", "", station_token).strip()
            if not station:
                continue

            walk = None
            matched_walk_token = ""
            # Prefer a walk cell naming the same station; otherwise use same index.
            for token in walk_cells:
                m = re.search(rf"{re.escape(station)}(?:駅)?\s*[:：]?\s*(\d+)\s*分", token)
                if m:
                    walk = int(m.group(1))
                    matched_walk_token = token
                    break
            if walk is None and idx < len(walk_cells):
                m = re.search(r"(\d+)\s*分", walk_cells[idx])
                if m:
                    walk = int(m.group(1))
                    matched_walk_token = walk_cells[idx]
            if walk is None:
                continue

            stop_suffix = "停留所" if "停留所" in (station_token + matched_walk_token) else "駅"
            raw = clean_text(f"{line} {station}{stop_suffix} 徒歩{walk}分")
            out.append({"line": line, "station": station, "walk_minutes": walk, "raw": raw})
        return out

    @classmethod
    def _transport(cls, soup: BeautifulSoup, pairs, fallback: list) -> list:
        structured = cls._structured_transport(pairs)
        if structured:
            return structured

        found: list[str] = []
        for label in ("交通", "アクセス", "最寄駅"):
            v = pair_value(pairs, (label,), contains=False)
            if v:
                found.append(cls._nfkc(v))
        # KIN templates may render each station in separate text nodes.
        for node in soup.find_all(string=re.compile(r"駅")):
            text = cls._nfkc(node)
            parent = getattr(node, "parent", None)
            if parent is not None:
                whole = cls._nfkc(parent.get_text(" ", strip=True))
                if "駅" in whole and ("徒歩" in whole or "バス" in whole) and len(whole) <= 180:
                    found.append(whole)
            if "駅" in text and ("徒歩" in text or "バス" in text):
                found.append(text)
        out: list[dict] = []
        seen: set[str] = set()
        for raw in found:
            raw = clean_text(raw)
            if not raw or raw in seen:
                continue
            seen.add(raw)
            # KIN commonly wraps station names in Japanese quotes, e.g.
            # 京成本線「京成津田沼」駅 徒歩12分. Accept both quoted and bare forms.
            m = re.search(
                r"(?P<line>.+?(?:線|ライン))\s*[「『\"](?P<station>[^」』\"]+)[」』\"]\s*駅[^0-9]{0,20}(?:徒歩|歩)\s*(?P<walk>\d+)分",
                raw,
            )
            if not m:
                m = re.search(
                    r"(?P<line>.+?(?:線|ライン))\s*(?P<station>[^,/、()（）]+?)\s*駅[^0-9]{0,20}(?:徒歩|歩)\s*(?P<walk>\d+)分",
                    raw,
                )
            if m:
                out.append({
                    "line": clean_text(m.group("line") or ""),
                    "station": clean_text(m.group("station") or "").strip("「」『』\" "),
                    "walk_minutes": int(m.group("walk")),
                    "raw": raw,
                })
            else:
                out.append({"raw": raw})
            if len(out) >= 6:
                break
        return out or fallback

    def parse(self, html: str, url: str) -> PropertyCandidate:
        source_id, source_id_kind = self.source_id(url)
        data = extract_generic(
            html,
            url,
            management_company=self.management_company,
            source_property_id=source_id,
            source_id_kind=source_id_kind,
        )
        soup = BeautifulSoup(html, "html.parser")
        pairs = _pairs(soup)
        body_text = self._nfkc(soup.get_text(" ", strip=True))

        combined_rent, combined_fee = self._combined_rent_fee(pairs, body_text)
        if combined_rent:
            data["rent"] = combined_rent
        if combined_fee:
            data["management_fee"] = combined_fee
        elif not int(data.get("management_fee") or 0):
            standalone = pair_value(pairs, ("管理費・共益費", "共益費", "管理費"), contains=False)
            if standalone:
                data["management_fee"] = parse_yen(standalone)
            else:
                m = re.search(r"(?:管理費|共益費)\s*[:：]?\s*([0-9,]+)\s*円", body_text)
                if m:
                    data["management_fee"] = parse_yen(m.group(1))

        building = clean_text(str(data.get("building_name") or ""))
        # Remove a trailing room from a KIN heading/title even if generic extraction missed the room.
        building = re.sub(r"\s+[0-9０-９]{1,5}[A-Za-z]?\s*号室.*$", "", building).strip()
        table_fields = self._table_fields(soup)
        structured_pairs = pairs.copy()
        for key, values in table_fields.items():
            structured_pairs[key] = values
        room = self._room(soup, structured_pairs, building, str(data.get("room") or ""))
        data["address"] = self._full_address(soup, structured_pairs, str(data.get("address") or ""))
        data["prefecture"] = extract_prefecture(str(data.get("address") or ""))
        data["orientation"] = self._orientation(pairs, str(data.get("orientation") or ""))
        data["move_in_date"] = self._move_in(pairs, str(data.get("move_in_date") or ""))
        data["floor"], data["total_floors"] = self._floor_parts(
            pairs,
            str(data.get("floor") or ""),
            str(data.get("total_floors") or ""),
        )
        # structured_pairs already includes independently preserved KIN table pairs.
        # This fixes split transport/charge rows and room labels in 4-cell layouts.
        data["transport"] = self._transport(soup, structured_pairs, list(data.get("transport") or []))
        data["deposit"], data["key_money"] = self._deposit_key_money(
            structured_pairs,
            body_text,
            str(data.get("deposit") or ""),
            str(data.get("key_money") or ""),
        )
        data["equipment"] = self._equipment_features(soup, table_fields, list(data.get("equipment") or []))
        data["photo_sources"] = self._property_photo_sources(soup, url)

        address = str(data.get("address") or "").strip()
        pref = str(data.get("prefecture") or "").strip()
        warnings: list[str] = []
        if not building:
            warnings.append("건물명 추출 실패")
        if not room:
            warnings.append("호실 추출 실패")
        if pref not in ALLOWED_PREFECTURES:
            warnings.append("1도3현 주소 확인 실패")
        if not address:
            warnings.append("주소 추출 실패")
        if not int(data.get("management_fee") or 0):
            warnings.append("관리비 0/미표기 - 원본 확인 권장")
        if not building or not room or pref not in ALLOWED_PREFECTURES:
            raise ValueError(" / ".join(warnings))

        return PropertyCandidate(
            source_site=canonical_host(url),
            source_property_id=source_id,
            management_company=self.management_company,
            building_name=building,
            room=room,
            prefecture=pref,
            address=address,
            source_url=url,
            rent=int(data.get("rent") or 0),
            management_fee=int(data.get("management_fee") or 0),
            deposit=str(data.get("deposit") or ""),
            key_money=str(data.get("key_money") or ""),
            area=data.get("area"),
            layout=str(data.get("layout") or ""),
            built_date=str(data.get("built_date") or ""),
            floor=str(data.get("floor") or ""),
            total_floors=str(data.get("total_floors") or ""),
            structure=str(data.get("structure") or ""),
            orientation=str(data.get("orientation") or ""),
            move_in_date=str(data.get("move_in_date") or ""),
            transport=list(data.get("transport") or []),
            equipment=list(data.get("equipment") or []),
            photo_sources=list(data.get("photo_sources") or []),
            source_id_kind=source_id_kind,
            scrape_warnings=warnings,
            collected_info={
                "source_site": canonical_host(url),
                "source_url": url,
                "table_fields": table_fields,
            },
        )
