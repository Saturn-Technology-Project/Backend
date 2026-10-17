import time
from collections import defaultdict

from fastapi import Request

from app.core.config import get_settings

WINDOW_SECONDS = 600
MAX_HITS = 20

_hits: dict[str, list[float]] = defaultdict(list)
_redis = None


def client_ip(request: Request) -> str:
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

        _redis = redis.Redis.from_url(get_settings().redis_url)
    return _redis


def _memory_allow(key: str, *, limit: int, window: int) -> bool:
    now = time.monotonic()
    bucket = [stamp for stamp in _hits[key] if now - stamp < window]
    if len(bucket) >= limit:
        _hits[key] = bucket
        return False
    bucket.append(now)
    _hits[key] = bucket
    return True


def _redis_allow(key: str, *, limit: int, window: int) -> bool:
    client = _redis_client()
    count = int(client.incr(f"saturn:rate:{key}"))
    if count == 1:
        client.expire(f"saturn:rate:{key}", window)
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


def login_allowed(request: Request) -> bool:
    return allow(f"login:{client_ip(request)}")


def migration_allowed(request: Request) -> bool:
    return allow(f"login-migration:{client_ip(request)}", limit=8)


def reset() -> None:
    _hits.clear()
    global _redis
    _redis = None
