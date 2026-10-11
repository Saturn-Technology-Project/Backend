from __future__ import annotations

import hashlib
import hmac
import json

HEADER_ID = "Saturn-Webhook-Id"
HEADER_TIMESTAMP = "Saturn-Webhook-Timestamp"
HEADER_SIGNATURE = "Saturn-Webhook-Signature"
SCHEMA = "saturn.webhook.v1"


def canonical_body(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def sign(secret: str, timestamp: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"v1={digest}"


def signed_headers(secret: str, event_id: str, timestamp: str, body: bytes) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "User-Agent": "Saturn-Webhooks",
        HEADER_ID: event_id,
        HEADER_TIMESTAMP: timestamp,
        HEADER_SIGNATURE: sign(secret, timestamp, body),
    }
