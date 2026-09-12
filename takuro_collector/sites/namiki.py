from .base import BaseAdapter

class NamikiAdapter(BaseAdapter):
    code = "NAM"
    label = "ナミキ"
    domains = ("ref.namiki-grp.co.jp",)
    management_company = "ナミキ"
    seed_urls = ("https://ref.namiki-grp.co.jp/estate/",)
    detail_patterns = (r"/estate/building\d+/room(\d+)(?:$|[/?#])",)
