from takuro_collector import __version__
from takuro_collector.wordpress import WordPressClient, WordPressError


class Resp:
    def __init__(self, status, data=None, text=''):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._data = data
        self.text = text
        self.content = text.encode('utf-8')

    def json(self):
        if self._data is None:
            raise ValueError('not json')
        return self._data


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def client_with(session):
    c = object.__new__(WordPressClient)
    c.site_url = 'https://example.test'
    c.key = 'a' * 64
    c.timeout = 20
    c.session = session
    return c


def test_release_version_026():
    assert tuple(map(int, __version__.split('.'))) >= (0, 2, 6)


def test_registration_v2_non_json_403_is_reported():
    s = Session([Resp(403, None, '<html>SiteGuard</html>')])
    c = client_with(s)
    try:
        c.send_candidates([{'building_name': 'A', 'room': '101'}])
    except WordPressError:
        pass
    else:
        raise AssertionError('expected WordPressError')
    assert len(s.calls) == 1
    assert s.calls[0][0].endswith('/wp-json/takuro-registration/v1/candidates')


def test_json_403_does_not_bypass_takuro_auth_with_fallback():
    s = Session([Resp(403, {'message': 'Collector 연동 키를 확인하세요.'})])
    c = client_with(s)
    try:
        c.send_candidates([{'building_name': 'A', 'room': '101'}])
    except WordPressError as e:
        assert 'HTTP 403' in str(e)
    else:
        raise AssertionError('expected WordPressError')
    assert len(s.calls) == 1


def test_normal_rest_success_does_not_call_fallback():
    s = Session([Resp(200, {'received': 1, 'results': [{'status': 'new', 'id': 1}]})])
    c = client_with(s)
    result = c.send_candidates([{'building_name': 'A', 'room': '101'}])
    assert result['received'] == 1
    assert len(s.calls) == 1


def test_registration_v2_does_not_fallback_to_legacy_intake():
    s = Session([Resp(403, None, '<html>blocked</html>')])
    c = client_with(s)
    try: c.send_candidates([{'building_name':'A','room':'101'}])
    except WordPressError: pass
    else: raise AssertionError('expected WordPressError')
    assert len(s.calls)==1
