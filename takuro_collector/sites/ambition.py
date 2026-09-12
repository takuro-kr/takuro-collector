import re

from bs4 import BeautifulSoup

from ..extractor import extract_generic
from ..models import PropertyCandidate
from ..utils import canonical_host
from .base import BaseAdapter


def _yen(value: str) -> int:
    match = re.search(r"([\d,]+)\s*円", value or "")
    return int(match.group(1).replace(",", "")) if match else 0


def _zero_or_text(value: str) -> str:
    text = (value or "").strip()
    return "0" if text in {"なし", "無し", "無", "0円", "-"} else text

class AmbitionAdapter(BaseAdapter):
    code = "AMB"
    label = "アンビション"
    domains = ("pm.am-bition.jp",)
    management_company = "アンビション"
    seed_urls = ("https://pm.am-bition.jp/rent/",)
    detail_patterns = (r"/rent/\d+/(\d+)(?:$|[/?#])",)
    force_browser = True
    login_expected = True

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

        # The first price-plan row is the site's primary advertised plan.
        # Generic extraction cannot associate these column headers with values.
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue
            headers = [cell.get_text(" ", strip=True) for cell in rows[0].find_all(["th", "td"])]
            if "賃料" not in headers or not any("管理費" in h or "共益費" in h for h in headers):
                continue
            values = [cell.get_text(" ", strip=True) for cell in rows[1].find_all(["th", "td"])]
            if len(values) != len(headers):
                continue
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
            transport=list(data.get("transport") or []),
            equipment=list(data.get("equipment") or []),
            photo_sources=list(data.get("photo_sources") or []),
            source_id_kind=source_id_kind,
            scrape_warnings=[],
        )
