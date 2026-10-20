"""Arguments that an approval executes, and the digest that binds them.

An approval must run exactly the arguments people approved. When the organization
has no payload key, the gateway keeps them sealed with the organization's content
key (KMS-rooted, see pq_crypto.content_key). It never stores only a digest and
then sends that digest upstream.

Digests are keyed: HMAC-SHA256 with a per-organization key derived from the same
KMS-rooted content key. A plain SHA-256 stored next to the data would let anyone
with a database copy confirm guesses of low-entropy arguments such as an amount.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.features.credentials.vault.services.pq_crypto import content_key
from app.features.execution.activity.services.content_seal import open_json, seal_json

SERVER_SEALED = "server-sealed-v1"
_DIGEST_INFO = b"saturn-approval-digest-v1\n"


class ArgumentsUnavailable(Exception):
    """Stored arguments exist but cannot be opened. Callers must fail closed."""


def _canonical(arguments: Any) -> bytes:
    return json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str).encode()


def legacy_digest(arguments: Any) -> str:
    return hashlib.sha256(_canonical(arguments)).hexdigest()


def _digest_key(organization_id: str) -> bytes:
    # Separate the MAC key from the AES content key with HKDF instead of reusing it directly.
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_DIGEST_INFO + organization_id.encode(),
    ).derive(content_key(organization_id))


def keyed_digest(organization_id: str, arguments: Any) -> str:
    if not organization_id:
        raise RuntimeError("organization id is required")
    return hmac.new(_digest_key(organization_id), _canonical(arguments), hashlib.sha256).hexdigest()


def digest_matches(organization_id: str, stored: str | None, arguments: Any) -> bool:
    """Rows written before keyed digests hold a plain SHA-256; both forms are 64 hex characters."""
    if not stored:
        return False
    if hmac.compare_digest(stored, keyed_digest(organization_id, arguments)):
        return True
    return hmac.compare_digest(stored, legacy_digest(arguments))


def server_sealed(organization_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"sealed": seal_json(organization_id, arguments), "encryption": SERVER_SEALED}


def is_server_sealed(stored: Any) -> bool:
    return isinstance(stored, dict) and stored.get("encryption") == SERVER_SEALED and isinstance(stored.get("sealed"), str)


def _is_legacy_sealed(stored: Any) -> bool:
    return isinstance(stored, dict) and isinstance(stored.get("sealed"), str) and set(stored) <= {"sealed"}


def open_sealed(organization_id: str | None, stored: Any) -> dict[str, Any]:
    """Open server-sealed or legacy sealed arguments. Raises instead of returning {} on failure."""
    if not organization_id:
        raise ArgumentsUnavailable()
    try:
        opened = open_json(organization_id, stored["sealed"])
    except Exception as exc:
        raise ArgumentsUnavailable() from exc
    if not isinstance(opened, dict):
        raise ArgumentsUnavailable()
    return opened


def stored_arguments(organization_id: str | None, stored: Any) -> dict[str, Any]:
    """Like content_seal.plain_arguments, but a sealed value that cannot be opened is an error."""
    if is_server_sealed(stored) or _is_legacy_sealed(stored):
        return open_sealed(organization_id, stored)
    if not isinstance(stored, dict):
        return {}
    return stored
