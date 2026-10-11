from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from fastapi import HTTPException, status

from app.features.mcp.servers.services import mcp_endpoint

_METADATA_HOSTS = frozenset(
    {
        "metadata.google.internal",
        "metadata.google.com",
        "metadata.internal",
    }
)


def require_webhook_url(url: str) -> str:
    raw = (url or "").strip()
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme != "https" or not host or parts.username or parts.password:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Webhook URL must be HTTPS without embedded credentials",
        )
    if host in _METADATA_HOSTS or host == "localhost" or host.endswith(".localhost") or host.endswith(".internal"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Webhook URL host is not allowed")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if mcp_endpoint.address_blocked(host):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Webhook URL address is not allowed")
        return raw
    port = parts.port or 443
    try:
        found = mcp_endpoint.addresses_for(host, port)
    except HTTPException:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Webhook URL could not be resolved") from None
    if not found or any(mcp_endpoint.address_blocked(item) for item in found):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Webhook URL address is not allowed")
    return raw
