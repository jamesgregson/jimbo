import urllib.request

import shop.http


class Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b"ok"


def test_timeouts(monkeypatch):
    seen = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: seen.append(timeout) or Response())
    assert shop.http.fetch("http://x") == b"ok"
    assert shop.http.upload("http://x", b"d") == 200
    assert seen == [30, 10]
