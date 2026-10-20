"""Organization payload encryption.

The server encapsulates a data key to each ML-KEM-1024 public key, then discards it.
It never stores or accepts an organization private key, a member private key, or a recovery key.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.mlkem import MLKEM1024PrivateKey, MLKEM1024PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.execution.activity.models.payload_keys import MemberPayloadKey, OrgPayloadKey

logger = logging.getLogger(__name__)

MEMBER_KEY_PENDING = "pending"
MEMBER_KEY_APPROVED = "approved"
MEMBER_KEY_REJECTED = "rejected"
MEMBER_KEY_INVALID = "invalid"

FORMAT_VERSION = 1
WRAP_ALG = "ML-KEM-1024-HKDF-SHA384-AES-256-GCM"
PAYLOAD_ENCRYPTION = "org-mlkem1024-v1"
PAYLOAD_ALG = "AES-256-GCM"
# Legacy (v1) recovery backups: one unstretched HKDF over the recovery key. Still opened, and
# still accepted on upload while ACCEPT_LEGACY_RECOVERY_KDF is set (payload_key_service).
RECOVERY_KDF = "HKDF-SHA256"
# v2 recovery backups stretch the recovery key with PBKDF2-HMAC-SHA256 (WebCrypto has it natively).
RECOVERY_FORMAT_VERSION = 2
RECOVERY_KDF_V2 = "PBKDF2-HMAC-SHA256"
RECOVERY_ITERATIONS = 600_000
RECOVERY_MAX_ITERATIONS = 10_000_000
# Legacy member backup: the KEK comes from the login password, which the server receives at login.
PASSWORD_KDF = "PBKDF2-HMAC-SHA256"
PASSWORD_ITERATIONS = 600_000
# v2 member backup: an opaque blob the client wrapped under a secret that never reaches the server.
CLIENT_BACKUP_VERSION = 2
CLIENT_BACKUP_KDF = "CLIENT-SECRET"
# Envelope integrity for new envelopes: a MAC keyed by the DEK replaces the plaintext content_sha256,
# and key_check commits to a DEK proof so only DEK holders can add recipient wraps or rewrap.
ENVELOPE_INTEGRITY = "dek-hkdf-hmac-sha256-v1"
_CONTENT_MAC_INFO = b"saturn-payload-content-mac-v1"
_DEK_PROOF_INFO = b"saturn-payload-dek-proof-v1"
_KEY_CHECK_PREFIX = b"saturn-payload-key-check-v1\n"
PUBLIC_KEY_BYTES = 1568
PRIVATE_KEY_BYTES = 64  # FIPS 203 seed. The browser expands this into the decapsulation key.
KEM_CIPHERTEXT_BYTES = 1568
_WRAP_VERSION = 2
_WRAP_INFO = b"saturn-payload-mlkem1024-v1"
_RECOVERY_INFO = b"saturn-recovery-v1"
CONTENT_KEYS = ("arguments", "raw_request", "raw_response", "server_result", "agent_result")


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def aad_bytes(organization_id: str, record_id: str, payload_type: str, version: int) -> bytes:
    return f"{organization_id}\n{record_id}\n{payload_type}\n{version}".encode()


def _record_binding(organization_id: str, record_id: str, payload_type: str) -> bytes:
    return f"{organization_id}\n{record_id}\n{payload_type}".encode()


def _dek_subkey(dek: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(dek)


def content_mac(dek: bytes, organization_id: str, record_id: str, payload_type: str, content: Any) -> str:
    """Keyed digest of the content. Without the DEK it cannot confirm a guessed argument."""
    message = _record_binding(organization_id, record_id, payload_type) + b"\n" + canonical_json(content)
    return hmac.new(_dek_subkey(dek, _CONTENT_MAC_INFO), message, hashlib.sha256).hexdigest()


def dek_proof(dek: bytes, organization_id: str, record_id: str, payload_type: str) -> str:
    """What a client sends to show it holds the record's DEK. The server never stores it."""
    return hmac.new(
        _dek_subkey(dek, _DEK_PROOF_INFO),
        _record_binding(organization_id, record_id, payload_type),
        hashlib.sha256,
    ).hexdigest()


