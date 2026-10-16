"""Seal tool arguments and upstream bodies so a database copy is not readable.

The running gateway can still open an organization's content. The key is a
KMS MAC of that organization id, separate from the credential seal. Platform
admin responses never receive the opened content.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.ids import utcnow
from app.features.credentials.vault.services.pq_crypto import content_key

CONTENT_KEYS = ("arguments", "raw_request", "raw_response", "server_result", "agent_result")
CONTENT_TTL = timedelta(days=7)
_PREFIX = "s2."


def policy_fields(rule: Any) -> list[str]:
    if rule is None:
        return []
    names: list[str] = []
    conditions = getattr(rule, "conditions", None) or []
    if isinstance(conditions, list):
        for item in conditions:
            if isinstance(item, dict) and str(item.get("field") or "").strip():
                names.append(str(item["field"]).strip())
    expression = getattr(rule, "expression", None)
    if isinstance(expression, dict) and str(expression.get("field") or "").strip():
        names.append(str(expression["field"]).strip())
    condition = str(getattr(rule, "condition", "") or "")
    for atom in condition.split("AND"):
        piece = atom.strip()
        if not piece or piece.lower() in {"true", "*"}:
            continue
        field = piece.split(" ", 1)[0].strip()
        if field.isidentifier():
            names.append(field)
    return list(dict.fromkeys(names))


def _key(organization_id: str) -> bytes:
    return content_key(organization_id)


def seal_json(organization_id: str, value: Any) -> str:
    nonce = os.urandom(12)
    body = json.dumps(value, separators=(",", ":"), sort_keys=True, default=str).encode()
    encrypted = AESGCM(_key(organization_id)).encrypt(nonce, body, None)
    return _PREFIX + (nonce + encrypted).hex()


def open_json(organization_id: str, token: str) -> Any:
    if not isinstance(token, str) or not token.startswith(_PREFIX):
        return None
    raw = bytes.fromhex(token[len(_PREFIX) :])
    nonce, encrypted = raw[:12], raw[12:]
    body = AESGCM(_key(organization_id)).decrypt(nonce, encrypted, None)
    return json.loads(body.decode())


def seal_arguments(organization_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if not organization_id:
        return arguments
    return {"sealed": seal_json(organization_id, arguments)}


def plain_arguments(organization_id: str | None, stored: Any) -> dict[str, Any]:
    if not isinstance(stored, dict):
        return {}
    token = stored.get("sealed")
    if isinstance(token, str) and set(stored) <= {"sealed"} and organization_id:
        try:
            opened = open_json(organization_id, token)
        except Exception:
            return {}
        return opened if isinstance(opened, dict) else {}
    return stored


def seal_payload(organization_id: str | None, payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or not organization_id:
        return payload
    content = {key: payload[key] for key in CONTENT_KEYS if key in payload and payload[key] not in (None, {}, [])}
    if not content:
        return payload
    rest = {key: value for key, value in payload.items() if key not in content}
    fields = rest.get("policy_fields") if isinstance(rest.get("policy_fields"), list) else []
    arguments = content.get("arguments") if isinstance(content.get("arguments"), dict) else {}
    if not isinstance(rest.get("shown"), dict):
        rest["shown"] = {name: arguments[name] for name in fields if name in arguments}
    rest["sealed"] = seal_json(organization_id, content)
    return rest


def public_payload(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return payload
    if not isinstance(payload.get("sealed"), str):
        return payload
    shown = payload.get("shown") if isinstance(payload.get("shown"), dict) else {}
    visible = {key: value for key, value in payload.items() if key not in {"sealed", "shown", *CONTENT_KEYS}}
    visible["sealed"] = True
    visible["arguments"] = shown
    return visible


def facts_only(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return payload
    hidden = set(CONTENT_KEYS) | {"sealed", "shown"}
    return {key: value for key, value in payload.items() if key not in hidden}


def reveal_content(organization_id: str, payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    token = payload.get("sealed")
    if not isinstance(token, str):
        return None
    opened = open_json(organization_id, token)
    return opened if isinstance(opened, dict) else None


def content_deadline():
    return utcnow() + CONTENT_TTL
