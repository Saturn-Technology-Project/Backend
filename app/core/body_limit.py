"""A ceiling on request bodies, applied before any handler reads one.

Without it a handler that calls request.json() buffers whatever arrives, and the platform's
own limit (32 MiB on Cloud Run) is most of an instance's memory. Sign-in and agent
registration are reachable without an account, so they get a much smaller ceiling.
"""

from __future__ import annotations

from fastapi import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Large enough for a 500-record key rewrap batch, a 1 MB policy definition or a profile photo.
DEFAULT_MAX_BYTES = 4 * 1024 * 1024
# Credentials, codes, passkey answers and OAuth forms are a few kilobytes at most.
SMALL_MAX_BYTES = 64 * 1024
_SMALL_PREFIXES = ("/auth/", "/oauth/", "/mcp-servers/oauth/")
# A photo is a data URL of up to 512 KiB of image.
_SMALL_EXCEPTIONS = frozenset({"/auth/me/photo"})
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def limit_for(path: str) -> int:
    if path in _SMALL_EXCEPTIONS:
        return DEFAULT_MAX_BYTES
    if path.startswith(_SMALL_PREFIXES):
        return SMALL_MAX_BYTES
    return DEFAULT_MAX_BYTES


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method", "").upper() not in _BODY_METHODS:
            await self.app(scope, receive, send)
            return
        limit = limit_for(scope.get("path") or "")
        declared = _content_length(scope)
        if declared is not None and declared > limit:
            await _too_large(send)
            return
        received = 0

        async def capped() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body") or b"")
                if received > limit:
                    # No Content-Length, or one that lied. FastAPI passes an HTTPException
                    # raised while it reads the body straight through as the response. It is
                    # FastAPI's class, so handlers that catch that one see it too.
                    raise HTTPException(status_code=413, detail="Request body is too large")
            return message

        await self.app(scope, capped, send)


def _content_length(scope: Scope) -> int | None:
    for key, value in scope.get("headers") or []:
        if key == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


async def _too_large(send: Send) -> None:
    body = b'{"detail":"Request body is too large"}'
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})
