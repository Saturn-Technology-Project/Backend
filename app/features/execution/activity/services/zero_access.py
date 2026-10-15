"""Organization payload encryption.

The server encapsulates a data key to each ML-KEM-1024 public key, then discards it.
It never stores or accepts an organization private key, a member private key, or a recovery key.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.mlkem import MLKEM1024PrivateKey, MLKEM1024PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.execution.activity.models.payload_keys import MemberPayloadKey, OrgPayloadKey

FORMAT_VERSION = 1
WRAP_ALG = "ML-KEM-1024-HKDF-SHA384-AES-256-GCM"
PAYLOAD_ENCRYPTION = "org-mlkem1024-v1"
PAYLOAD_ALG = "AES-256-GCM"
RECOVERY_KDF = "HKDF-SHA256"
PASSWORD_KDF = "PBKDF2-HMAC-SHA256"
PASSWORD_ITERATIONS = 600_000
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
        "content_sha256": content_hash(content),
    }


def decrypt_payload(private_key: bytes, organization_id: str, record_id: str, payload_type: str, envelope: dict[str, Any]) -> Any:
    if int(envelope.get("v") or 0) != FORMAT_VERSION:
        raise ValueError("unsupported payload encryption version")
    wrapped = bytes.fromhex(str(envelope["wrapped_dek"]))
    wrap_aad = f"dek\n{organization_id}\n{envelope['org_key_version']}\n{record_id}\n{payload_type}".encode()
    dek = unwrap_secret(private_key, wrapped, wrap_aad)
    aad = aad_bytes(organization_id, record_id, payload_type, FORMAT_VERSION)
    body = AESGCM(dek).decrypt(bytes.fromhex(str(envelope["nonce"])), bytes.fromhex(str(envelope["ciphertext"])), aad)
    del dek
    opened = json.loads(body.decode())
    if content_hash(opened) != envelope.get("content_sha256"):
        raise ValueError("payload hash mismatch")
    return opened


def recovery_backup(recovery_key: bytes, organization_id: str, key_version: int, private_key: bytes) -> dict[str, str | int]:
    salt = os.urandom(16)
    nonce = os.urandom(12)
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=_RECOVERY_INFO).derive(recovery_key)
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
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=bytes.fromhex(str(backup["salt"])),
        info=_RECOVERY_INFO,
    ).derive(recovery_key)
    aad = f"recovery\n{organization_id}\n{backup['org_key_version']}".encode()
    return AESGCM(derived).decrypt(bytes.fromhex(str(backup["nonce"])), bytes.fromhex(str(backup["ciphertext"])), aad)


def member_wrap_aad(organization_id: str, user_id: str, key_version: int) -> bytes:
    return f"member\n{organization_id}\n{user_id}\n{key_version}".encode()
def password_backup(password: str, user_id: str, private_key: bytes) -> dict[str, str | int]:
    salt = os.urandom(16)
    nonce = os.urandom(12)
    kek = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PASSWORD_ITERATIONS,
    ).derive(password.encode())
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
    ).derive(password.encode())
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
    try:
        raw = bytes.fromhex(value)
    except ValueError:
        return None
    if len(raw) != PUBLIC_KEY_BYTES:
        return None
    return raw


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
            )
        )
    )
    recipient = _encapsulation_key(key.public_key) if key is not None else None
    member_keys = []
    for item in members:
        public = _encapsulation_key(item.public_key)
        if public is not None:
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
