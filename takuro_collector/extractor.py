from __future__ import annotations

import json
import re
import unicodedata
from collections import OrderedDict
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .utils import ALLOWED_PREFECTURES, extract_prefecture, parse_area, parse_yen

SPACE_RE = re.compile(r"[\s\u3000]+")
IMAGE_EXT_RE = re.compile(r"\.(?:jpe?g|png|webp)(?:\?.*)?$", re.I)

EXCLUDE_IMAGE_WORDS = (
    "周辺", "周辺施設", "地図", "map", "google", "logo", "ロゴ", "icon", "アイコン", "banner", "バナー",
    "学校", "病院", "コンビニ", "スーパー", "駅周辺", "environment", "nearby", "accessmap", "qr",
)
FLOORPLAN_WORDS = ("間取り", "間取", "madori", "floorplan", "floor-plan", "layout", "間取図")
EXTERIOR_WORDS = ("外観", "外景", "建物外観", "exterior", "facade")
COMMON_WORDS = ("共用", "共用部", "エントランス", "ロビー", "廊下", "エレベータ", "集合ポスト", "宅配ボックス", "common")
INTERIOR_WORDS = ("室内", "居室", "洋室", "和室", "リビング", "キッチン", "台所", "浴室", "バス", "トイレ", "洗面", "玄関", "収納", "クローゼット", "interior", "living", "kitchen", "bath", "bedroom")


def clean_text(value: Any) -> str:
    # Preserve source spelling/full-width characters for REINS copy/search and evidence.
    # Numeric parsing normalizes separately where needed.
    return SPACE_RE.sub(" ", str(value or "")).strip()


def compare_text(value: Any) -> str:
    return unicodedata.normalize("NFKC", clean_text(value))


def first_nonempty(*values: Any) -> str:
    for v in values:
        t = clean_text(v)
        if t:
            return t
    return ""


def _jsonld_objects(soup: BeautifulSoup) -> list[dict]:
    out: list[dict] = []
    for tag in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw)
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop(0)
            if isinstance(item, dict):
                out.append(item)
                graph = item.get("@graph")
                if isinstance(graph, list):
                    stack.extend(graph)
            elif isinstance(item, list):
                stack.extend(item)
    return out


def _address_from_jsonld(rows: list[dict]) -> str:
    for obj in rows:
        addr = obj.get("address")
        if isinstance(addr, str) and any(p in addr for p in ALLOWED_PREFECTURES):
            return clean_text(addr)
        if isinstance(addr, dict):
            pieces = [addr.get("addressRegion"), addr.get("addressLocality"), addr.get("streetAddress")]
            value = "".join(clean_text(x) for x in pieces if x)
            if any(p in value for p in ALLOWED_PREFECTURES):
                return value
    return ""


def _name_from_jsonld(rows: list[dict]) -> str:
    preferred = {"Apartment", "House", "Residence", "Accommodation", "Product", "Place", "RealEstateListing"}
    for obj in rows:
        typ = obj.get("@type")
        types = set(typ if isinstance(typ, list) else [typ])
        if types & preferred and obj.get("name"):
            return clean_text(obj.get("name"))
    for obj in rows:
        if obj.get("name"):
            return clean_text(obj.get("name"))
    return ""


def _price_from_jsonld(rows: list[dict]) -> int:
    for obj in rows:
        offers = obj.get("offers")
        candidates = offers if isinstance(offers, list) else [offers]
        for offer in candidates:
            if isinstance(offer, dict) and offer.get("price") is not None:
                return parse_yen(offer.get("price"))
        if obj.get("price") is not None:
            return parse_yen(obj.get("price"))
    return 0


def _pairs(soup: BeautifulSoup) -> OrderedDict[str, list[str]]:
    pairs: OrderedDict[str, list[str]] = OrderedDict()

    def add(k: str, v: str) -> None:
        k, v = clean_text(k), clean_text(v)
        if not k or not v or k == v:
            return
        pairs.setdefault(k, [])
        if v not in pairs[k]:
            pairs[k].append(v)

    for tr in soup.find_all("tr"):
        cells = tr.find_all(["th", "td"], recursive=False)
        if len(cells) >= 2:
            add(cells[0].get_text(" ", strip=True), " ".join(c.get_text(" ", strip=True) for c in cells[1:]))
    for dt in soup.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        if dd:
            add(dt.get_text(" ", strip=True), dd.get_text(" ", strip=True))
    # Common list/card markup where a label and value are siblings.
    for label in soup.find_all(class_=re.compile(r"(?:label|ttl|title|term|head|name)", re.I)):
        text = clean_text(label.get_text(" ", strip=True))
        if not text or len(text) > 40:
            continue
        sibling = label.find_next_sibling()
        if sibling:
            value = clean_text(sibling.get_text(" ", strip=True))
            if value and len(value) < 500:
                add(text, value)
    return pairs


