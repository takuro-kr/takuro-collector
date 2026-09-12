from __future__ import annotations

import json
from typing import Any


class RequiredListingError(ValueError):
    pass


def _list_value(row: dict, key: str) -> list:
    value: Any = row.get(key)
    if value is None:
        value = row.get(f"{key}_json")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = []
    return list(value) if isinstance(value, list) else []


def homepage_payload(row: dict) -> dict:
    """Return the one canonical property contract consumed by TAKURO.

    Source identity fields are kept only for duplicate/update matching. Everything
    else corresponds directly to information shown on the property page.
    """
    rent = int(row.get("rent") or 0)
    fee = int(row.get("management_fee") or 0)
    payload = {
        # Internal identity; not presentation content.
        "source_site": str(row.get("source_site") or "")[:300],
        "source_property_id": str(row.get("source_property_id") or "")[:300],
        "management_company": str(row.get("management_company") or "")[:300],
        "source_url": str(row.get("source_url") or "")[:2000],
        # Homepage property information.
        "building_name": str(row.get("building_name") or "")[:1000],
        "room": str(row.get("room") or "")[:100],
        "prefecture": str(row.get("prefecture") or "")[:100],
        "address": str(row.get("address") or "")[:1000],
        "transport": _list_value(row, "transport")[:10],
        "rent": rent,
        "management_fee": fee,
        "total_monthly_cost": rent + fee,
        "deposit": str(row.get("deposit") or "")[:300],
        "key_money": str(row.get("key_money") or "")[:300],
        "layout": str(row.get("layout") or "")[:100],
        "area": row.get("area"),
        "built_date": str(row.get("built_date") or "")[:100],
        "floor": str(row.get("floor") or "")[:100],
        "total_floors": str(row.get("total_floors") or "")[:100],
        "orientation": str(row.get("orientation") or "")[:100],
        "move_in_date": str(row.get("move_in_date") or "")[:300],
        "structure": str(row.get("structure") or "")[:300],
        "equipment": _list_value(row, "equipment")[:100],
    }
    missing = []
    if not payload["building_name"]:
        missing.append("매물명")
    if not payload["room"]:
        missing.append("호실")
    if not payload["address"]:
        missing.append("주소")
    if rent <= 0:
        missing.append("월세")
    if missing:
        raise RequiredListingError("필수 매물 정보 누락: " + ", ".join(missing))
    return payload

