from __future__ import annotations

import os

from fastapi import Request, Response

from app.core.config import get_settings

COOKIE_NAME = "saturn_session"


def cookie_secure(request: Request) -> bool:
    """Secure everywhere except a plain-http local run.

    Production always sets Secure. Request headers (Origin, X-Forwarded-Proto) cannot turn it off there.
    """
    if os.environ.get("K_SERVICE", "").strip():
        return True
    if request.url.scheme == "https":
        return True
    return request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"


def set_session_cookie(response: Response, request: Request, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=cookie_secure(request),
        samesite="lax",
        path="/",
        max_age=settings.session_ttl_days * 24 * 60 * 60,
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        key=COOKIE_NAME,
        path="/",
        httponly=True,
        secure=cookie_secure(request),
        samesite="lax",
    )