def key_check(proof_hex: str) -> str:
    """Public commitment to the DEK proof, stored in the envelope."""
    return hashlib.sha256(_KEY_CHECK_PREFIX + bytes.fromhex(proof_hex)).hexdigest()


def has_key_check(envelope: dict[str, Any]) -> bool:
    return isinstance(envelope.get("key_check"), str) and bool(envelope.get("key_check"))


def verify_dek_proof(envelope: dict[str, Any], proof_hex: str | None) -> bool:
    if not has_key_check(envelope) or not proof_hex:
        return False
    try:
        raw = bytes.fromhex(proof_hex)
    except ValueError:
        return False
    if len(raw) != 32:
        return False
    return hmac.compare_digest(key_check(proof_hex), str(envelope["key_check"]))


def load_public_key(raw: bytes) -> MLKEM1024PublicKey:
    """Parse and exercise an ML-KEM-1024 encapsulation key. Raises ValueError when it is unusable."""
    if len(raw) != PUBLIC_KEY_BYTES:
        raise ValueError("invalid ML-KEM public key")
    try:
        key = MLKEM1024PublicKey.from_public_bytes(raw)
        _shared, kem_ciphertext = key.encapsulate()
    except Exception as exc:
        raise ValueError("invalid ML-KEM public key") from exc
    if len(kem_ciphertext) != KEM_CIPHERTEXT_BYTES:
        raise ValueError("invalid ML-KEM public key")
    return key


def _wrap_key(shared: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA384(), length=32, salt=None, info=_WRAP_INFO).derive(shared)


def _public_bytes(key: MLKEM1024PublicKey) -> bytes:
    return key.public_bytes_raw()


def _private_bytes(key: MLKEM1024PrivateKey) -> bytes:
    return key.private_bytes_raw()


def generate_mlkem() -> tuple[bytes, bytes]:
    private = MLKEM1024PrivateKey.generate()
    secret = _private_bytes(private)
    public = _public_bytes(private.public_key())
    if len(secret) != PRIVATE_KEY_BYTES or len(public) != PUBLIC_KEY_BYTES:
        raise RuntimeError("unexpected ML-KEM-1024 key size")
    return secret, public


def wrap_secret(recipient_public: bytes, secret: bytes, aad: bytes) -> bytes:
    if len(recipient_public) != PUBLIC_KEY_BYTES:
        raise ValueError("invalid ML-KEM public key")
    shared, kem_ciphertext = MLKEM1024PublicKey.from_public_bytes(recipient_public).encapsulate()
    if len(kem_ciphertext) != KEM_CIPHERTEXT_BYTES:
        raise RuntimeError("unexpected ML-KEM-1024 ciphertext size")
    nonce = os.urandom(12)
    ciphertext = AESGCM(_wrap_key(shared)).encrypt(nonce, secret, aad)
    return bytes((_WRAP_VERSION,)) + kem_ciphertext + nonce + ciphertext


def unwrap_secret(recipient_private: bytes, wrapped: bytes, aad: bytes) -> bytes:
    kem_end = 1 + KEM_CIPHERTEXT_BYTES
    nonce_end = kem_end + 12
    if len(recipient_private) != PRIVATE_KEY_BYTES or len(wrapped) < nonce_end + 16 or wrapped[0] != _WRAP_VERSION:
        raise ValueError("invalid key envelope")
    kem_ciphertext = wrapped[1:kem_end]
    nonce, ciphertext = wrapped[kem_end:nonce_end], wrapped[nonce_end:]
    shared = MLKEM1024PrivateKey.from_seed_bytes(recipient_private).decapsulate(kem_ciphertext)
    return AESGCM(_wrap_key(shared)).decrypt(nonce, ciphertext, aad)


