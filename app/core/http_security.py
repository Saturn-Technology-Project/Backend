import os

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Two years, the usual HSTS lifetime. Preload is omitted because it is hard to undo.
HSTS = "max-age=63072000; includeSubDomains"
API_CSP = "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
# Swagger and ReDoc load scripts from a CDN and an inline bootstrap. Only used when docs are open.
DOCS_CSP = (
    "default-src 'none'; "
    "script-src 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src https://fastapi.tiangolo.com; "
    "connect-src 'self'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
PERMISSIONS_POLICY = (
    "accelerometer=(), camera=(), geolocation=(), gyroscope=(), microphone=(), payment=(), usb=()"
)


def api_docs_closed() -> bool:
    """Production Cloud Run always sets K_SERVICE. EXPOSE_API_DOCS=false closes docs anywhere else."""
    if os.environ.get("K_SERVICE", "").strip():
        return True
    flag = os.environ.get("EXPOSE_API_DOCS", "true").strip().lower()
    return flag in {"0", "false", "no", "off"}


def is_api_doc(path: str) -> bool:
    if path == "/openapi.json":
        return True
    return path == "/redoc" or path.startswith("/docs") or path.startswith("/redoc/")


def _https(scope: Scope) -> bool:
    if scope.get("scheme") == "https":
        return True
    for key, value in scope.get("headers") or []:
        if key == b"x-forwarded-proto":
            return value.split(b",", 1)[0].strip() == b"https"
    return False


def apply_security_headers(headers: MutableHeaders, scope: Scope, path: str) -> None:
    headers["X-Content-Type-Options"] = "nosniff"
    headers["X-Frame-Options"] = "DENY"
    headers["Referrer-Policy"] = "no-referrer"
    headers["Permissions-Policy"] = PERMISSIONS_POLICY
    headers["Content-Security-Policy"] = DOCS_CSP if is_api_doc(path) and not api_docs_closed() else API_CSP
    if _https(scope):
        headers["Strict-Transport-Security"] = HSTS


class HttpSecurityMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or ""
        if api_docs_closed() and is_api_doc(path):
            await _not_found(scope, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                apply_security_headers(MutableHeaders(raw=message["headers"]), scope, path)
            await send(message)

        await self.app(scope, receive, send_with_headers)


async def _not_found(scope: Scope, send: Send) -> None:
    body = b'{"detail":"Not found"}'
    headers = MutableHeaders()
    headers["content-type"] = "application/json"
    headers["content-length"] = str(len(body))
    apply_security_headers(headers, scope, scope.get("path") or "")
    await send({"type": "http.response.start", "status": 404, "headers": headers.raw})
    await send({"type": "http.response.body", "body": body})
