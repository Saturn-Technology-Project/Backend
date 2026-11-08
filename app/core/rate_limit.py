import hashlib
import hmac
import ipaddress
import os
import time
from collections import defaultdict

from fastapi import Request

from app.core.config import get_settings

WINDOW_SECONDS = 600
MAX_HITS = 20
_PRUNE_EVERY = 1024

_hits: dict[str, list[float]] = defaultdict(list)
_calls = 0
_redis = None

# INCR and EXPIRE in one step, so a key can never be left without a TTL.
_INCR_WITH_TTL = """
local count = redis.call('INCR', KEYS[1])
if redis.call('TTL', KEYS[1]) < 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""


def behind_cloudflare() -> bool:
    """CF-Connecting-IP is only meaningful when every request must carry the Cloudflare origin secret."""
    return bool(os.environ.get("ORIGIN_AUTH_SECRET", "").strip())


CONSOLE_AUTH_HEADER = "x-saturn-console-auth"
CONSOLE_CLIENT_IP_HEADER = "x-saturn-client-ip"


def _console_secret_ok(presented: str) -> bool:
    accepted = [
        os.environ.get("CONSOLE_PROXY_SECRET", "").strip(),
        os.environ.get("CONSOLE_PROXY_SECRET_PREVIOUS", "").strip(),
    ]
    digest = hashlib.sha256(presented.encode()).digest()
    return any(hmac.compare_digest(digest, hashlib.sha256(value.encode()).digest()) for value in accepted if value)


def console_client_ip(request: Request) -> str:
    """The browser's address as the console proxy reported it, or "" when it cannot be trusted.

    Every console request reaches the API from the console's own address, so Cloudflare's
    CF-Connecting-IP names the console, not the browser. The console sends the browser's address
    with a secret only it holds; without that secret the header is ignored.
    """
    presented = request.headers.get(CONSOLE_AUTH_HEADER, "")
    if not presented or not _console_secret_ok(presented):
        return ""
    value = request.headers.get(CONSOLE_CLIENT_IP_HEADER, "").strip()
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return ""


def client_ip(request: Request) -> str:
    """The address used for rate limits and throttles.

    The console proxy's report wins when its secret matches. Cloudflare overwrites CF-Connecting-IP, so it
    is trusted only when origin auth proves the request came through Cloudflare. Otherwise the rightmost
    X-Forwarded-For entry is the one the nearest proxy appended. Any value a client chose itself is never used.
    """
    console = console_client_ip(request)
    if console:
        return console
    if behind_cloudflare():
        cf_ip = request.headers.get("cf-connecting-ip", "").strip()
        if cf_ip:
            return cf_ip
    forwarded = request.headers.get("x-forwarded-for", "")
    parts = [part.strip() for part in forwarded.split(",") if part.strip()]
    if parts:
        return parts[-1]
    if request.client is not None and request.client.host:
        return request.client.host
    return "unknown"


def _use_redis() -> bool:
    settings = get_settings()
    return bool(settings.redis_url) and not settings.in_process_runtime


def _redis_client():
    global _redis
    if _redis is None:
        import redis

        # Short timeouts: the client is synchronous, so a slow Redis must not stall the event loop.
        _redis = redis.Redis.from_url(
            get_settings().redis_url,
            socket_timeout=0.5,
            socket_connect_timeout=0.5,
        )
    return _redis


def _prune(now: float, window: int) -> None:
    for key in list(_hits):
        kept = [stamp for stamp in _hits[key] if now - stamp < max(window, WINDOW_SECONDS)]
        if kept:
            _hits[key] = kept
        else:
            del _hits[key]


def _memory_allow(key: str, *, limit: int, window: int) -> bool:
    global _calls
    now = time.monotonic()
    _calls += 1
    if _calls % _PRUNE_EVERY == 0:
        _prune(now, window)
    bucket = [stamp for stamp in _hits[key] if now - stamp < window]
    if len(bucket) >= limit:
        _hits[key] = bucket
        return False
    bucket.append(now)
    _hits[key] = bucket
    return True


def _redis_allow(key: str, *, limit: int, window: int) -> bool:
    client = _redis_client()
    count = int(client.eval(_INCR_WITH_TTL, 1, f"saturn:rate:{key}", int(window)))
    return count <= limit


def allow(key: str, *, limit: int = MAX_HITS, window: int = WINDOW_SECONDS) -> bool:
    if _use_redis():
        try:
            return _redis_allow(key, limit=limit, window=window)
        except Exception:
            return False
    if not get_settings().in_process_runtime:
        return False
    return _memory_allow(key, limit=limit, window=window)


def registration_allowed(request: Request) -> bool:
    return allow(f"oauth-register:{client_ip(request)}")


def signup_allowed(request: Request) -> bool:
    """Account sign-up. Its own budget, so agent client registrations cannot use it up."""
    return allow(f"signup:{client_ip(request)}")


def login_allowed(request: Request, email: str | None = None) -> bool:
    if not allow(f"login:{client_ip(request)}"):
        return False
    if email is None:
        return True
    # Per account, so rotating addresses does not give unlimited guesses against one user.
    return allow(f"login-account:{email.strip().lower()}", limit=10, window=WINDOW_SECONDS)


def migration_allowed(request: Request, email: str | None = None) -> bool:
    if not allow(f"login-migration:{client_ip(request)}", limit=24):
        return False
    if email is None:
        return True
    # It answers whether a legacy password is right, so it gets the same per-account cap as sign-in.
    return allow(f"login-migration-account:{email.strip().lower()}", limit=10)


def code_check_allowed(request: Request, subject: str | None = None) -> bool:
    """Checking an emailed, reset or authenticator code. Per address, and per account when known.

    Each code is also capped at a few guesses; this stops one address from spreading guesses
    over many codes and accounts.
    """
    if not allow(f"code-check:{client_ip(request)}", limit=30):
        return False
    if not subject:
        return True
    return allow(f"code-check-account:{subject.strip().lower()}", limit=15)


def reauth_allowed(user_id: str) -> bool:
    return allow(f"reauth:{user_id}", limit=8, window=900)


def accept_invite_allowed(request: Request) -> bool:
    return allow(f"accept-invite:{client_ip(request)}", limit=20)


def reset() -> None:
    global _redis, _calls
    _hits.clear()
    _calls = 0
    _redis = None
