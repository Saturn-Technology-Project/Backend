"""Per-tenant credential keys.

The root HMAC key stays in Cloud KMS and is never loaded into this process.
Each organization, and each user when a secret belongs to one person, gets its
own ML-KEM seed from a KMS MAC of a labeled string. A tenant key does not open
another tenant's ciphertext, and a copy of this process does not contain the root.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time

from cryptography.hazmat.primitives import hashes, hmac
from cryptography.hazmat.primitives.asymmetric.mlkem import MLKEM1024PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger(__name__)

_PREFIX = "pq2."
_INFO_AES = b"saturn-credential-aes-256-gcm-v1"
# Full cryptoKeyVersion resource name, for example .../cryptoKeys/kem-root/cryptoKeyVersions/N.
# Rotation creates a new version and points SATURN_KMS_KEY at it. Existing ciphertext stays
# readable only by the version that produced it, so a rotation also rewraps each secret:
# open with the old version, then seal again after the setting changes.
_KMS_ENV = "SATURN_KMS_KEY"
_CONTENT_TTL_SECONDS = 300.0
_root: bytes | None = None
_keys: dict[tuple[str, str, str], MLKEM1024PrivateKey] = {}
_content_keys: dict[str, tuple[float, bytes]] = {}
_kms_client = None


def _hkdf(material: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA384(), length=length, salt=None, info=info).derive(material)


def install_root(root: bytes) -> None:
    """Test hook. Production derives keys through KMS and never calls this."""
    global _root
    if len(root) < 32:
        raise RuntimeError("local root must be at least 32 bytes")
    _root = root
    _keys.clear()
    _content_keys.clear()


def _mac(data: bytes) -> bytes:
    if _root is not None:
        digest = hmac.HMAC(_root, hashes.SHA256())
        digest.update(data)
        return digest.finalize()
    name = os.environ.get(_KMS_ENV, "").strip()
    if not name:
        raise RuntimeError(f"{_KMS_ENV} is required")
    response = _kms().mac_sign(request={"name": name, "data": data})
    mac = bytes(response.mac)
    if len(mac) != 32:
        raise RuntimeError("KMS MAC was not 32 bytes")
    return mac


def require_root() -> None:
    if _root is not None:
        return
    _mac(b"saturn-kms-probe")


def mlkem_seed(organization_id: str, user_id: str = "") -> bytes:
    if not organization_id:
        raise RuntimeError("organization id is required")
    label = f"saturn-mlkem-v1\n{organization_id}\n{user_id}\n".encode()
    return _mac(label + b"0") + _mac(label + b"1")


def _kms():
    global _kms_client
    if _kms_client is None:
        from google.cloud import kms

        _kms_client = kms.KeyManagementServiceClient()
    return _kms_client


def content_key(organization_id: str) -> bytes:
    if not organization_id:
        raise RuntimeError("organization id is required")
    now = time.monotonic()
    cached = _content_keys.get(organization_id)
    if cached is not None and cached[0] > now:
        return cached[1]
    value = _mac(f"saturn-org-content-v2\n{organization_id}".encode())
    _content_keys[organization_id] = (now + _CONTENT_TTL_SECONDS, value)
    return value


async def content_key_async(organization_id: str) -> bytes:
    return await asyncio.to_thread(content_key, organization_id)


async def open_secret_async(token: str, organization_id: str, user_id: str = "") -> str:
    return await asyncio.to_thread(open_secret, token, organization_id, user_id)


async def seal_async(plain: str, organization_id: str, user_id: str = "") -> str:
    return await asyncio.to_thread(seal, plain, organization_id, user_id)


def _private(organization_id: str, user_id: str) -> MLKEM1024PrivateKey:
    cache_key = ("mlkem", organization_id, user_id)
    found = _keys.get(cache_key)
    if found is None:
        found = MLKEM1024PrivateKey.from_seed_bytes(mlkem_seed(organization_id, user_id))
        _keys[cache_key] = found
    return found


def seal(plain: str, organization_id: str, user_id: str = "") -> str:
    if not plain:
        return ""
    public = _private(organization_id, user_id).public_key()
    shared, kem_ciphertext = public.encapsulate()
    key = _hkdf(shared, _INFO_AES, 32)
    nonce = os.urandom(12)
    encrypted = AESGCM(key).encrypt(nonce, plain.encode(), None)
    return _PREFIX + base64.b64encode(kem_ciphertext + nonce + encrypted).decode()


def open_secret(token: str, organization_id: str, user_id: str = "") -> str:
    if not token or not organization_id or not token.startswith(_PREFIX):
        return ""
    try:
        raw = base64.b64decode(token[len(_PREFIX) :])
        kem_ciphertext, rest = raw[:1568], raw[1568:]
        nonce, encrypted = rest[:12], rest[12:]
        shared = _private(organization_id, user_id).decapsulate(kem_ciphertext)
        key = _hkdf(shared, _INFO_AES, 32)
        return AESGCM(key).decrypt(nonce, encrypted, None).decode()
    except Exception as exc:
        logger.warning("credential open failed: %s", type(exc).__name__)
        return ""
