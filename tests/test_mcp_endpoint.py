import pytest
from fastapi import HTTPException

from app.features.mcp.servers.services.mcp_endpoint import address_blocked, require_mcp_endpoint


def _status(url: str) -> int:
    try:
        require_mcp_endpoint(url)
    except HTTPException as exc:
        return exc.status_code
    return 200


def test_private_and_metadata_addresses_are_blocked():
    assert address_blocked("127.0.0.1")
    assert address_blocked("10.1.2.3")
    assert address_blocked("172.16.0.1")
    assert address_blocked("192.168.1.1")
    assert address_blocked("169.254.169.254")
    assert address_blocked("100.64.0.1")
    assert address_blocked("::1")
    assert address_blocked("fe80::1")
    assert address_blocked("::ffff:127.0.0.1")
    assert address_blocked("not-an-ip")
    assert not address_blocked("1.1.1.1")


def test_mcp_endpoint_requires_an_approved_public_https_host(monkeypatch):
    assert _status("http://mcp.notion.com/mcp") == 400
    assert _status("https://user:pass@mcp.notion.com/mcp") == 400
    assert _status("https://metadata.google.internal/") == 400
    assert _status("https://169.254.169.254/") == 400
    assert _status("https://evil.example/mcp") == 400

    monkeypatch.setattr(
        "app.features.mcp.servers.services.mcp_endpoint.addresses_for",
        lambda _host, _port: ["10.0.0.1"],
    )
    assert _status("https://mcp.notion.com/mcp") == 400
    monkeypatch.setattr(
        "app.features.mcp.servers.services.mcp_endpoint.addresses_for",
        lambda _host, _port: ["1.1.1.1", "127.0.0.1"],
    )
    assert _status("https://mcp.notion.com/mcp") == 400
    monkeypatch.setattr(
        "app.features.mcp.servers.services.mcp_endpoint.addresses_for",
        lambda _host, _port: ["1.1.1.1"],
    )
    assert require_mcp_endpoint("https://mcp.notion.com/mcp") == "https://mcp.notion.com/mcp"


@pytest.mark.parametrize("endpoint", ["https://mcp.ether.fi/mcp", "https://agents.coinbase.com/mcp"])
def test_finance_marketplace_endpoints_still_reject_private_dns(endpoint, monkeypatch):
    assert require_mcp_endpoint(endpoint) == endpoint
    monkeypatch.setattr(
        "app.features.mcp.servers.services.mcp_endpoint.addresses_for",
        lambda _host, _port: ["1.1.1.1", "127.0.0.1"],
    )
    assert _status(endpoint) == 400


@pytest.mark.asyncio
async def test_changing_the_endpoint_does_not_forward_the_saved_token(client, monkeypatch):
    seen: list[tuple[str, str]] = []

    async def listed(endpoint, authorization=""):
        seen.append((endpoint, authorization))
        return [{"name": "repo.read", "description": "Read", "inputSchema": {"type": "object", "properties": {}}}]

    monkeypatch.setattr("app.features.mcp.servers.services.mcp_connect.list_remote_tools", listed)
    first = "https://api.githubcopilot.com/mcp/"
    second = "https://mcp.notion.com/mcp"
    created = await client.post(
        "/mcp-servers",
        json={"name": "GitHub", "endpoint": first, "authorization": "token-1"},
    )
    assert created.status_code == 201, created.text
    reused = await client.post("/mcp-servers", json={"name": "GitHub", "endpoint": first})
    assert reused.status_code == 201, reused.text
    moved = await client.post("/mcp-servers", json={"name": "GitHub", "endpoint": second})
    assert moved.status_code == 201, moved.text
    assert seen == [(first, "token-1"), (first, "token-1"), (second, "")]

    monkeypatch.setattr(
        "app.features.mcp.servers.services.mcp_endpoint.addresses_for",
        lambda _host, _port: ["127.0.0.1"],
    )
    blocked = await client.post(
        "/mcp-servers",
        json={"name": "GitHub", "endpoint": "https://mcp.linear.app/mcp", "authorization": "token-2"},
    )
    assert blocked.status_code == 400
    assert len(seen) == 3


