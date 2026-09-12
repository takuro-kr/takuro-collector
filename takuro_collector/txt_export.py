from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any

from .utils import safe_filename


def _digits(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "")


def _split_address(address: str, pref: str) -> tuple[str, str, str]:
    rest = (address or "").strip()
    if pref and rest.startswith(pref):
        rest = rest[len(pref):]
    # Prefer 市+区 as one locality for Yokohama/Kawasaki/Saitama wards, otherwise first municipality suffix.
    m = re.match(r"^(.+?市.+?区)(.*)$", rest)
    if not m:
        m = re.match(r"^(.+?(?:市|区|町|村))(.*)$", rest)
    if m:
        locality, rest2 = m.group(1), m.group(2)
    else:
        locality, rest2 = "", rest
    rest2 = _digits(rest2)
    m2 = re.match(r"^(.+?\d+丁目)(.*)$", rest2)
    if m2:
        return locality, m2.group(1), m2.group(2).strip()
    return locality, rest2, ""


def _layout(layout: str) -> tuple[str, int]:
    v = _digits(layout).upper().replace(" ", "")
    if v in {"1R", "R", "ワンルーム", "ONE-ROOM", "ONEROOM"}:
        return "ワンルーム", 1
    m = re.match(r"^(\d+)(K|DK|LDK|SK|SDK|SLDK)$", v)
    if not m:
        return layout, 0
    count = int(m.group(1))
    fw = str.maketrans({"K": "Ｋ", "D": "Ｄ", "L": "Ｌ", "S": "Ｓ"})
    return m.group(2).translate(fw), count


def _structure(value: str) -> str:
    v = _digits(value).upper().strip()
    if v in {"RC", "ＲＣ"} or "鉄筋コンクリート" in value:
        return "ＲＣ"
    if v in {"SRC", "ＳＲＣ"} or "鉄骨鉄筋コンクリート" in value:
        return "ＳＲＣ"
    if v in {"S", "Ｓ"} or "鉄骨" in value:
        return "鉄骨"
    if "木造" in value:
        return "木造"
    return value


def _orientation(value: str) -> str:
    m = {
        "북향": "北", "남향": "南", "동향": "東", "서향": "西",
        "북동향": "北東", "북서향": "北西", "남동향": "南東", "남서향": "南西",
        "north": "北", "south": "南", "east": "東", "west": "西",
        "northeast": "北東", "northwest": "北西", "southeast": "南東", "southwest": "南西",
    }
    key = (value or "").strip().lower()
    return m.get(key, value)


def _built(value: str) -> str:
    v = _digits(value).strip()
    m = re.search(r"(\d{4})[-/.年](\d{1,2})", v)
    if m:
        return f"{int(m.group(1))}年 {int(m.group(2))}月"
    return value


def _floor(value: str) -> str:
    v = _digits(value).strip()
    m = re.search(r"-?\d+", v)
    return f"{m.group(0)}階" if m else value


def _move_in(value: str) -> tuple[str, str, str]:
    v = _digits(value).strip()
    if not v:
        return "", "", ""
    if re.search(r"即時|即入居|即日|即入居可", v):
        return "即時", "", ""
    m = re.search(r"(\d{4})[-/.年](\d{1,2})(?:[-/.月](\d{1,2})日?)?", v)
    phase = ""
    if "上旬" in v or "初旬" in v:
        phase = "上旬"
    elif "中旬" in v:
        phase = "中旬"
    elif "下旬" in v or "月末" in v:
        phase = "下旬"
    if m:
        date = f"{int(m.group(1))}年 {int(m.group(2))}月"
        if m.group(3):
            date += f" {int(m.group(3))}日"
        return "予定", date, phase
    # If the source already contains a Japanese year/month notation, preserve it.
    if "年" in v and "月" in v:
        return "予定", v, phase
    return "", "", ""


def _parse_transport_raw(raw: str) -> tuple[str, str, int | None]:
    """Parse common Japanese railway text without inventing missing facts.

    KIN frequently writes stations as e.g. 京成本線「京成津田沼」駅 徒歩12分.
    Older parsing expected a bare station token and silently dropped those rows from
    基本情報.txt.  This parser accepts quoted/unquoted station names and optional walk
    minutes, while still requiring both an explicit line and station.
    """
    text = _digits(raw).strip()
    if not text:
        return "", "", None

    walk: int | None = None
    wm = re.search(r"(?:徒歩|歩)\s*([0-9]+)\s*分", text)
    if wm:
        walk = int(wm.group(1))

    # Prefer Japanese quote marks because KIN commonly wraps the station name.
    qm = re.search(r"(?P<line>.+?(?:線|ライン))\s*[「『\"](?P<station>[^」』\"]+)[」』\"]\s*駅", text)
    if qm:
        return clean_transport(qm.group("line")), clean_transport(qm.group("station")), walk

    # Unquoted form: 京浜東北線 西川口駅 徒歩8分 / JR総武線幕張本郷駅 ...
    um = re.search(r"(?P<line>.+?(?:線|ライン))\s*(?P<station>[^,/、()（）]+?)\s*駅", text)
    if um:
        line = clean_transport(um.group("line"))
        station = clean_transport(um.group("station"))
        # Avoid swallowing common separators into the station name.
        station = re.sub(r"^[：:・\s]+|[：:・\s]+$", "", station)
        return line, station, walk
    return "", "", walk


