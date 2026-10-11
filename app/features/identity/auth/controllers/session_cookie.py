from __future__ import annotations

from fastapi import Request, Response

from app.core.config import get_settings

COOKIE_NAME = "saturn_session"


def _secure(request: Request) -> bool:
    origin = request.headers.get("origin", "")
    if origin.startswith("http://"):
        return False
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return proto != "http"


def set_session_cookie(response: Response, request: Request, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=_secure(request),
        samesite="lax",
        path="/",
        max_age=settings.session_ttl_days * 24 * 60 * 60,
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        key=COOKIE_NAME,
        path="/",
        httponly=True,
        secure=_secure(request),
        samesite="lax",
    )
