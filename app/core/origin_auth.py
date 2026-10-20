import hashlib
import hmac
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

EXEMPT_PATHS = {"/healthz"}
DEFAULT_HEADER = "X-Saturn-Origin-Auth"


def request_path(request: Request) -> str:
    """The routed path. request.url is rebuilt from the Host header, which a client controls."""
    return request.scope.get("path") or "/"


def origin_allowed(path: str, headers) -> bool:
    if path in EXEMPT_PATHS:
        return True
    expected = os.environ.get("ORIGIN_AUTH_SECRET", "")
    if not expected:
        # Local and tests leave the secret unset. Cloud Run must fail closed.
        return not os.environ.get("K_SERVICE")
    name = os.environ.get("ORIGIN_AUTH_HEADER", DEFAULT_HEADER)
    presented = headers.get(name, "")
    return hmac.compare_digest(
        hashlib.sha256(presented.encode()).digest(),
        hashlib.sha256(expected.encode()).digest(),
    )


class OriginAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not origin_allowed(request_path(request), request.headers):
            return JSONResponse(status_code=403, content={"detail": "Forbidden"})
        return await call_next(request)
