from .base import BaseAdapter

class StagePlanAdapter(BaseAdapter):
    code = "STAGEPLAN"
    label = "StagePlan"
    domains = ("stageplan.es-ws.jp",)
    management_company = "StagePlan"
    seed_urls = ("https://stageplan.es-ws.jp/es/rent/",)
    detail_patterns = (r"/es/rent/(\d+)(?:$|[/?#])",)
    force_browser = True