def test_endpoint_must_use_port_443():
    from app.features.mcp.servers.services.mcp_endpoint import require_mcp_endpoint

    assert require_mcp_endpoint("https://mcp.notion.com:443/mcp") == "https://mcp.notion.com:443/mcp"
    for endpoint in ["https://mcp.notion.com:8443/mcp", "https://mcp.notion.com:80/mcp", "https://mcp.notion.com:99999/mcp"]:
        with pytest.raises(HTTPException):
            require_mcp_endpoint(endpoint)


@pytest.mark.asyncio
async def test_a_key_in_the_endpoint_url_moves_to_the_vault_and_is_never_shown(client, monkeypatch):
    seen: list[tuple[str, str]] = []

    async def listed(endpoint, authorization=""):
        seen.append((endpoint, authorization))
        return [{"name": "filings.search", "description": "Search", "inputSchema": {"type": "object", "properties": {}}}]

    monkeypatch.setattr("app.features.mcp.servers.services.mcp_connect.list_remote_tools", listed)
    created = await client.post(
        "/mcp-servers",
        json={"name": "SEC", "endpoint": "https://api.sec-api.io/mcp?apiKey=sk-live-1234567890&region=us"},
    )
    assert created.status_code == 201, created.text
    assert seen == [("https://api.sec-api.io/mcp?region=us", "sk-live-1234567890")]
    assert "sk-live" not in created.text
    assert created.json()["has_authorization"] is True
    listed_servers = await client.get("/mcp-servers")
    assert "sk-live" not in listed_servers.text
    tools = await client.get("/tools")
    assert "sk-live" not in tools.text

    conflicting = await client.post(
        "/mcp-servers",
        json={"name": "SEC2", "endpoint": "https://api.sec-api.io/mcp?apiKey=one", "authorization": "two"},
    )
    assert conflicting.status_code == 400
    started = await client.post(
        "/mcp-servers/oauth/start",
        json={"name": "SEC3", "endpoint": "https://api.sec-api.io/mcp?token=abc123456", "nonce": "nonce-12345678"},
    )
    assert started.status_code == 400


def test_endpoints_stored_with_a_key_are_redacted_when_read():
    from app.schemas import McpServerRead, ToolRead

    tool = ToolRead(
        id="t", name="n", description="", type="mcp", mcp_endpoint="https://api.sec-api.io/mcp?apiKey=sk-live-1234567890",
        permissions=[], status="active",
    )
    assert "sk-live" not in tool.mcp_endpoint
    server = McpServerRead(id="s", name="n", endpoint="https://api.sec-api.io/mcp?apiKey=sk-live-1234567890", status="active")
    assert "sk-live" not in server.endpoint


@pytest.mark.asyncio
async def test_mcp_requests_connect_to_the_checked_address(monkeypatch):
    import httpx

    from app.features.mcp.servers.services import mcp_remote

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers.get("host"), request.extensions.get("sni_hostname")))
        body = __import__("json").loads(request.content)
        if body["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "t"}]}})

    real = httpx.AsyncClient

    def build(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr("app.features.mcp.servers.services.mcp_remote.httpx.AsyncClient", build)
    monkeypatch.setattr("app.features.mcp.servers.services.mcp_endpoint.addresses_for", lambda _h, _p: ["1.1.1.1"])
    await mcp_remote.list_remote_tools("https://mcp.notion.com/mcp", "token")
    assert seen == [("1.1.1.1", "mcp.notion.com", "mcp.notion.com")] * 2

    # A name that now resolves to an internal address is refused before any request is sent.
    monkeypatch.setattr("app.features.mcp.servers.services.mcp_endpoint.addresses_for", lambda _h, _p: ["10.0.0.5"])
    with pytest.raises(HTTPException):
        await mcp_remote.list_remote_tools("https://mcp.notion.com/mcp", "token")
    assert len(seen) == 2
