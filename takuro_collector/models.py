from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class PropertyCandidate:
    source_site: str
    source_property_id: str
    management_company: str
    building_name: str
    room: str
    prefecture: str
    address: str
    source_url: str
    rent: int = 0
    management_fee: int = 0
    deposit: str = ""
    key_money: str = ""
    area: float | None = None
    layout: str = ""
    built_date: str = ""
    floor: str = ""
    total_floors: str = ""
    structure: str = ""
    orientation: str = ""
    move_in_date: str = ""
    transport: list[Any] = field(default_factory=list)
    equipment: list[str] = field(default_factory=list)
    photo_sources: list[dict[str, Any]] = field(default_factory=list)
    source_id_kind: str = "site"
    scrape_warnings: list[str] = field(default_factory=list)
    collected_info: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def wp_payload(self) -> dict[str, Any]:
        return {
            "source_site": self.source_site,
            "source_property_id": self.source_property_id,
            "management_company": self.management_company,
            "building_name": self.building_name,
            "room": self.room,
            "prefecture": self.prefecture,
            "address": self.address,
            "rent": int(self.rent or 0),
            "management_fee": int(self.management_fee or 0),
            "source_url": self.source_url,
            "deposit": self.deposit,
            "key_money": self.key_money,
            "area": self.area,
            "layout": self.layout,
            "built_date": self.built_date,
            "floor": self.floor,
            "total_floors": self.total_floors,
            "structure": self.structure,
            "orientation": self.orientation,
            "move_in_date": self.move_in_date,
            "transport": self.transport,
            "equipment": self.equipment,
        }
