from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.services.mcp_remote import McpUnauthorized


def _response(status_code: int, body: dict) -> httpx.Response:
    return httpx.Response(status_code, json=body)


@pytest.mark.asyncio
async def test_marketplace_connect_opens_provider_consent_and_stores_the_token(client, monkeypatch):
    async def listed(endpoint, authorization=""):
        assert endpoint == "https://mcp.example/mcp"
        if authorization != "access-1":
            raise McpUnauthorized(
                'Bearer resource_metadata="https://mcp.example/.well-known/oauth-protected-resource"',
                endpoint,
            )
        return [{"name": "repo.read", "description": "Read", "inputSchema": {"type": "object", "properties": {}}}]

    async def requested(method, url, **kwargs):
        if url == "https://mcp.example/.well-known/oauth-protected-resource":
            return _response(
                200,
                {
                    "resource": "https://mcp.example/mcp",
                    "authorization_servers": ["https://auth.example"],
                    "scopes_supported": ["mcp"],
                },
            )
        if url == "https://auth.example/.well-known/oauth-authorization-server":
            return _response(
                200,
                {
                    "authorization_endpoint": "https://auth.example/authorize",
                    "token_endpoint": "https://auth.example/token",
                    "registration_endpoint": "https://auth.example/register",
                },
            )
        if method == "POST" and url == "https://auth.example/register":
            assert kwargs["json"]["redirect_uris"] == ["http://test/mcp-servers/oauth/callback"]
            assert kwargs["json"]["token_endpoint_auth_method"] == "none"
            return _response(201, {"client_id": "saturn-client"})
        if method == "POST" and url == "https://auth.example/token":
            form = kwargs["data"]
            assert form["grant_type"] == "authorization_code"
            assert form["code"] == "code-1"
            assert form["client_id"] == "saturn-client"
            assert form["resource"] == "https://mcp.example/mcp"
            assert form["code_verifier"]
            return _response(200, {"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 3600})
        raise AssertionError(url)

    monkeypatch.setattr("app.services.mcp_oauth.list_remote_tools", listed)
    monkeypatch.setattr("app.services.mcp_connect.list_remote_tools", listed)
    monkeypatch.setattr("app.services.mcp_oauth._request", requested)

    started = await client.post(
        "/mcp-servers/oauth/start",
        json={"name": "GitHub", "endpoint": "https://mcp.example/mcp", "nonce": "nonce-12345678"},
    )
    assert started.status_code == 200, started.text
    authorize = urlparse(started.json()["authorization_url"])
    assert authorize.netloc == "auth.example"
    query = parse_qs(authorize.query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["scope"] == ["mcp"]
    assert query["resource"] == ["https://mcp.example/mcp"]

    page = await client.get("/mcp-servers/oauth/callback", params={"code": "code-1", "state": query["state"][0]})
    assert page.status_code == 200, page.text
    assert "saturn-mcp-oauth" in page.text
    assert "nonce-12345678" in page.text
    assert "Connected" in page.text

    servers = (await client.get("/mcp-servers")).json()
    github = next(item for item in servers if item["name"] == "GitHub")
    assert github["has_authorization"] is True
    assert github["tools"] == ["repo.read"]

    again = await client.get("/mcp-servers/oauth/callback", params={"code": "code-1", "state": query["state"][0]})
    assert "expired" in again.text


@pytest.mark.asyncio
async def test_marketplace_connects_without_a_popup_when_the_mcp_is_public(client, monkeypatch):
    async def listed(endpoint, authorization=""):
        return [{"name": "docs", "description": "Docs", "inputSchema": {"type": "object", "properties": {}}}]

    monkeypatch.setattr("app.services.mcp_oauth.list_remote_tools", listed)
    monkeypatch.setattr("app.services.mcp_connect.list_remote_tools", listed)

    started = await client.post(
        "/mcp-servers/oauth/start",
        json={"name": "Context7", "endpoint": "https://mcp.context7.com/mcp", "nonce": "nonce-12345678"},
    )
    assert started.status_code == 200, started.text
    body = started.json()
    assert body["authorization_url"] is None
    assert body["server"]["tools"] == ["docs"]
    assert body["server"]["has_authorization"] is False


@pytest.mark.asyncio
async def test_provider_denial_does_not_store_a_token(client, monkeypatch):
    async def listed(endpoint, authorization=""):
        raise McpUnauthorized('Bearer resource_metadata="https://mcp.example/.well-known/oauth-protected-resource"', endpoint)

    async def requested(method, url, **kwargs):
        if "oauth-protected-resource" in url:
            return _response(200, {"resource": "https://mcp.example/mcp", "authorization_servers": ["https://auth.example"]})
        if url.endswith("oauth-authorization-server"):
            return _response(
                200,
                {
                    "authorization_endpoint": "https://auth.example/authorize",
                    "token_endpoint": "https://auth.example/token",
                    "registration_endpoint": "https://auth.example/register",
                },
            )
        if url.endswith("/register"):
            return _response(201, {"client_id": "saturn-client"})
        raise AssertionError(url)

    monkeypatch.setattr("app.services.mcp_oauth.list_remote_tools", listed)
    monkeypatch.setattr("app.services.mcp_oauth._request", requested)

    started = await client.post(
        "/mcp-servers/oauth/start",
        json={"name": "Notion", "endpoint": "https://mcp.example/mcp", "nonce": "nonce-87654321"},
    )
    state = parse_qs(urlparse(started.json()["authorization_url"]).query)["state"][0]
    page = await client.get("/mcp-servers/oauth/callback", params={"error": "access_denied", "state": state})
    assert "did not grant" in page.text
    assert "nonce-87654321" in page.text
    names = [item["name"] for item in (await client.get("/mcp-servers")).json()]
    assert "Notion" not in names
