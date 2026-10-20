"""The public origin of this API.

Production sets PUBLIC_WS_URL. Links, OAuth metadata and callback URLs must come from it,
not from the Host or X-Forwarded-* headers, which a client can choose.
"""

from __future__ import annotations

import os

from starlette.requests import Request


def configured_origin() -> str:
    """PUBLIC_WS_URL as an http(s) origin. ws:// and wss:// map to http:// and https://."""
    value = os.environ.get("PUBLIC_WS_URL", "").strip().rstrip("/")
    if value.startswith("wss://"):
        return "https://" + value.removeprefix("wss://")
    if value.startswith("ws://"):
        return "http://" + value.removeprefix("ws://")
    return value


def public_origin(request: Request | None = None) -> str:
    configured = configured_origin()
    if configured:
        return configured
    if os.environ.get("K_SERVICE", "").strip():
        raise RuntimeError("PUBLIC_WS_URL must be set in production")
    if request is None:
        return "http://localhost:8000"
    return str(request.base_url).rstrip("/")
