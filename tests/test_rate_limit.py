from app.core.rate_limit import allow, client_ip


def test_allow_blocks_after_the_limit():
    key = "test-register-limit"
    assert allow(key, limit=2, window=60)
    assert allow(key, limit=2, window=60)
    assert allow(key, limit=2, window=60) is False


def test_shared_limit_uses_one_redis_counter(monkeypatch):
    from app.core import rate_limit

    class Fake:
        def __init__(self) -> None:
            self.values: dict[str, int] = {}

        def incr(self, key: str) -> int:
            self.values[key] = self.values.get(key, 0) + 1
            return self.values[key]

        def expire(self, key: str, window: int) -> bool:
            return True

    fake = Fake()
    monkeypatch.setattr(rate_limit, "_use_redis", lambda: True)
    monkeypatch.setattr(rate_limit, "_redis_client", lambda: fake)
    assert rate_limit.allow("shared", limit=1, window=60)
    assert rate_limit.allow("shared", limit=1, window=60) is False
    assert fake.values["saturn:rate:shared"] == 2


def test_client_ip_uses_the_proxy_appended_address():
    class Request:
        headers = {"x-forwarded-for": "1.1.1.1, 203.0.113.8"}
        client = None

    assert client_ip(Request()) == "203.0.113.8"
