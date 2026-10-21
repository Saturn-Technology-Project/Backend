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


def test_etherfi_cash_endpoint_is_approved():
    # ether.fi's read-only Cash connector (help.ether.fi, "AI connections").
    assert require_mcp_endpoint("https://mcp.ether.fi/mcp") == "https://mcp.ether.fi/mcp"
    with pytest.raises(HTTPException):
        require_mcp_endpoint("https://evil.ether.fi.example/mcp")
