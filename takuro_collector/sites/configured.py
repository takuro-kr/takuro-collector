from __future__ import annotations

import json
import re
from pathlib import Path

from bs4 import BeautifulSoup

from ..extractor import extract_generic
from ..paths import data_root
from .base import BaseAdapter


class ConfiguredAdapter(BaseAdapter):
    def __init__(self, spec: dict):
        self.code = str(spec["code"]).strip().upper()
        self.label = str(spec["label"]).strip()
        self.management_company = str(spec.get("management_company") or self.label).strip()
        self.domains = tuple(str(value).strip().lower() for value in spec["domains"])
        self.seed_urls = tuple(str(value).strip() for value in spec.get("seed_urls", []))
        self.detail_patterns = tuple(str(value) for value in spec["detail_patterns"])
        self.force_browser = bool(spec.get("force_browser", False))
        self.login_expected = bool(spec.get("login_expected", False))
        self.selectors = dict(spec.get("selectors") or {})

    @staticmethod
    def _text(soup: BeautifulSoup, selector: str) -> str:
        node = soup.select_one(selector)
        return node.get_text(" ", strip=True) if node else ""

    @staticmethod
    def _number(value: str) -> int:
        match = re.search(r"[\d,]+", value or "")
        return int(match.group().replace(",", "")) if match else 0

    def parse(self, html: str, url: str):
        source_id, source_id_kind = self.source_id(url)
        data = extract_generic(
            html,
            url,
            management_company=self.management_company,
            source_property_id=source_id,
            source_id_kind=source_id_kind,
        )
        soup = BeautifulSoup(html, "html.parser")
        for field, selector in self.selectors.items():
            if field in {"transport", "equipment"}:
                values = [node.get_text(" ", strip=True) for node in soup.select(str(selector))]
                data[field] = [value for value in values if value]
                continue
            value = self._text(soup, str(selector))
            if not value:
                continue
            if field in {"rent", "management_fee"}:
                data[field] = self._number(value)
            elif field == "area":
                match = re.search(r"\d+(?:\.\d+)?", value)
                data[field] = float(match.group()) if match else None
            else:
                data[field] = value
        return self.candidate_from_data(data, url)


def load_configured_adapters(path: Path | None = None) -> list[ConfiguredAdapter]:
    config_path = path or (data_root() / "managed-sites.json")
    if not config_path.exists():
        return []
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        sites = payload.get("sites", []) if isinstance(payload, dict) else []
        return [ConfiguredAdapter(spec) for spec in sites if spec.get("enabled", True)]
    except (OSError, ValueError, KeyError, TypeError):
        return []

