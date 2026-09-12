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
        from .required_listing import homepage_payload

        return homepage_payload(self.to_dict())
