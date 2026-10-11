from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.features.mcp.servers.services.mcp_remote import McpUnauthorized, list_remote_tools


def _response(status_code: int, body: dict) -> httpx.Response:
    return httpx.Response(status_code, json=body)


@pytest.mark.asyncio
async def test_marketplace_connect_opens_provider_consent_and_stores_the_token(client, monkeypatch):
    monkeypatch.delenv("PUBLIC_WS_URL", raising=False)
    async def listed(endpoint, authorization=""):
        assert endpoint == "https://mcp.notion.com/mcp"
        if authorization != "access-1":
            raise McpUnauthorized(
                'Bearer resource_metadata="https://mcp.notion.com/.well-known/oauth-protected-resource"',
                endpoint,
            )
        return [{"name": "repo.read", "description": "Read", "inputSchema": {"type": "object", "properties": {}}}]

    async def requested(method, url, **kwargs):
        if url == "https://mcp.notion.com/.well-known/oauth-protected-resource":
            return _response(
                200,
                {
                    "resource": "https://mcp.notion.com/mcp",
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
            assert form["resource"] == "https://mcp.notion.com/mcp"
            assert form["code_verifier"]
            return _response(200, {"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 3600})
        raise AssertionError(url)

    monkeypatch.setattr("app.features.mcp.oauth.services.mcp_oauth.list_remote_tools", listed)
    monkeypatch.setattr("app.features.mcp.servers.services.mcp_connect.list_remote_tools", listed)
    monkeypatch.setattr("app.features.mcp.oauth.services.mcp_oauth._request", requested)

    started = await client.post(
        "/mcp-servers/oauth/start",
        json={"name": "GitHub", "endpoint": "https://mcp.notion.com/mcp", "nonce": "nonce-12345678"},
    )
    assert started.status_code == 200, started.text
    authorize = urlparse(started.json()["authorization_url"])
    assert authorize.netloc == "auth.example"
    query = parse_qs(authorize.query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["scope"] == ["mcp"]
    assert query["resource"] == ["https://mcp.notion.com/mcp"]

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

    monkeypatch.setattr("app.features.mcp.oauth.services.mcp_oauth.list_remote_tools", listed)
    monkeypatch.setattr("app.features.mcp.servers.services.mcp_connect.list_remote_tools", listed)

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
        raise McpUnauthorized('Bearer resource_metadata="https://mcp.notion.com/.well-known/oauth-protected-resource"', endpoint)

    async def requested(method, url, **kwargs):
        if "oauth-protected-resource" in url:
            return _response(200, {"resource": "https://mcp.notion.com/mcp", "authorization_servers": ["https://auth.example"]})
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

    monkeypatch.setattr("app.features.mcp.oauth.services.mcp_oauth.list_remote_tools", listed)
    monkeypatch.setattr("app.features.mcp.oauth.services.mcp_oauth._request", requested)

    started = await client.post(
        "/mcp-servers/oauth/start",
        json={"name": "Notion", "endpoint": "https://mcp.notion.com/mcp", "nonce": "nonce-87654321"},
    )
    state = parse_qs(urlparse(started.json()["authorization_url"]).query)["state"][0]
    page = await client.get("/mcp-servers/oauth/callback", params={"error": "access_denied", "state": state})
    assert "did not grant" in page.text
    assert "nonce-87654321" in page.text
    names = [item["name"] for item in (await client.get("/mcp-servers")).json()]
    assert "Notion" not in names


@pytest.mark.asyncio
async def test_github_401_asks_for_sign_in_instead_of_failing(monkeypatch):
    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            return httpx.Response(
                401,
                headers={
                    "www-authenticate": 'Bearer resource_metadata="https://api.githubcopilot.com/.well-known/oauth-protected-resource/mcp/"'
                },
                text="bad request: missing required Authorization header",
            )

    monkeypatch.setattr("app.features.mcp.servers.services.mcp_remote.httpx.AsyncClient", Client)
    with pytest.raises(McpUnauthorized) as caught:
        await list_remote_tools("https://api.githubcopilot.com/mcp/", "")
    assert "oauth-protected-resource/mcp/" in caught.value.www_authenticate


@pytest.mark.asyncio
async def test_github_sign_in_uses_the_configured_oauth_app(client, monkeypatch):
    monkeypatch.setenv("SATURN_GITHUB_OAUTH_CLIENT_ID", "Iv1.saturn")
    monkeypatch.setenv("SATURN_GITHUB_OAUTH_CLIENT_SECRET", "secret-value")
    monkeypatch.setenv("PUBLIC_WS_URL", "https://saturn-backend-clcae2l2mq-de.a.run.app")

    async def listed(endpoint, authorization=""):
        raise McpUnauthorized(
            'Bearer resource_metadata="https://api.githubcopilot.com/.well-known/oauth-protected-resource/mcp/"',
            endpoint,
        )

    async def requested(method, url, **kwargs):
        if url == "https://api.githubcopilot.com/.well-known/oauth-protected-resource/mcp/":
            return _response(
                200,
                {
                    "resource": "https://api.githubcopilot.com/mcp/",
                    "authorization_servers": ["https://github.com/login/oauth"],
                    "scopes_supported": ["repo", "read:user"],
                },
            )
        if url == "https://github.com/.well-known/oauth-authorization-server/login/oauth":
            return _response(
                200,
                {
                    "authorization_endpoint": "https://github.com/login/oauth/authorize",
                    "token_endpoint": "https://github.com/login/oauth/access_token",
                },
            )
        raise AssertionError(url)

    monkeypatch.setattr("app.features.mcp.oauth.services.mcp_oauth.list_remote_tools", listed)
    monkeypatch.setattr("app.features.mcp.oauth.services.mcp_oauth._request", requested)
    started = await client.post(
        "/mcp-servers/oauth/start",
        json={"name": "GitHub", "endpoint": "https://api.githubcopilot.com/mcp/", "nonce": "nonce-github1"},
    )
    assert started.status_code == 200, started.text
    authorize = urlparse(started.json()["authorization_url"])
    assert authorize.netloc == "github.com"
    query = parse_qs(authorize.query)
    assert query["client_id"] == ["Iv1.saturn"]
    assert "client_secret" not in query
    assert query["scope"] == ["repo read:user"]
    assert query["redirect_uri"] == ["https://saturn-backend-clcae2l2mq-de.a.run.app/mcp-servers/oauth/callback"]


@pytest.mark.asyncio
async def test_github_without_an_oauth_app_asks_for_a_pasted_token(client, monkeypatch):
    monkeypatch.delenv("SATURN_GITHUB_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("SATURN_GITHUB_OAUTH_CLIENT_SECRET", raising=False)

    async def listed(endpoint, authorization=""):
        raise McpUnauthorized(
            'Bearer resource_metadata="https://api.githubcopilot.com/.well-known/oauth-protected-resource/mcp/"',
            endpoint,
        )

    async def requested(method, url, **kwargs):
        if "oauth-protected-resource" in url:
            return _response(
                200,
                {
                    "resource": "https://api.githubcopilot.com/mcp/",
                    "authorization_servers": ["https://github.com/login/oauth"],
                },
            )
        if "oauth-authorization-server" in url:
            return _response(
                200,
                {
                    "authorization_endpoint": "https://github.com/login/oauth/authorize",
                    "token_endpoint": "https://github.com/login/oauth/access_token",
                },
            )
        raise AssertionError(url)

    monkeypatch.setattr("app.features.mcp.oauth.services.mcp_oauth.list_remote_tools", listed)
    monkeypatch.setattr("app.features.mcp.oauth.services.mcp_oauth._request", requested)
    started = await client.post(
        "/mcp-servers/oauth/start",
        json={"name": "GitHub", "endpoint": "https://api.githubcopilot.com/mcp/", "nonce": "nonce-github2"},
    )
    assert started.status_code == 400
    assert started.json()["detail"] == "This MCP server does not let Saturn start sign-in"


async def _google_metadata(method, url, **kwargs):
    if url == "https://gmailmcp.googleapis.com/.well-known/oauth-protected-resource/mcp/v1":
        return _response(
            200,
            {
                "resource": "https://gmailmcp.googleapis.com/mcp/v1",
                "authorization_servers": ["https://accounts.google.com/"],
                "scopes_supported": [
                    "https://mail.google.com/",
                    "https://www.googleapis.com/auth/gmail.compose",
                    "https://www.googleapis.com/auth/gmail.readonly",
                ],
            },
        )
    if url == "https://accounts.google.com/.well-known/oauth-authorization-server":
        return _response(404, {})
    if url == "https://accounts.google.com/.well-known/openid-configuration":
        return _response(
            200,
            {
                "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
                "token_endpoint": "https://oauth2.googleapis.com/token",
            },
        )
    raise AssertionError(url)


@pytest.mark.asyncio
async def test_google_sign_in_uses_the_configured_oauth_client(client, monkeypatch):
    monkeypatch.setenv("SATURN_GOOGLE_OAUTH_CLIENT_ID", "saturn.apps.googleusercontent.com")
    monkeypatch.setenv("SATURN_GOOGLE_OAUTH_CLIENT_SECRET", "google-secret")
    monkeypatch.setenv("PUBLIC_WS_URL", "https://saturn-backend-629072855899.asia-east1.run.app")

    async def listed(endpoint, authorization=""):
        assert authorization == ""
        return [{"name": "search_threads", "description": "Search", "inputSchema": {"type": "object", "properties": {}}}]

    monkeypatch.setattr("app.features.mcp.oauth.services.mcp_oauth.list_remote_tools", listed)
    monkeypatch.setattr("app.features.mcp.oauth.services.mcp_oauth._request", _google_metadata)
    started = await client.post(
        "/mcp-servers/oauth/start",
        json={"name": "Gmail", "endpoint": "https://gmailmcp.googleapis.com/mcp/v1", "nonce": "nonce-google1"},
    )
    assert started.status_code == 200, started.text
    authorize = urlparse(started.json()["authorization_url"])
    assert authorize.netloc == "accounts.google.com"
    query = parse_qs(authorize.query)
    assert query["client_id"] == ["saturn.apps.googleusercontent.com"]
    assert "client_secret" not in query
    assert query["scope"] == [
        "https://www.googleapis.com/auth/gmail.compose https://www.googleapis.com/auth/gmail.readonly"
    ]
    assert query["access_type"] == ["offline"]
    assert query["redirect_uri"] == [
        "https://saturn-backend-629072855899.asia-east1.run.app/mcp-servers/oauth/callback"
    ]


@pytest.mark.asyncio
async def test_google_without_an_oauth_client_asks_for_a_pasted_token(client, monkeypatch):
    monkeypatch.delenv("SATURN_GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("SATURN_GOOGLE_OAUTH_CLIENT_SECRET", raising=False)

    async def listed(endpoint, authorization=""):
        return [{"name": "search_threads", "description": "Search", "inputSchema": {"type": "object", "properties": {}}}]

    monkeypatch.setattr("app.features.mcp.oauth.services.mcp_oauth.list_remote_tools", listed)
    monkeypatch.setattr("app.features.mcp.oauth.services.mcp_oauth._request", _google_metadata)
    started = await client.post(
        "/mcp-servers/oauth/start",
        json={"name": "Gmail", "endpoint": "https://gmailmcp.googleapis.com/mcp/v1", "nonce": "nonce-google2"},
    )
    assert started.status_code == 400
    assert started.json()["detail"] == "This MCP server does not let Saturn start sign-in"
