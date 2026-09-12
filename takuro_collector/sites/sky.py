from .base import BaseAdapter

class SKYAdapter(BaseAdapter):
    code = "SKY"
    label = "スカイコート"
    domains = ("skyc-chintai.jp",)
    management_company = "スカイコート"
    seed_urls = ("https://www.skyc-chintai.jp/",)
    detail_patterns = (r"/build-\d+/room-(\d+)\.html(?:$|[?#])",)
