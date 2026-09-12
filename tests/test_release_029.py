from takuro_collector import __version__
from takuro_collector.wordpress import WordPressClient


def test_release_version_029():
    assert tuple(map(int, __version__.split("."))) >= (0, 2, 9)


def test_wordpress_user_agent_tracks_app_version(monkeypatch):
    class Session:
        def __init__(self):
            self.headers = {}

    session = Session()
    monkeypatch.setattr("takuro_collector.wordpress.requests.Session", lambda: session)
    WordPressClient("https://example.test", "a" * 64)
    assert session.headers["User-Agent"] == f"TAKURO-Collector/{__version__}"
