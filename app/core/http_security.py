import os
import re
import secrets
from contextvars import ContextVar
from urllib.parse import urlparse

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Two years, the usual HSTS lifetime. Preload is omitted because it is hard to undo.
HSTS = "max-age=63072000; includeSubDomains"
API_CSP = "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
_csp_nonce: ContextVar[str] = ContextVar("csp_nonce", default="")
# Extra form-action sources for this response. A list, so a handler that runs in a copied
# context can still add to the one the middleware reads.
_form_targets: ContextVar[list[str] | None] = ContextVar("form_targets", default=None)
_ORIGIN = re.compile(r"[a-z][a-z0-9+.-]*://[A-Za-z0-9.\-\[\]:]+")
_SCHEME = re.compile(r"[a-z][a-z0-9+.-]*:")
_UNSAFE_SCHEMES = frozenset({"javascript", "data", "blob", "filesystem", "vbscript", "about"})


# Server-rendered pages that carry their own nonce-tagged scripts and styles.
_NONCE_PREFIXES = ("/oauth", "/mcp-servers/oauth/callback")


def _nonce_page(path: str) -> bool:
    return path.startswith(_NONCE_PREFIXES)


def current_csp_nonce() -> str:
    return _csp_nonce.get()


def allow_form_redirect(uri: str) -> None:
    """Let a form on this page end at uri's origin.

    Browsers apply form-action to every redirect after a submit, so a consent form that posts
    to 'self' and is answered with a 303 to the agent's redirect_uri is blocked without it.
    Only pass a redirect_uri the client registered.
    """
    targets = _form_targets.get()
    if targets is None:
        return
    parsed = urlparse(uri)
    scheme = parsed.scheme.lower()
    if scheme in {"http", "https"}:
        source = f"{scheme}://{parsed.netloc.rsplit('@', 1)[-1]}"
        if _ORIGIN.fullmatch(source):
            targets.append(source)
    elif scheme and scheme not in _UNSAFE_SCHEMES and _SCHEME.fullmatch(f"{scheme}:"):
        # A native app's custom scheme (cursor://...). CSP matches it by scheme only.
        targets.append(f"{scheme}:")


def oauth_csp(nonce: str, form_targets: list[str] | None = None) -> str:
    form_action = " ".join(["'self'", *dict.fromkeys(form_targets or [])])
    return (
        "default-src 'none'; "
        f"script-src 'self' 'nonce-{nonce}' 'wasm-unsafe-eval'; "
        f"style-src 'nonce-{nonce}'; "
        "connect-src 'self'; "
        f"base-uri 'none'; form-action {form_action}; frame-ancestors 'none'"
    )
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
    if _nonce_page(path):
        headers["Content-Security-Policy"] = oauth_csp(_csp_nonce.get(), _form_targets.get())
    else:
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
        nonce_token = _csp_nonce.set(secrets.token_urlsafe(16)) if _nonce_page(path) else None
        targets_token = _form_targets.set([]) if _nonce_page(path) else None
        if api_docs_closed() and is_api_doc(path):
            if nonce_token is not None:
                _csp_nonce.reset(nonce_token)
            if targets_token is not None:
                _form_targets.reset(targets_token)
            await _not_found(scope, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                apply_security_headers(MutableHeaders(raw=message["headers"]), scope, path)
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        finally:
            if nonce_token is not None:
                _csp_nonce.reset(nonce_token)
            if targets_token is not None:
                _form_targets.reset(targets_token)


async def _not_found(scope: Scope, send: Send) -> None:
    body = b'{"detail":"Not found"}'
    headers = MutableHeaders()
    headers["content-type"] = "application/json"
    headers["content-length"] = str(len(body))
    apply_security_headers(headers, scope, scope.get("path") or "")
    await send({"type": "http.response.start", "status": 404, "headers": headers.raw})
    await send({"type": "http.response.body", "body": body})
