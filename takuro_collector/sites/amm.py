from .base import BaseAdapter

class AMMAdapter(BaseAdapter):
    code = "AMM"
    label = "アムス / otoku-chintai"
    domains = ("otoku-chintai.com",)
    management_company = "アムス"
    seed_urls = ("https://www.otoku-chintai.com/",)
    detail_patterns = (r"/room(\d+)\.html(?:$|[?#])",)