def encrypt_payload(
    *,
    recipient_public: bytes | None,
    organization_id: str,
    record_id: str,
    payload_type: str,
    key_version: int,
    content: Any,
    member_keys: list[tuple[str, str, bytes]] | None = None,
) -> dict[str, Any]:
    dek = os.urandom(32)
    nonce = os.urandom(12)
    aad = aad_bytes(organization_id, record_id, payload_type, FORMAT_VERSION)
    ciphertext = AESGCM(dek).encrypt(nonce, canonical_json(content), aad)
    wrapped = ""
    if recipient_public:
        wrap_aad = f"dek\n{organization_id}\n{key_version}\n{record_id}\n{payload_type}".encode()
        wrapped = wrap_secret(recipient_public, dek, wrap_aad).hex()
    recipient_wraps = []
    proof = dek_proof(dek, organization_id, record_id, payload_type)
    mac = content_mac(dek, organization_id, record_id, payload_type, content)
    for user_id, key_id, public in member_keys or []:
        member_aad = f"dek\n{organization_id}\n{key_id}\n{record_id}\n{payload_type}".encode()
        recipient_wraps.append(
            {
                "user_id": user_id,
                "member_key_id": key_id,
                "wrapped_dek": wrap_secret(public, dek, member_aad).hex(),
            }
        )
    del dek
    return {
        "v": FORMAT_VERSION,
        "alg": PAYLOAD_ALG,
        "wrap": WRAP_ALG,
        "org_key_version": key_version,
        "nonce": nonce.hex(),
        "ciphertext": ciphertext.hex(),
        "wrapped_dek": wrapped,
        "recipient_wraps": recipient_wraps,
        "integrity": ENVELOPE_INTEGRITY,
        "content_mac": mac,
        "key_check": key_check(proof),
    }


def decrypt_payload(private_key: bytes, organization_id: str, record_id: str, payload_type: str, envelope: dict[str, Any]) -> Any:
    if int(envelope.get("v") or 0) != FORMAT_VERSION:
        raise ValueError("unsupported payload encryption version")
    wrapped = bytes.fromhex(str(envelope["wrapped_dek"]))
    wrap_aad = f"dek\n{organization_id}\n{envelope['org_key_version']}\n{record_id}\n{payload_type}".encode()
    dek = unwrap_secret(private_key, wrapped, wrap_aad)
    aad = aad_bytes(organization_id, record_id, payload_type, FORMAT_VERSION)
    body = AESGCM(dek).decrypt(bytes.fromhex(str(envelope["nonce"])), bytes.fromhex(str(envelope["ciphertext"])), aad)
    opened = json.loads(body.decode())
    valid = verify_content(dek, organization_id, record_id, payload_type, envelope, opened)
    del dek
    if not valid:
        raise ValueError("payload hash mismatch")
    return opened


def verify_content(dek: bytes, organization_id: str, record_id: str, payload_type: str, envelope: dict[str, Any], opened: Any) -> bool:
    """New envelopes carry content_mac (keyed by the DEK); legacy ones carry content_sha256."""
    mac = envelope.get("content_mac")
    if isinstance(mac, str) and mac:
        return hmac.compare_digest(content_mac(dek, organization_id, record_id, payload_type, opened), mac)
    legacy = envelope.get("content_sha256")
    if isinstance(legacy, str) and legacy:
        return hmac.compare_digest(content_hash(opened), legacy)
    return False


def _recovery_kek(recovery_key: bytes, backup_salt: bytes, kdf: str, iterations: int) -> bytes:
    if kdf == RECOVERY_KDF_V2:
        if not RECOVERY_ITERATIONS <= iterations <= RECOVERY_MAX_ITERATIONS:
            raise ValueError("unsupported recovery backup")
        return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=backup_salt, iterations=iterations).derive(recovery_key)
    if kdf == RECOVERY_KDF:
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=backup_salt, info=_RECOVERY_INFO).derive(recovery_key)
    raise ValueError("unsupported recovery backup")


