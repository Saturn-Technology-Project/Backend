from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import HTTPException, status

# Marketplace MCP hosts. A connection must use HTTPS and one of these names.
ALLOWED_MCP_HOSTS = frozenset(
    {
        "gmailmcp.googleapis.com",
        "calendarmcp.googleapis.com",
        "mcp.mercury.com",
        "mcp.ether.fi",
        "agents.coinbase.com",
        "api.ibkr.com",
        "mcp.dukascopy.com",
        "mcp.app.carta.com",
        "mcp.twelvedata.com",
        "mcp.factset.com",
        "mcp.financialdatasets.ai",
        "api.sec-api.io",
        "finance.foundation",
        "mcp.ramp.com",
        "api.brex.com",
        "mcp.xero.com",
        "seekingalpha.com",
        "api.githubcopilot.com",
        "mcp.notion.com",
        "mcp.linear.app",
        "mcp.stripe.com",
        "mcp.atlassian.com",
        "mcp.sentry.dev",
        "mcp.context7.com",
        "mcp.deepwiki.com",
        "huggingface.co",
        "mcp.neon.tech",
        "mcp.supabase.com",
    }
)

_METADATA_HOSTS = frozenset({"metadata.google.internal", "metadata.google.com"})
_BLOCKED_V4 = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("240.0.0.0/4"),
)


def addresses_for(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "MCP endpoint could not be resolved") from exc
    return [item[4][0] for item in infos]


def address_blocked(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return True
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        return True
    return address.version == 4 and any(address in network for network in _BLOCKED_V4)


def require_mcp_endpoint(url: str) -> str:
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme != "https" or not host or parts.username or parts.password:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "MCP endpoint must be HTTPS")
    try:
        ipaddress.ip_address(host)
        literal_ip = True
    except ValueError:
        literal_ip = False
    if literal_ip or host in _METADATA_HOSTS or host not in ALLOWED_MCP_HOSTS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "MCP endpoint host is not approved")
    try:
        port = parts.port or 443
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "MCP endpoint must be HTTPS") from exc
    if port != 443:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "MCP endpoint must use port 443")
    found = addresses_for(host, port)
    if not found or any(address_blocked(item) for item in found):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "MCP endpoint address is not allowed")
    return url.strip()


# Query parameter names that carry a credential, compared without case, "-" or "_".
_CREDENTIAL_PARAMS = frozenset(
    {
        "apikey",
        "key",
        "token",
        "accesstoken",
        "apitoken",
        "authtoken",
        "auth",
        "authorization",
        "secret",
        "clientsecret",
        "password",
        "accesskey",
        "signature",
        "sig",
    }
)


def _param_name(name: str) -> str:
    return re.sub(r"[-_]", "", name).lower()


def split_url_credential(url: str) -> tuple[str, str]:
    """The URL without credential query parameters, and the credential it carried ("" if none).

    An endpoint is stored and shown to every member who can see connections, so a key never stays
    in it. A URL that carries more than one distinct credential is refused.
    """
    parts = urlsplit(url.strip())
    if not parts.query:
        return url.strip(), ""
    kept: list[tuple[str, str]] = []
    found: set[str] = set()
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        if _param_name(name) in _CREDENTIAL_PARAMS:
            if value:
                found.add(value)
            continue
        kept.append((name, value))
    if len(found) > 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Put the API key in the authorization field, not the URL")
    clean = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))
    return clean, next(iter(found), "")
