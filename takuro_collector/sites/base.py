from __future__ import annotations

import re
import html as html_lib
from dataclasses import dataclass, field
from hashlib import sha1
from typing import Iterable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..extractor import extract_generic
from ..fetcher import Fetcher, LoginRequired
from ..models import PropertyCandidate
from ..utils import ALLOWED_PREFECTURES, canonical_host, normalize_room, source_id_fallback


class ListingInactive(RuntimeError):
    """A listing disappeared after discovery and must be skipped normally."""


@dataclass
class DiscoveryResult:
    urls: list[str]
    via_browser: bool = False
    # Optional lightweight search-result facts keyed by detail URL.  Adapters may
    # populate this so WordPress duplicate checks can happen before expensive
    # detail-page fetches.
    hints: dict[str, dict] = field(default_factory=dict)
    listed_count: int = 0
    messages: list[str] = field(default_factory=list)
    # True only when the adapter can prove that this discovery contains the
    # complete current inventory for its supported region(s).  Only these
    # snapshots are eligible for outgoing-listing detection.
    inventory_complete: bool = False
    inventory_site: str = ""
    inventory_items: dict[str, dict] = field(default_factory=dict)


class BaseAdapter:
    code = "BASE"
    label = "Base"
    domains: tuple[str, ...] = ()
    management_company = ""
    seed_urls: tuple[str, ...] = ()
    detail_patterns: tuple[str, ...] = ()
    force_browser = False
    login_expected = False
    max_discovery_pages = 5

    def matches_url(self, url: str) -> bool:
        host = canonical_host(url)
        return any(host == d or host.endswith("." + d) for d in self.domains)

    def is_detail_url(self, url: str) -> bool:
        return any(re.search(p, url, re.I) for p in self.detail_patterns)

    def source_id(self, url: str) -> tuple[str, str]:
        for pat in self.detail_patterns:
            m = re.search(pat, url, re.I)
            if m:
                if m.groups():
                    parts = [g for g in m.groups() if g is not None]
                    if parts:
                        return "-".join(parts), "site"
        return source_id_fallback(url), "url_fallback"

    def discover(self, fetcher: Fetcher) -> DiscoveryResult:
        detail_urls: list[str] = []
        detail_seen: set[str] = set()
        queue = list(self.seed_urls)
        page_seen: set[str] = set()
        any_browser = False
        max_details = 100

        def add_detail(u: str) -> None:
            if u not in detail_seen and len(detail_urls) < max_details:
                detail_seen.add(u)
                detail_urls.append(u)

        for seed in self.seed_urls:
            if self.is_detail_url(seed):
                add_detail(seed)

        # First inspect the site's current root/search pages. Those links are more likely to
        # represent currently advertised rooms than a historical sitemap.
        while queue and len(page_seen) < self.max_discovery_pages and len(detail_urls) < max_details:
            page_url = queue.pop(0)
            if page_url in page_seen:
                continue
            page_seen.add(page_url)
            result = fetcher.fetch(
                page_url,
                self.code,
                force_browser=self.force_browser,
                login_expected=self.login_expected,
            )
            any_browser = any_browser or result.via_browser
            soup = BeautifulSoup(result.html, "html.parser")
            for a in soup.find_all("a", href=True):
                href = urljoin(result.url, str(a.get("href") or ""))
                if not self.matches_url(href):
                    continue
                if self.is_detail_url(href):
                    add_detail(href)
                    continue
                text = (a.get_text(" ", strip=True) or "").lower()
                path = (urlparse(href).path or "").lower()
                if len(queue) + len(page_seen) < self.max_discovery_pages and any(
                    x in (text + " " + path)
                    for x in ("賃貸", "空室", "物件", "search", "rent", "estate", "building", "bukken", "list")
                ):
                    if href not in page_seen and href not in queue:
                        queue.append(href)

        # If the current pages exposed few/no details, supplement with public sitemap URLs.
        # This never bypasses authentication and is capped to avoid crawling a whole history.
        if len(detail_urls) < 10:
            sitemap_queue: list[str] = []
            for domain in self.domains[:1]:
                sitemap_queue.extend([f"https://{domain}/sitemap.xml", f"https://{domain}/sitemap_index.xml"])
            sitemap_seen: set[str] = set()
            while sitemap_queue and len(sitemap_seen) < 8 and len(detail_urls) < max_details:
                sm = sitemap_queue.pop(0)
                if sm in sitemap_seen:
                    continue
                sitemap_seen.add(sm)
                try:
                    r = fetcher.session.get(sm, timeout=min(fetcher.timeout, 12), allow_redirects=True)
                    if not r.ok or not r.text:
                        continue
                    locs = [html_lib.unescape(x.strip()) for x in re.findall(r"<loc>\s*(.*?)\s*</loc>", r.text, re.I | re.S)]
                    for loc in locs:
                        if self.is_detail_url(loc):
                            add_detail(loc)
                        elif loc.lower().endswith('.xml') and len(sitemap_queue) < 20:
                            sitemap_queue.append(loc)
                except Exception:
                    pass
        return DiscoveryResult(detail_urls[:max_details], any_browser)

    def parse(self, html: str, url: str) -> PropertyCandidate:
        source_id, source_id_kind = self.source_id(url)
        data = extract_generic(
            html,
            url,
            management_company=self.management_company,
            source_property_id=source_id,
            source_id_kind=source_id_kind,
        )
        return self.candidate_from_data(data, url)

    def candidate_from_data(self, data: dict, url: str) -> PropertyCandidate:
        source_id, source_id_kind = self.source_id(url)
        building = str(data.get("building_name") or "").strip()
        room = normalize_room(str(data.get("room") or ""))
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
        )

    def collect_url(self, fetcher: Fetcher, url: str) -> PropertyCandidate:
        result = fetcher.fetch(
            url,
            self.code,
            force_browser=self.force_browser,
            login_expected=self.login_expected,
        )
        return self.parse(result.html, result.url)
