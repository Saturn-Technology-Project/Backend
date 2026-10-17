"""RFC 6238 TOTP. Backup codes are stored only as hashes."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

from fastapi import HTTPException, status
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import new_id, utcnow
from app.features.credentials.vault.services.credential_service import open_secret, seal
from app.features.identity.auth.models.mfa import TotpBackupCode
from app.features.identity.auth.models.user import User

_STEP = 30
_DIGITS = 6
_BACKUP_COUNT = 8


def _hotp(secret: bytes, counter: int) -> str:
    digest = hmac.new(secret, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(binary % 10**_DIGITS).zfill(_DIGITS)


def current_code(secret_b32: str, now: float | None = None) -> str:
    moment = time.time() if now is None else now
    return _hotp(_decode(secret_b32), int(moment // _STEP))


def verify_code(secret_b32: str, code: str, now: float | None = None) -> bool:
    presented = code.strip()
    if len(presented) != _DIGITS or not presented.isdigit():
        return False
    moment = time.time() if now is None else now
    counter = int(moment // _STEP)
    matched = False
    for offset in (-1, 0, 1):
        matched |= hmac.compare_digest(_hotp(_decode(secret_b32), counter + offset), presented)
    return matched


def hash_backup_code(code: str) -> str:
    return hashlib.sha256(f"saturn-totp-backup-v1\n{code}".encode()).hexdigest()


def _decode(secret_b32: str) -> bytes:
    padded = secret_b32.strip().upper()
    padded += "=" * ((8 - len(padded) % 8) % 8)
    return base64.b32decode(padded)


def _otpauth(email: str, secret_b32: str) -> str:
    label = quote(f"Saturn:{email}")
    return (
        f"otpauth://totp/{label}?secret={secret_b32}"
        "&issuer=Saturn&algorithm=SHA1&digits=6&period=30"
    )


async def begin_setup(session: AsyncSession, user: User) -> dict[str, object]:
    if user.totp_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "MFA is already enabled")
    raw = secrets.token_bytes(20)
    secret_b32 = base64.b32encode(raw).decode().rstrip("=")
    user.totp_secret = seal(secret_b32, user.organization_id, user.id)
    user.totp_enabled = False
    await session.execute(delete(TotpBackupCode).where(TotpBackupCode.user_id == user.id))
    codes = [secrets.token_hex(4) for _ in range(_BACKUP_COUNT)]
    for code in codes:
        session.add(
            TotpBackupCode(
                id=new_id("tbc"),
                user_id=user.id,
                code_hash=hash_backup_code(code),
                created_at=utcnow(),
            )
        )
    await session.flush()
    return {"secret": secret_b32, "otpauth_url": _otpauth(user.email, secret_b32), "backup_codes": codes}


async def confirm_setup(session: AsyncSession, user: User, code: str) -> None:
    if user.totp_enabled:
        return
    if not user.totp_secret:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Start MFA setup first")
    try:
        secret_b32 = open_secret(user.totp_secret, user.organization_id, user.id)
    except Exception as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "MFA setup needs to be started again") from exc
    if not secret_b32 or not verify_code(secret_b32, code):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That code did not match")
    user.totp_enabled = True
    await session.flush()
