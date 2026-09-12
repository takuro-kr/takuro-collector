from .base import BaseAdapter

class GoodComAdapter(BaseAdapter):
    code = "GOODCOM"
    label = "GoodCom"
    domains = ("goodcomasset-gc.co.jp",)
    management_company = "GoodCom"
    seed_urls = ("https://www.goodcomasset-gc.co.jp/",)
    detail_patterns = (r"/bkndetail/\d+/room(\d+)/?(?:$|[?#])",)