def clean_transport(value: str) -> str:
    return re.sub(r"\s+", " ", _digits(value).strip())


def _transport_rows(items: list[Any]) -> list[tuple[str, str, int | None]]:
    out: list[tuple[str, str, int | None]] = []
    seen: set[tuple[str, str, int | None]] = set()
    for item in items:
        if isinstance(item, dict):
            line = clean_transport(str(item.get("line") or ""))
            station = clean_transport(str(item.get("station") or "")).removesuffix("駅")
            walk = item.get("walk_minutes")
            raw = str(item.get("raw") or "")
        else:
            line = station = ""
            walk = None
            raw = str(item)
        if not (line and station):
            raw_line, raw_station, raw_walk = _parse_transport_raw(raw)
            line = line or raw_line
            station = station or raw_station
            if walk in (None, ""):
                walk = raw_walk
        if line and station:
            try:
                walk_i = int(walk) if walk is not None and str(walk).strip() else None
            except Exception:
                walk_i = None
            row = (line, station, walk_i)
            if row not in seen:
                seen.add(row)
                out.append(row)
        if len(out) >= 3:
            break
    return out


def build_text(prop: dict) -> str:
    lines: list[str] = ["基本情報", "物件番号", str(prop.get("source_property_id") or "")]
    company = str(prop.get("management_company") or "").strip()
    if company:
        lines += ["会員情報", "商号", company, "代表電話番号"]
    lines += ["価格"]
    rent = int(prop.get("rent") or 0)
    if rent > 0:
        lines += ["賃料", f"{rent:,}円"]
    deposit = str(prop.get("deposit") or "").strip()
    key_money = str(prop.get("key_money") or "").strip()
    # Important: do not emit blank 敷金/礼金 labels. In TAKURO FAST a recognized blank field means explicit zero.
    if deposit:
        lines += ["敷金", deposit]
    if key_money:
        lines += ["礼金", key_money]

    area = prop.get("area")
    if area not in (None, ""):
        lines += ["面積・不動産ＩＤ", "基本情報", "使用部分面積", f"{float(area):g}㎡", "不動産ＩＤ（建物）"]

    pref = str(prop.get("prefecture") or "")
    address = str(prop.get("address") or "")
    a1, a2, a3 = _split_address(address, pref)
    lines += [
        "所在", "都道府県名", pref,
        "所在地名１", a1,
        "所在地名２", a2,
        "所在地名３", a3,
        "建物名", str(prop.get("building_name") or ""),
        "部屋番号", str(prop.get("room") or ""),
        "その他所在地表示",
    ]

    transport = prop.get("transport") or []
    trows = _transport_rows(transport if isinstance(transport, list) else [])
    if trows:
        lines.append("交通")
        for i, (line, station, walk) in enumerate(trows, start=1):
            lines += [f"交通{i}", "沿線名", line, "駅名", station, "駅より徒歩", f"{walk}分" if walk is not None else "", "駅より車"]
        lines += ["交通その他"]

    layout_type, room_count = _layout(str(prop.get("layout") or ""))
    if layout_type:
        lines += ["間取", "間取タイプ", layout_type]
        if room_count:
            lines += ["間取部屋数", f"{room_count}室"]
        lines += ["室１:所在階"]

    lines.append("建物")
    built = _built(str(prop.get("built_date") or ""))
    if built:
        lines += ["築年月", built]
    structure = _structure(str(prop.get("structure") or ""))
    if structure:
        lines += ["建物構造", structure]
    total = str(prop.get("total_floors") or "").strip()
    if total:
        lines += ["地上階層", _floor(total), "地下階層"]
    floor = str(prop.get("floor") or "").strip()
    if floor:
        lines += ["所在階", _floor(floor)]
    orientation = _orientation(str(prop.get("orientation") or ""))
    if orientation:
        lines += ["バルコニー方向", orientation]
    lines += ["増改築年月１", "維持"]

    management = int(prop.get("management_fee") or 0)
    if management > 0:
        lines += ["管理費", f"{management:,}円", "うち管理費消費税"]
    lines += ["更新区分"]

    move_kind, move_date, phase = _move_in(str(prop.get("move_in_date") or ""))
    if move_kind:
        lines += ["入居", "入居時期", move_kind]
        if move_date:
            lines += ["入居年月", move_date]
            if phase:
                lines.append(phase)

    equipment = prop.get("equipment") or []
    if isinstance(equipment, str):
        eq_text = equipment
    else:
        eq_text = ",".join(str(x).strip() for x in equipment if str(x).strip())
    if eq_text:
        lines += ["取引", "設備・条件・住宅性能等", "設備・条件・住宅性能等", eq_text, "設備(フリースペース)"]

    # Remove trailing blank lines but keep intentionally blank values in the middle.
    return "\n".join(str(x) for x in lines).rstrip() + "\n"


def write_text(prop: dict, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "基本情報.txt"
    path.write_text(build_text(prop), encoding="utf-8", newline="\n")
    return path
