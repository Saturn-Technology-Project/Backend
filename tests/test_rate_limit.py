import pytest

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


def test_client_ip_prefers_cf_connecting_ip():
    class CfRequest:
        headers = {
            "cf-connecting-ip": "198.51.100.42",
            "x-forwarded-for": "1.1.1.1, 203.0.113.8",
        }
        client = None

    assert client_ip(CfRequest()) == "198.51.100.42"


def test_client_ip_uses_the_proxy_appended_address_when_no_cf_header():
    class Request:
        headers = {"x-forwarded-for": "1.1.1.1, 203.0.113.8"}
        client = None

    assert client_ip(Request()) == "203.0.113.8"


@pytest.mark.asyncio
async def test_redis_outage_rejects_login_and_registration_without_memory_fallback(client, monkeypatch):
    from app.core import rate_limit

    class DownRedis:
        def incr(self, _key: str) -> int:
            raise ConnectionError("redis unreachable")

    monkeypatch.setattr(rate_limit, "_use_redis", lambda: True)
    monkeypatch.setattr(rate_limit, "_redis_client", lambda: DownRedis())

    assert rate_limit.allow("any-key") is False

    login_resp = await client.post(
        "/auth/login",
        json={"email": "rick@saturn.ai", "password": "saturn"},
    )
    assert login_resp.status_code == 429

    migration_resp = await client.post(
        "/auth/login/migration",
        json={"email": "rick@saturn.ai", "password": "saturn"},
    )
    assert migration_resp.status_code == 429

    reg_code_resp = await client.post(
        "/auth/register/code",
        json={"name": "Alice", "email": "alice@example.com", "password": "password123"},
    )
    assert reg_code_resp.status_code == 429

    oauth_reg_resp = await client.post(
        "/oauth/register",
        json={"redirect_uris": ["http://127.0.0.1:8765/callback"]},
    )
    assert oauth_reg_resp.status_code == 429
