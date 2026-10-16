from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.mlkem import MLKEM1024PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_PREFIX = "pq1."
_INFO_AES = b"saturn-credential-aes-256-gcm-v1"
_SEED_ENV = "SATURN_KEM_SEED"
_seed_bytes: bytes | None = None
_private: MLKEM1024PrivateKey | None = None


def _hkdf(material: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA384(), length=length, salt=None, info=info).derive(material)


def install_seed(seed: bytes) -> None:
    global _seed_bytes, _private
    if len(seed) != 64:
        raise RuntimeError("ML-KEM seed must be 64 bytes")
    _seed_bytes = seed
    _private = None


def _seed_from_env() -> bytes:
    raw = os.environ.get(_SEED_ENV, "").strip()
    if not raw:
        raise RuntimeError(f"{_SEED_ENV} is required")
    try:
        seed = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise RuntimeError(f"{_SEED_ENV} is not valid base64") from exc
    if len(seed) != 64:
        raise RuntimeError(f"{_SEED_ENV} must be 64 bytes")
    return seed


def require_seed() -> bytes:
    global _seed_bytes
    if _seed_bytes is None:
        _seed_bytes = _seed_from_env()
    return _seed_bytes


def _seed() -> bytes:
    return require_seed()


def private_key() -> MLKEM1024PrivateKey:
    global _private
    if _private is None:
        _private = MLKEM1024PrivateKey.from_seed_bytes(_seed())
    return _private


def reset_key() -> None:
    global _private
    _private = None


def seal(plain: str) -> str:
    if not plain:
        return ""
    public = private_key().public_key()
    shared, kem_ciphertext = public.encapsulate()
    key = _hkdf(shared, _INFO_AES, 32)
    nonce = os.urandom(12)
    encrypted = AESGCM(key).encrypt(nonce, plain.encode(), None)
    return _PREFIX + base64.b64encode(kem_ciphertext + nonce + encrypted).decode()


def open_secret(token: str) -> str:
    if not token:
        return ""
    if not token.startswith(_PREFIX):
        return ""
    try:
        raw = base64.b64decode(token[len(_PREFIX) :])
        kem_ciphertext, rest = raw[:1568], raw[1568:]
        nonce, encrypted = rest[:12], rest[12:]
        shared = private_key().decapsulate(kem_ciphertext)
        key = _hkdf(shared, _INFO_AES, 32)
        return AESGCM(key).decrypt(nonce, encrypted, None).decode()
    except Exception:
        return ""