def recovery_backup(recovery_key: bytes, organization_id: str, key_version: int, private_key: bytes) -> dict[str, str | int]:
    """Reference for the console: v2 stretches the recovery key with PBKDF2-HMAC-SHA256."""
    salt = os.urandom(16)
    nonce = os.urandom(12)
    derived = _recovery_kek(recovery_key, salt, RECOVERY_KDF_V2, RECOVERY_ITERATIONS)
    aad = f"recovery\n{organization_id}\n{key_version}".encode()
    ciphertext = AESGCM(derived).encrypt(nonce, private_key, aad)
    return {
        "v": RECOVERY_FORMAT_VERSION,
        "kdf": RECOVERY_KDF_V2,
        "iterations": RECOVERY_ITERATIONS,
        "salt": salt.hex(),
        "nonce": nonce.hex(),
        "ciphertext": ciphertext.hex(),
        "org_key_version": key_version,
    }


def legacy_recovery_backup(recovery_key: bytes, organization_id: str, key_version: int, private_key: bytes) -> dict[str, str | int]:
    """The v1 format (unstretched HKDF). Kept so old backups and old consoles keep working."""
    salt = os.urandom(16)
    nonce = os.urandom(12)
    derived = _recovery_kek(recovery_key, salt, RECOVERY_KDF, 0)
    aad = f"recovery\n{organization_id}\n{key_version}".encode()
    ciphertext = AESGCM(derived).encrypt(nonce, private_key, aad)
    return {
        "v": FORMAT_VERSION,
        "kdf": RECOVERY_KDF,
        "salt": salt.hex(),
        "nonce": nonce.hex(),
        "ciphertext": ciphertext.hex(),
        "org_key_version": key_version,
    }


def open_recovery_backup(recovery_key: bytes, organization_id: str, backup: dict[str, Any]) -> bytes:
    derived = _recovery_kek(
        recovery_key,
        bytes.fromhex(str(backup["salt"])),
        str(backup.get("kdf") or ""),
        int(backup.get("iterations") or 0),
    )
    aad = f"recovery\n{organization_id}\n{backup['org_key_version']}".encode()
    return AESGCM(derived).decrypt(bytes.fromhex(str(backup["nonce"])), bytes.fromhex(str(backup["ciphertext"])), aad)


def member_wrap_aad(organization_id: str, user_id: str, key_version: int) -> bytes:
    return f"member\n{organization_id}\n{user_id}\n{key_version}".encode()


# Legacy member backup (M-10). The KEK is derived from the login password, which the server
# receives in plaintext at /auth/login, so a compromised server could rebuild it. New consoles
# send a CLIENT-SECRET backup instead; these helpers stay so old backups can still be opened.
def _member_wrap_material(password: str) -> bytes:
    return hashlib.sha256(b"saturn-member-wrap-v1\n" + password.encode()).digest()


def password_backup(password: str, user_id: str, private_key: bytes) -> dict[str, str | int]:
    salt = os.urandom(16)
    nonce = os.urandom(12)
    kek = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PASSWORD_ITERATIONS,
    ).derive(_member_wrap_material(password))
    ciphertext = AESGCM(kek).encrypt(nonce, private_key, f"password\n{user_id}".encode())
    return {
        "v": FORMAT_VERSION,
        "kdf": PASSWORD_KDF,
        "iterations": PASSWORD_ITERATIONS,
        "salt": salt.hex(),
        "nonce": nonce.hex(),
        "ciphertext": ciphertext.hex(),
    }


