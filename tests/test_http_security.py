import pytest

DOC_PATHS = ("/docs", "/redoc", "/openapi.json")


async def test_api_docs_stay_available_for_local_development(client):
    opened = await client.get("/openapi.json")
    assert opened.status_code == 200
    assert opened.json()["openapi"]


@pytest.mark.parametrize("path", DOC_PATHS)
async def test_cloud_run_hides_api_docs(client, monkeypatch, path):
    monkeypatch.setenv("K_SERVICE", "saturn-backend")
    monkeypatch.setenv("ORIGIN_AUTH_SECRET", "expected-value")
    hidden = await client.get(path, headers={"X-Saturn-Origin-Auth": "expected-value"})
    assert hidden.status_code == 404
    assert hidden.json()["detail"] == "Not found"


async def test_expose_api_docs_false_hides_the_schema(client, monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.setenv("EXPOSE_API_DOCS", "false")
    hidden = await client.get("/openapi.json")
    assert hidden.status_code == 404


async def test_oauth_pages_use_a_script_nonce(client):
    response = await client.get("/oauth/authorize")
    policy = response.headers["content-security-policy"]
    assert "unsafe-inline" not in policy
    assert "'nonce-" in policy
    assert "nonce=" in response.text


async def test_security_headers_on_api_responses(client):
    response = await client.get("/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "camera=()" in response.headers["permissions-policy"]
    assert response.headers["content-security-policy"].startswith("default-src 'none'")
    assert "strict-transport-security" not in response.headers


async def test_hsts_is_sent_when_the_request_arrived_over_https(client):
    response = await client.get("/health", headers={"x-forwarded-proto": "https"})
    assert response.headers["strict-transport-security"] == "max-age=63072000; includeSubDomains"
