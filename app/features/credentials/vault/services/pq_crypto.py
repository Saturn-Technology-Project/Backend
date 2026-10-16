from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.mlkem import MLKEM1024PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_PREFIX = "pq1."
_INFO_AES = b"saturn-credential-aes-256-gcm-v1"
_FALLBACK_SEED = base64.b64decode("noH34+h4eNdeKTOYslHcTbs8cbYaAvbXGeXps41eUYwyu9Tny4rA5UjXV+DksUokHvYY+96z1yZQKjLZ7JE2+g==")
_private: MLKEM1024PrivateKey | None = None


def _hkdf(material: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA384(), length=length, salt=None, info=info).derive(material)


def _seed() -> bytes:
    return _FALLBACK_SEED


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
