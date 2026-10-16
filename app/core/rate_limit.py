import time
from collections import defaultdict

from fastapi import Request

WINDOW_SECONDS = 600
MAX_HITS = 20

_hits: dict[str, list[float]] = defaultdict(list)


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    parts = [part.strip() for part in forwarded.split(",") if part.strip()]
    if parts:
        return parts[-1]
    if request.client is not None and request.client.host:
        return request.client.host
    return "unknown"


def allow(key: str, *, limit: int = MAX_HITS, window: int = WINDOW_SECONDS) -> bool:
    now = time.monotonic()
    bucket = [stamp for stamp in _hits[key] if now - stamp < window]
    if len(bucket) >= limit:
        _hits[key] = bucket
        return False
    bucket.append(now)
    _hits[key] = bucket
    return True


def registration_allowed(request: Request) -> bool:
    return allow(f"oauth-register:{client_ip(request)}")


def reset() -> None:
    _hits.clear()
