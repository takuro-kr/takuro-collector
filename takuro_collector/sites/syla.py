from .base import BaseAdapter

class SylaAdapter(BaseAdapter):
    code = "SYLA"
    label = "SYLA"
    domains = ("rent.syla.jp",)
    management_company = "SYLA"
    seed_urls = ("https://rent.syla.jp/",)
    detail_patterns = (r"/room(\d+)\.html(?:$|[?#])",)
