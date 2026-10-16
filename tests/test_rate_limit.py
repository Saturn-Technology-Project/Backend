from app.core.rate_limit import allow, client_ip


def test_allow_blocks_after_the_limit():
    key = "test-register-limit"
    assert allow(key, limit=2, window=60)
    assert allow(key, limit=2, window=60)
    assert allow(key, limit=2, window=60) is False


def test_client_ip_uses_the_proxy_appended_address():
    class Request:
        headers = {"x-forwarded-for": "1.1.1.1, 203.0.113.8"}
        client = None

    assert client_ip(Request()) == "203.0.113.8"
