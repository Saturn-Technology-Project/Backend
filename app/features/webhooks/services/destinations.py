from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException, status

from app.features.mcp.servers.services import mcp_endpoint

_METADATA_HOSTS = frozenset(
    {
        "metadata.google.internal",
        "metadata.google.com",
        "metadata.internal",
    }
)

# Webhooks go to the standard HTTPS port only, so an endpoint cannot be used to probe other
# ports on a public host.
WEBHOOK_PORT = 443


class WebhookUnresolved(HTTPException):
    """The host name did not resolve just now. Unlike a refused address, this can pass on a retry."""

    def __init__(self) -> None:
        super().__init__(status.HTTP_400_BAD_REQUEST, "Webhook URL could not be resolved")


@dataclass(frozen=True)
class PinnedTarget:
    """Where a delivery connects: the checked IP, with the hostname kept for Host, SNI and the certificate."""

    url: str
    host: str
    address: str


def _split(url: str):
    raw = (url or "").strip()
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme != "https" or not host or parts.username or parts.password:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Webhook URL must be HTTPS without embedded credentials",
        )
    try:
        port = parts.port
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Webhook URL port is not valid") from None
    if port not in (None, WEBHOOK_PORT):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Webhook URL must use port 443")
    if host in _METADATA_HOSTS or host == "localhost" or host.endswith(".localhost") or host.endswith(".internal"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Webhook URL host is not allowed")
    return raw, parts, host


def _allowed_addresses(host: str) -> list[str]:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if mcp_endpoint.address_blocked(host):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Webhook URL address is not allowed")
        return [host]
    try:
        found = mcp_endpoint.addresses_for(host, WEBHOOK_PORT)
    except HTTPException:
        raise WebhookUnresolved() from None
    if not found or any(mcp_endpoint.address_blocked(item) for item in found):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Webhook URL address is not allowed")
    return found


def require_webhook_url(url: str) -> str:
    """Check the URL and resolve its host. This blocks on DNS: call it through asyncio.to_thread."""
    raw, _parts, host = _split(url)
    _allowed_addresses(host)
    return raw


def require_webhook_shape(url: str) -> str:
    """The checks that need no network: scheme, port, credentials and refused host names."""
    raw, _parts, _host = _split(url)
    return raw


def pin_webhook_url(url: str) -> PinnedTarget:
    """Resolve and check the destination once, and return a URL that connects to that exact IP.

    The connection must go to the address that was checked. Resolving the name again when
    connecting would let a DNS answer change between the check and the request.
    """
    _raw, parts, host = _split(url)
    address = _allowed_addresses(host)[0]
    netloc = f"[{address}]" if ":" in address else address
    pinned = urlunsplit(("https", netloc, parts.path or "/", parts.query, ""))
    return PinnedTarget(url=pinned, host=host, address=address)