def pair_value(pairs: OrderedDict[str, list[str]], labels: tuple[str, ...], *, contains: bool = True) -> str:
    normalized = [(compare_text(k), vals) for k, vals in pairs.items()]
    for label in labels:
        nl = compare_text(label)
        for key, vals in normalized:
            if key == nl or (contains and nl in key):
                for v in vals:
                    if v:
                        return clean_text(v)
    return ""


def _line_candidates(soup: BeautifulSoup) -> list[str]:
    lines = []
    for line in soup.get_text("\n", strip=True).splitlines():
        t = clean_text(line)
        if t and len(t) <= 300:
            lines.append(t)
    return lines


def _address_from_text(lines: list[str]) -> str:
    for line in lines:
        if any(pref in line for pref in ALLOWED_PREFECTURES):
            m = re.search(r"(東京都|千葉県|埼玉県|神奈川県)([^|｜\n]{1,100})", line)
            if m:
                value = clean_text(m.group(1) + m.group(2))
                # Trim common trailing labels accidentally caught on one line.
                value = re.split(r"(?:交通|アクセス|賃料|家賃|間取り|専有面積|管理費)", value, maxsplit=1)[0].strip()
                if len(value) >= 5:
                    return value
    return ""


def _room_from_text(text: str) -> str:
    patterns = [
        r"(?:部屋番号|号室|ROOM|Room)\s*[:：#]?\s*([0-9０-９A-Za-z_-]+)",
        r"([0-9０-９]{2,5}[A-Za-z]?)\s*号室",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            return clean_text(m.group(1))
    return ""


def _strip_room_from_name(name: str, room: str) -> str:
    name = clean_text(name)
    if not name:
        return ""
    if room:
        r = re.escape(clean_text(room))
        name = re.sub(rf"\s*[-｜|/]?\s*{r}\s*(?:号室)?\s*$", "", name, flags=re.I)
    # Remove site-title suffixes.
    name = re.split(r"\s*[｜|]\s*(?:賃貸|物件|お部屋|空室|マンション|アパート).*", name, maxsplit=1)[0]
    return clean_text(name)


def _transport(lines: list[str], pairs: OrderedDict[str, list[str]]) -> list[dict[str, Any]]:
    found: list[str] = []
    for key, values in pairs.items():
        if any(x in key for x in ("交通", "アクセス", "最寄", "駅")):
            found.extend(values)
    for line in lines:
        if "駅" in line and ("徒歩" in line or "バス" in line) and len(line) < 180:
            found.append(line)
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in found:
        raw = clean_text(raw)
        if not raw or raw in seen:
            continue
        seen.add(raw)
        m = re.search(r"(?P<line>[^\s,/、]+(?:線|ライン))?[\s　]*(?P<station>[^\s,/、]+?)駅[^0-9]{0,20}(?:徒歩|歩)(?P<walk>[0-9０-９]+)分", raw)
        if m:
            result.append({
                "line": clean_text(m.group("line") or ""),
                "station": clean_text(m.group("station") or ""),
                "walk_minutes": int(unicodedata.normalize("NFKC", m.group("walk"))),
                "raw": raw,
            })
        else:
            result.append({"raw": raw})
        if len(result) >= 6:
            break
    return result


def _equipment(pairs: OrderedDict[str, list[str]], lines: list[str]) -> list[str]:
    raw = pair_value(pairs, ("設備", "設備・条件", "設備条件", "こだわり", "特徴", "物件設備"))
    if not raw:
        for line in lines:
            if any(x in line for x in ("オートロック", "宅配ボックス", "浴室乾燥", "エレベーター", "独立洗面", "バス・トイレ別")) and len(line) < 500:
                raw = line
                break
    if not raw:
        return []
    parts = re.split(r"[,、/｜|・\n]+", raw)
    return [clean_text(x) for x in parts if clean_text(x)][:80]


def _images(soup: BeautifulSoup, base_url: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(raw_url: str, alt: str = "", context: str = "") -> None:
        raw_url = (raw_url or "").strip()
        if not raw_url or raw_url.startswith(("data:", "javascript:", "#")):
            return
        url = urljoin(base_url, raw_url)
        if url in seen:
            return
        label = clean_text(" ".join([alt, context, url]))
        low = label.lower()
        if any(word.lower() in low for word in EXCLUDE_IMAGE_WORDS):
            return
        if any(word.lower() in low for word in FLOORPLAN_WORDS):
            kind = "floorplan"
        elif any(word.lower() in low for word in EXTERIOR_WORDS):
            kind = "exterior"
        elif any(word.lower() in low for word in COMMON_WORDS):
            kind = "common_area"
        elif any(word.lower() in low for word in INTERIOR_WORDS):
            kind = "interior"
        else:
            kind = "photo"
        # Keep URL-like images even when extension is generated dynamically; discard obvious SVG/GIF icons.
        if re.search(r"\.(?:svg|gif|ico)(?:\?|$)", url, re.I):
            return
        seen.add(url)
        out.append({"url": url, "alt": clean_text(alt), "kind": kind})

    for img in soup.find_all("img"):
        alt = img.get("alt") or img.get("title") or ""
        context = ""
        parent = img.parent
        has_full_link = False
        if parent:
            context = clean_text(parent.get_text(" ", strip=True))[:150]
            if getattr(parent, "name", "") == "a":
                href = parent.get("href") or ""
                if href and (IMAGE_EXT_RE.search(href) or "image" in href.lower() or "photo" in href.lower()):
                    add(href, alt, context)
                    has_full_link = True
        # When an image links directly to a full-size file, prefer that source and avoid
        # keeping the thumbnail as a second candidate. Otherwise try lazy/original attrs first.
        attrs = ("data-original", "data-large", "data-src", "data-lazy-src", "src")
        if not has_full_link:
            for attr in attrs:
                add(str(img.get(attr) or ""), alt, context)
        srcset = str(img.get("srcset") or img.get("data-srcset") or "")
        if srcset:
            # Prefer the last/largest candidate.
            bits = [x.strip().split()[0] for x in srcset.split(",") if x.strip()]
            if bits:
                add(bits[-1], alt, context)

    og = soup.find("meta", attrs={"property": "og:image"})
    if og:
        add(str(og.get("content") or ""), "og:image", "")

    # Background-image URLs are common in carousel widgets.
    html = str(soup)
    for m in re.finditer(r"(?:background-image\s*:\s*url\(|data-(?:src|image)=)[\"']?([^\"')\s>]+)", html, re.I):
        add(m.group(1), "", "background")

    # Floorplan first, then ordinary photos; preserve discovery order within each group.
    order = {"floorplan": 0, "interior": 1, "exterior": 2, "common_area": 3, "photo": 4}
    return sorted(out, key=lambda x: order.get(x["kind"], 9))[:120]


def extract_generic(html: str, url: str, *, management_company: str = "", source_property_id: str = "", source_id_kind: str = "site") -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    rows = _jsonld_objects(soup)
    pairs = _pairs(soup)
    lines = _line_candidates(soup)
    body_text = clean_text(soup.get_text(" ", strip=True))

    room = first_nonempty(
        pair_value(pairs, ("部屋番号", "部屋", "号室", "室番号", "ROOM")),
        _room_from_text(body_text),
    )

    h1 = soup.find("h1")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    building = first_nonempty(
        pair_value(pairs, ("建物名", "物件名", "物件名称", "マンション名", "建物名称")),
        _name_from_jsonld(rows),
        h1.get_text(" ", strip=True) if h1 else "",
        title,
    )
    building = _strip_room_from_name(building, room)

    address = first_nonempty(
        pair_value(pairs, ("所在地", "住所", "物件所在地", "所在地住所")),
        _address_from_jsonld(rows),
        _address_from_text(lines),
    )
    prefecture = extract_prefecture(address)

    rent_raw = pair_value(pairs, ("賃料", "家賃", "月額賃料"))
    rent = parse_yen(rent_raw) or _price_from_jsonld(rows)
    management_fee = parse_yen(pair_value(pairs, ("管理費", "共益費", "管理・共益費")))
    deposit = pair_value(pairs, ("敷金", "保証金"))
    key_money = pair_value(pairs, ("礼金",))
    area_raw = pair_value(pairs, ("専有面積", "使用部分面積", "面積"))
    area = parse_area(area_raw)
    layout = pair_value(pairs, ("間取り", "間取", "間取タイプ"))
    built_date = pair_value(pairs, ("築年月", "築年", "竣工", "築年月日"))
    floor = pair_value(pairs, ("所在階", "階数", "階"))
    total_floors = pair_value(pairs, ("地上階層", "総階数", "建物階数"))
    structure = pair_value(pairs, ("建物構造", "構造"))
    orientation = pair_value(pairs, ("向き", "方角", "バルコニー方向", "主要採光面"))
    move_in = pair_value(pairs, ("入居時期", "入居可能", "入居日", "入居予定"))

    return {
        "management_company": management_company,
        "building_name": building,
        "room": room,
        "prefecture": prefecture,
        "address": address,
        "rent": rent,
        "management_fee": management_fee,
        "deposit": deposit,
        "key_money": key_money,
        "area": area,
        "layout": layout,
        "built_date": built_date,
        "floor": floor,
        "total_floors": total_floors,
        "structure": structure,
        "orientation": orientation,
        "move_in_date": move_in,
        "transport": _transport(lines, pairs),
        "equipment": _equipment(pairs, lines),
        "photo_sources": _images(soup, url),
        "source_property_id": source_property_id,
        "source_id_kind": source_id_kind,
        "debug_pairs": {k: v[:3] for k, v in list(pairs.items())[:80]},
    }
