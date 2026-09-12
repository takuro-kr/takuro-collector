from .amm import AMMAdapter
from .ambition import AmbitionAdapter
from .goodcom import GoodComAdapter
from .kinoshita import KinoshitaAdapter
from .namiki import NamikiAdapter
from .sky import SKYAdapter
from .stageplan import StagePlanAdapter
from .syla import SylaAdapter

ADAPTER_CLASSES = [
    AMMAdapter,
    SKYAdapter,
    GoodComAdapter,
    NamikiAdapter,
    AmbitionAdapter,
    KinoshitaAdapter,
    SylaAdapter,
    StagePlanAdapter,
]


def adapters():
    return [cls() for cls in ADAPTER_CLASSES]


def adapter_for_url(url: str):
    for adapter in adapters():
        if adapter.matches_url(url):
            return adapter
    return None