def open_password_backup(password: str, user_id: str, backup: dict[str, Any]) -> bytes:
    if backup.get("kdf") != PASSWORD_KDF or int(backup.get("iterations") or 0) != PASSWORD_ITERATIONS:
        raise ValueError("unsupported password backup")
    kek = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=bytes.fromhex(str(backup["salt"])),
        iterations=PASSWORD_ITERATIONS,
    ).derive(_member_wrap_material(password))
    return AESGCM(kek).decrypt(
        bytes.fromhex(str(backup["nonce"])),
        bytes.fromhex(str(backup["ciphertext"])),
        f"password\n{user_id}".encode(),
    )


def is_zero_access(stored: Any) -> bool:
    return isinstance(stored, dict) and isinstance(stored.get("envelope"), dict)


def action_digest(stored: Any) -> str:
    if isinstance(stored, dict):
        return str(stored.get("digest") or "")
    return ""


def _encapsulation_key(value: str) -> bytes | None:
    """The raw key if it parses and encapsulates; None for anything unusable."""
    try:
        raw = bytes.fromhex(value)
        load_public_key(raw)
    except ValueError:
        return None
    return raw


class OrgPayloadKeyInvalid(HTTPException):
    """The active organization key cannot be loaded.

    protect_mapping refuses to write instead of encrypting to members only: an envelope the
    organization key cannot open would silently lock admins and recovery out of that record.
    Registration validates the key, so this only happens for rows stored before that check.
    """

    def __init__(self) -> None:
        super().__init__(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The organization payload key cannot be used. The payload owner must rotate it.",
        )


async def active_org_key(session: AsyncSession, organization_id: str) -> OrgPayloadKey | None:
    return await session.scalar(
        select(OrgPayloadKey)
        .where(OrgPayloadKey.organization_id == organization_id, OrgPayloadKey.status == "active")
        .order_by(OrgPayloadKey.version.desc())
    )


async def protect_mapping(
    session: AsyncSession,
    organization_id: str | None,
    record_id: str,
    payload_type: str,
    content: Any,
) -> dict[str, Any] | None:
    if not organization_id or content in (None, {}, []):
        return None
    key = await active_org_key(session, organization_id)
    members = list(
        await session.scalars(
            select(MemberPayloadKey).where(
                MemberPayloadKey.organization_id == organization_id,
                MemberPayloadKey.status == "active",
                MemberPayloadKey.approval == MEMBER_KEY_APPROVED,
            )
        )
    )
    recipient = None
    if key is not None:
        recipient = _encapsulation_key(key.public_key)
        if recipient is None:
            logger.error("organization payload key %s for %s cannot be loaded", key.id, organization_id)
            raise OrgPayloadKeyInvalid()
    member_keys = []
    for item in members:
        public = _encapsulation_key(item.public_key)
        if public is None:
            # One bad member key must not break every audit write for the organization.
            # Skip it and flag it, so the member sees no active key and registers again.
            logger.warning("member payload key %s cannot be loaded; marking it invalid", item.id)
            item.status = MEMBER_KEY_INVALID
            continue
        member_keys.append((item.user_id, item.id, public))
    if recipient is None and not member_keys:
        return None
    return encrypt_payload(
        recipient_public=recipient,
        organization_id=organization_id,
        record_id=record_id,
        payload_type=payload_type,
        key_version=key.version if key is not None and recipient is not None else 0,
        content=content,
        member_keys=member_keys,
    )


def public_audit_payload(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return payload
    hidden = set(CONTENT_KEYS) | {"sealed", "shown"}
    visible = {key: value for key, value in payload.items() if key not in hidden}
    if isinstance(payload.get("envelope"), dict):
        visible["sealed"] = True
        visible["envelope"] = payload["envelope"]
        visible["arguments"] = {}
    elif isinstance(payload.get("sealed"), str):
        visible["sealed"] = True
        visible["encryption_status"] = "legacy"
        visible["arguments"] = payload.get("shown") if isinstance(payload.get("shown"), dict) else {}
    return visible


def admin_audit_payload(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return payload
    hidden = set(CONTENT_KEYS) | {"sealed", "shown", "envelope"}
    return {key: value for key, value in payload.items() if key not in hidden}
