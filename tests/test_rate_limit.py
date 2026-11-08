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

        def eval(self, _script: str, _numkeys: int, key: str, window: int) -> int:
            # The limiter increments and sets the TTL in one atomic script.
            assert window == 60
            return self.incr(key)

    fake = Fake()
    monkeypatch.setattr(rate_limit, "_use_redis", lambda: True)
    monkeypatch.setattr(rate_limit, "_redis_client", lambda: fake)
    assert rate_limit.allow("shared", limit=1, window=60)
    assert rate_limit.allow("shared", limit=1, window=60) is False
    assert fake.values["saturn:rate:shared"] == 2


def test_client_ip_prefers_cf_connecting_ip(monkeypatch):
    # Behind Cloudflare every request carries the origin secret, so CF-Connecting-IP is trustworthy.
    monkeypatch.setenv("ORIGIN_AUTH_SECRET", "expected-value")

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

        def eval(self, *_args) -> int:
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


def test_client_ip_ignores_cf_header_without_cloudflare_origin_auth(monkeypatch):
    # Without the origin secret anyone can reach the app directly and choose CF-Connecting-IP.
    monkeypatch.delenv("ORIGIN_AUTH_SECRET", raising=False)

    class Request:
        headers = {"cf-connecting-ip": "6.6.6.6", "x-forwarded-for": "1.1.1.1, 203.0.113.8"}
        client = None

    assert client_ip(Request()) == "203.0.113.8"


def test_throttle_uses_the_same_client_ip_as_rate_limits(monkeypatch):
    from app.features.identity.auth.services import throttle

    monkeypatch.delenv("ORIGIN_AUTH_SECRET", raising=False)

    class Request:
        headers = {"x-forwarded-for": "9.9.9.9, 203.0.113.8"}
        client = None

    # The first X-Forwarded-For entry is whatever the client sent; it must never pick the bucket.
    assert throttle.client_ip(Request()) == "203.0.113.8"


def test_login_is_limited_per_account_across_addresses():
    from app.core import rate_limit

    class Request:
        def __init__(self, ip: str) -> None:
            self.headers = {"x-forwarded-for": ip}
            self.client = None

    results = [rate_limit.login_allowed(Request(f"10.0.0.{n}"), "victim@example.com") for n in range(12)]
    assert results[:10] == [True] * 10
    assert results[10:] == [False, False]


def test_client_ip_trusts_the_console_address_only_with_the_console_secret(monkeypatch):
    # Console requests reach the API from the console's address, so CF-Connecting-IP names the
    # console. Its own report of the browser's address must win, but only with its secret.
    monkeypatch.setenv("ORIGIN_AUTH_SECRET", "edge")
    monkeypatch.setenv("CONSOLE_PROXY_SECRET", "console-secret")

    class Request:
        def __init__(self, headers: dict) -> None:
            self.headers = {"cf-connecting-ip": "34.1.1.1", **headers}
            self.client = None

    assert client_ip(Request({"x-saturn-console-auth": "console-secret", "x-saturn-client-ip": "198.51.100.9"})) == "198.51.100.9"
    # Wrong or missing secret: the header is a client's own claim and is ignored.
    assert client_ip(Request({"x-saturn-console-auth": "guess", "x-saturn-client-ip": "198.51.100.9"})) == "34.1.1.1"
    assert client_ip(Request({"x-saturn-client-ip": "198.51.100.9"})) == "34.1.1.1"
    # Not an address: fall back rather than key a bucket on arbitrary text.
    assert client_ip(Request({"x-saturn-console-auth": "console-secret", "x-saturn-client-ip": "x"})) == "34.1.1.1"
    monkeypatch.setenv("CONSOLE_PROXY_SECRET_PREVIOUS", "old-secret")
    assert client_ip(Request({"x-saturn-console-auth": "old-secret", "x-saturn-client-ip": "198.51.100.10"})) == "198.51.100.10"
    monkeypatch.delenv("CONSOLE_PROXY_SECRET")
    monkeypatch.delenv("CONSOLE_PROXY_SECRET_PREVIOUS")
    assert client_ip(Request({"x-saturn-console-auth": "", "x-saturn-client-ip": "198.51.100.9"})) == "34.1.1.1"


def test_console_users_do_not_share_one_login_bucket(monkeypatch):
    from app.core import rate_limit

    monkeypatch.setenv("ORIGIN_AUTH_SECRET", "edge")
    monkeypatch.setenv("CONSOLE_PROXY_SECRET", "console-secret")

    class Request:
        def __init__(self, ip: str) -> None:
            self.headers = {
                "cf-connecting-ip": "34.1.1.1",
                "x-saturn-console-auth": "console-secret",
                "x-saturn-client-ip": ip,
            }
            self.client = None

    attacker = [rate_limit.login_allowed(Request("203.0.113.66")) for _ in range(25)]
    assert attacker[-1] is False
    # Another browser behind the same console is not locked out.
    assert rate_limit.login_allowed(Request("198.51.100.20")) is True
