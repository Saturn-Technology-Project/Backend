from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any, Callable

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import new_id, utcnow
from app.features.execution.activity.models.payload_keys import (
    MemberKeyBackup,
    MemberKeyEnvelope,
    MemberPayloadKey,
    OrgKeyBackup,
    OrgPayloadKey,
    PayloadRotation,
)
from app.features.execution.activity.services.zero_access import (
    CLIENT_BACKUP_KDF,
    CLIENT_BACKUP_VERSION,
    FORMAT_VERSION,
    MEMBER_KEY_APPROVED,
    MEMBER_KEY_PENDING,
    MEMBER_KEY_REJECTED,
    PASSWORD_ITERATIONS,
    PASSWORD_KDF,
    RECOVERY_FORMAT_VERSION,
    RECOVERY_ITERATIONS,
    RECOVERY_KDF,
    RECOVERY_KDF_V2,
    RECOVERY_MAX_ITERATIONS,
    WRAP_ALG,
    has_key_check,
    load_public_key,
    verify_dek_proof,
)
from app.features.organization.org.services.access import allows
from app.models import Action, AuditLog, Employee, Run, User

# The console uploads v2 (PBKDF2) recovery backups and CLIENT-SECRET member backups. Older uploads
# are refused; backups already stored in the old formats can still be downloaded and opened.
ACCEPT_LEGACY_RECOVERY_KDF = False
ACCEPT_LEGACY_PASSWORD_BACKUP = False

KEY_AUDIT_SOURCE = "payload_keys"
EVENT_ORG_KEY_CREATED = "payload_org_key_created"
EVENT_MEMBER_KEY_REGISTERED = "payload_member_key_registered"
EVENT_MEMBER_KEY_APPROVED = "payload_member_key_approved"
EVENT_MEMBER_KEY_REJECTED = "payload_member_key_rejected"
EVENT_MEMBER_REVOKED = "payload_member_revoked"
EVENT_WRAPS_GRANTED = "payload_wraps_granted"
EVENT_ENVELOPE_GRANTED = "payload_envelope_granted"
EVENT_ROTATION_REWRAPPED = "payload_rotation_rewrapped"
KEY_AUDIT_EVENTS = (
    EVENT_ORG_KEY_CREATED,
    EVENT_MEMBER_KEY_REGISTERED,
    EVENT_MEMBER_KEY_APPROVED,
    EVENT_MEMBER_KEY_REJECTED,
    EVENT_MEMBER_REVOKED,
    EVENT_WRAPS_GRANTED,
    EVENT_ENVELOPE_GRANTED,
    EVENT_ROTATION_REWRAPPED,
)

SCAN_DEFAULT_LIMIT = 200
SCAN_MAX_LIMIT = 500
_SCAN_CHUNK = 200
_SCAN_BUDGET = 5000
_SCAN_PHASES = ("audit", "action")
_AUDIT_RECORD_IDS = 100
_CLIENT_SCHEME = re.compile(r"^[A-Za-z0-9._-]{1,40}$")
_CLIENT_BLOB_MAX = 16384


def _encapsulation_key(value: str, label: str) -> str:
    try:
        raw = bytes.fromhex(value)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{label} is not hex") from exc
    try:
        load_public_key(raw)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{label} must be an ML-KEM-1024 public key") from exc
    return raw.hex()


def key_fingerprint(public_key_hex: str) -> str:
    """Short SHA-256 of the key so an admin can compare it with the member out of band."""
    try:
        raw = bytes.fromhex(public_key_hex)
    except ValueError:
        raw = public_key_hex.encode()
    return hashlib.sha256(raw).hexdigest()[:32]


async def _member(session: AsyncSession, user_id: str, organization_id: str) -> Employee:
    employee = await session.scalar(
        select(Employee).where(Employee.user_id == user_id, Employee.organization_id == organization_id, Employee.status == "active")
    )
    if employee is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not an organization member")
    return employee


def _is_key_admin(employee: Employee) -> bool:
    return allows(employee.permissions, "team", "write")


async def _other_key_admin(session: AsyncSession, organization_id: str, user_id: str) -> bool:
    """Whether another team.write admin holds an approved key and so can approve this one."""
    rows = (
        await session.execute(
            select(Employee, MemberPayloadKey)
            .join(
                MemberPayloadKey,
                (MemberPayloadKey.user_id == Employee.user_id)
                & (MemberPayloadKey.organization_id == Employee.organization_id),
            )
            .where(
                Employee.organization_id == organization_id,
                Employee.user_id != user_id,
                Employee.status == "active",
                MemberPayloadKey.status == "active",
                MemberPayloadKey.approval == MEMBER_KEY_APPROVED,
            )
        )
    ).all()
    return any(_is_key_admin(employee) for employee, _key in rows)


async def _audit(
    session: AsyncSession,
    *,
    event: str,
    actor_id: str,
    organization_id: str,
    result: str = "",
    payload: dict[str, Any] | None = None,
) -> None:
    from app.core.enums import ActorType
    from app.features.execution.activity.services.audit_service import audit_service

    await audit_service.log(
        session,
        event=event,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        organization_id=organization_id,
        result=result or None,
        payload={"source": KEY_AUDIT_SOURCE, **(payload or {})},
    )


async def active_key(session: AsyncSession, organization_id: str) -> OrgPayloadKey | None:
    return await session.scalar(
        select(OrgPayloadKey)
        .where(OrgPayloadKey.organization_id == organization_id, OrgPayloadKey.status == "active")
        .order_by(OrgPayloadKey.version.desc())
    )


async def create_org_key(
    session: AsyncSession,
    *,
    organization_id: str,
    owner_user_id: str,
    public_key: str,
    backup: dict,
) -> OrgPayloadKey:
    await _member(session, owner_user_id, organization_id)
    if await active_key(session, organization_id) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Organization payload key already exists")
    version = 1
    # Validate everything first. The session commits on HTTPException, so a refused backup must not
    # leave an active key behind with no way to recover it.
    public_hex = _encapsulation_key(public_key, "public_key")
    backup_row = _backup(organization_id, version, backup)
    row = OrgPayloadKey(
        id=new_id("opk"),
        organization_id=organization_id,
        version=version,
        public_key=public_hex,
        algorithm=WRAP_ALG,
        status="active",
        owner_user_id=owner_user_id,
        created_at=utcnow(),
        activated_at=utcnow(),
    )
    session.add(row)
    session.add(backup_row)
    await session.flush()
    await _audit(
        session,
        event=EVENT_ORG_KEY_CREATED,
        actor_id=owner_user_id,
        organization_id=organization_id,
        payload={"org_key_version": version, "fingerprint": key_fingerprint(row.public_key)},
    )
    return row


def _backup(organization_id: str, version: int, backup: dict) -> OrgKeyBackup:
    format_version = int(backup.get("v") or 0)
    kdf = backup.get("kdf")
    iterations = int(backup.get("iterations") or 0)
    if format_version == RECOVERY_FORMAT_VERSION and kdf == RECOVERY_KDF_V2:
        if not RECOVERY_ITERATIONS <= iterations <= RECOVERY_MAX_ITERATIONS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unsupported recovery backup")
    elif format_version == FORMAT_VERSION and kdf == RECOVERY_KDF and ACCEPT_LEGACY_RECOVERY_KDF:
        iterations = 0
    else:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unsupported recovery backup")
    for field in ("salt", "nonce", "ciphertext"):
        if not isinstance(backup.get(field), str) or not backup[field]:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Recovery backup is incomplete")
    return OrgKeyBackup(
        id=new_id("okb"),
        organization_id=organization_id,
        key_version=version,
        format_version=format_version,
        kdf=str(kdf),
        iterations=iterations,
        salt=backup["salt"],
        nonce=backup["nonce"],
        ciphertext=backup["ciphertext"],
        created_at=utcnow(),
    )


def recovery_backup_view(row: OrgKeyBackup) -> dict:
    body: dict[str, Any] = {
        "v": row.format_version,
        "kdf": row.kdf,
        "salt": row.salt,
        "nonce": row.nonce,
        "ciphertext": row.ciphertext,
        "org_key_version": row.key_version,
        "legacy": row.kdf == RECOVERY_KDF,
    }
    if row.iterations:
        body["iterations"] = row.iterations
    return body


async def own_member_key(session: AsyncSession, *, organization_id: str, user_id: str) -> MemberPayloadKey | None:
    return await session.scalar(
        select(MemberPayloadKey).where(
            MemberPayloadKey.organization_id == organization_id,
            MemberPayloadKey.user_id == user_id,
            MemberPayloadKey.status == "active",
        )
    )


async def register_member_key(
    session: AsyncSession,
    *,
    organization_id: str,
    user_id: str,
    public_key: str,
    backup: dict | None = None,
    needs_assist: bool = False,
) -> MemberPayloadKey:
    employee = await _member(session, user_id, organization_id)
    public_hex = _encapsulation_key(public_key, "public_key")
    key_id = new_id("mpk")
    # Validate the backup before touching the current key: the session commits on HTTPException,
    # so a refused registration must not leave the member without their working key.
    backup_row = _member_backup(organization_id, user_id, key_id, backup) if backup is not None else None
    current = await own_member_key(session, organization_id=organization_id, user_id=user_id)
    replaced = current is not None
    replaced_id = current.id if current is not None else ""
    if current is not None:
        current.status = "replaced"
        current.assist_requested = False
    # A key only receives DEK wraps once a team.write admin approves it. An admin's first key is
    # approved on registration, which also covers the owner bootstrapping the first key. A key that
    # replaces one needs a second admin when there is one: whoever holds a stolen session must not
    # be able to swap in their own key and have colleagues re-wrap every record to it.
    auto = _is_key_admin(employee) and not (
        replaced and await _other_key_admin(session, organization_id, user_id)
    )
    row = MemberPayloadKey(
        id=key_id,
        organization_id=organization_id,
        user_id=user_id,
        public_key=public_hex,
        status="active",
        approval=MEMBER_KEY_APPROVED if auto else MEMBER_KEY_PENDING,
        approved_by=user_id if auto else "",
        approved_at=utcnow() if auto else None,
        assist_requested=bool(needs_assist and replaced),
        created_at=utcnow(),
    )
    session.add(row)
    if backup_row is not None:
        session.add(backup_row)
    await session.flush()
    await _audit(
        session,
        event=EVENT_MEMBER_KEY_REGISTERED,
        actor_id=user_id,
        organization_id=organization_id,
        result=row.approval,
        payload={
            "member_key_id": row.id,
            "user_id": user_id,
            "fingerprint": key_fingerprint(public_hex),
            "approval": row.approval,
            "auto_approved": auto,
            "replaced_member_key_id": replaced_id,
            "backup_format": _backup_format(backup),
        },
    )
    return row


def _backup_format(backup: dict | None) -> str:
    if backup is None:
        return ""
    if int(backup.get("v") or 0) == CLIENT_BACKUP_VERSION and backup.get("kdf") == CLIENT_BACKUP_KDF:
        return "client-secret"
    return "legacy-password"


def _member_backup(organization_id: str, user_id: str, member_key_id: str, backup: dict) -> MemberKeyBackup:
    version = int(backup.get("v") or 0)
    if version == CLIENT_BACKUP_VERSION and backup.get("kdf") == CLIENT_BACKUP_KDF:
        # The client wrapped the private key under a secret the server never sees (not the
        # login password). The server stores the blob as-is and derives nothing from it.
        scheme = str(backup.get("scheme") or "")
        blob = backup.get("blob")
        if not _CLIENT_SCHEME.match(scheme):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Client backup scheme is invalid")
        if not isinstance(blob, str) or not blob or len(blob) > _CLIENT_BLOB_MAX:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Client backup is incomplete")
        return MemberKeyBackup(
            id=new_id("mkb"),
            organization_id=organization_id,
            user_id=user_id,
            member_key_id=member_key_id,
            format_version=CLIENT_BACKUP_VERSION,
            kdf=f"{CLIENT_BACKUP_KDF}:{scheme}",
            iterations=0,
            salt="",
            nonce="",
            ciphertext=blob,
            created_at=utcnow(),
        )
    if not ACCEPT_LEGACY_PASSWORD_BACKUP:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unsupported password backup")
    if backup.get("kdf") != PASSWORD_KDF or version != FORMAT_VERSION:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unsupported password backup")
    if int(backup.get("iterations") or 0) != PASSWORD_ITERATIONS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unsupported password backup")
    for field in ("salt", "nonce", "ciphertext"):
        if not isinstance(backup.get(field), str) or not backup[field]:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Password backup is incomplete")
    return MemberKeyBackup(
        id=new_id("mkb"),
        organization_id=organization_id,
        user_id=user_id,
        member_key_id=member_key_id,
        format_version=FORMAT_VERSION,
        kdf=PASSWORD_KDF,
        iterations=PASSWORD_ITERATIONS,
        salt=backup["salt"],
        nonce=backup["nonce"],
        ciphertext=backup["ciphertext"],
        created_at=utcnow(),
    )


def member_backup_view(key: MemberPayloadKey, backup: MemberKeyBackup) -> dict:
    body: dict[str, Any] = {
        "member_key_id": key.id,
        "public_key": key.public_key,
        "approval": key.approval,
        "v": backup.format_version,
    }
    if backup.kdf.startswith(f"{CLIENT_BACKUP_KDF}:"):
        body.update(
            {
                "kdf": CLIENT_BACKUP_KDF,
                "scheme": backup.kdf.split(":", 1)[1],
                "blob": backup.ciphertext,
                "legacy": False,
            }
        )
        return body
    body.update(
        {
            "kdf": backup.kdf,
            "iterations": backup.iterations,
            "salt": backup.salt,
            "nonce": backup.nonce,
            "ciphertext": backup.ciphertext,
            "legacy": True,
        }
    )
    return body


async def own_password_backup(session: AsyncSession, *, organization_id: str, user_id: str) -> tuple[MemberPayloadKey, MemberKeyBackup] | None:
    key = await own_member_key(session, organization_id=organization_id, user_id=user_id)
    if key is None:
        return None
    backup = await session.scalar(
        select(MemberKeyBackup)
        .where(MemberKeyBackup.member_key_id == key.id, MemberKeyBackup.user_id == user_id)
        .order_by(MemberKeyBackup.created_at.desc())
    )
    if backup is None:
        return None
    return key, backup


async def _admin(session: AsyncSession, actor_user_id: str, organization_id: str) -> Employee:
    actor = await _member(session, actor_user_id, organization_id)
    if not _is_key_admin(actor):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing team.write access")
    return actor


async def pending_member_keys(session: AsyncSession, *, organization_id: str, actor_user_id: str) -> list[dict]:
    await _admin(session, actor_user_id, organization_id)
    rows = list(
        await session.scalars(
            select(MemberPayloadKey)
            .where(
                MemberPayloadKey.organization_id == organization_id,
                MemberPayloadKey.status == "active",
                MemberPayloadKey.approval == MEMBER_KEY_PENDING,
            )
            .order_by(MemberPayloadKey.created_at)
            .limit(SCAN_MAX_LIMIT)
        )
    )
    found = []
    for row in rows:
        person = await session.get(User, row.user_id)
        found.append(
            {
                "member_key_id": row.id,
                "user_id": row.user_id,
                "name": person.name if person is not None else row.user_id,
                "email": person.email if person is not None else "",
                "fingerprint": key_fingerprint(row.public_key),
                "public_key": row.public_key,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "approval": row.approval,
            }
        )
    return found


async def decide_member_key(
    session: AsyncSession,
    *,
    organization_id: str,
    actor_user_id: str,
    member_key_id: str,
    approve: bool,
    via: str = "approval",
) -> MemberPayloadKey:
    await _admin(session, actor_user_id, organization_id)
    row = await session.get(MemberPayloadKey, member_key_id)
    if row is None or row.organization_id != organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Member key not found")
    if row.status != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, "Member key is not active")
    await _member(session, row.user_id, organization_id)
    wanted = MEMBER_KEY_APPROVED if approve else MEMBER_KEY_REJECTED
    if row.approval == wanted:
        return row
    if approve and row.approval == MEMBER_KEY_REJECTED and via != "envelope":
        raise HTTPException(status.HTTP_409_CONFLICT, "A rejected key must be registered again")
    if approve and row.user_id == actor_user_id and await _other_key_admin(session, organization_id, actor_user_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Another admin must approve your key")
    row.approval = wanted
    row.approved_by = actor_user_id
    row.approved_at = utcnow()
    if not approve:
        row.assist_requested = False
    await session.flush()
    await _audit(
        session,
        event=EVENT_MEMBER_KEY_APPROVED if approve else EVENT_MEMBER_KEY_REJECTED,
        actor_id=actor_user_id,
        organization_id=organization_id,
        result=wanted,
        payload={
            "member_key_id": row.id,
            "user_id": row.user_id,
            "fingerprint": key_fingerprint(row.public_key),
            "via": via,
        },
    )
    return row


def _same_wrap(envelope: dict, member_key_id: str, wrapped: str) -> bool | None:
    """None when the key has no wrap, otherwise whether the existing wrap equals `wrapped`."""
    for item in envelope.get("recipient_wraps") or []:
        if isinstance(item, dict) and item.get("member_key_id") == member_key_id:
            return hmac.compare_digest(str(item.get("wrapped_dek") or ""), wrapped)
    return None


def _append_wrap(envelope: dict, member_key_id: str, user_id: str, wrapped: str) -> dict:
    """Add a wrap for a key that has none. An existing wrap is never removed or replaced."""
    if _same_wrap(envelope, member_key_id, wrapped) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This member key already has a wrap for the record")
    wraps = [item for item in envelope.get("recipient_wraps") or [] if isinstance(item, dict)]
    wraps.append({"user_id": user_id, "member_key_id": member_key_id, "wrapped_dek": wrapped})
    return {**envelope, "recipient_wraps": wraps}


def _check_wrapped(wrapped: str) -> None:
    try:
        raw = bytes.fromhex(wrapped)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "wrapped_dek is not hex") from exc
    # version byte + ML-KEM ciphertext + nonce + 32-byte DEK + GCM tag
    if len(raw) != 1 + 1568 + 12 + 32 + 16:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "wrapped_dek has the wrong size")


def _check_possession(envelope: dict, item: dict) -> None:
    """New envelopes need a DEK proof. Legacy ones (no key_check) fall back to the old digest match."""
    if has_key_check(envelope):
        if not verify_dek_proof(envelope, str(item.get("dek_proof") or "")):
            raise HTTPException(status.HTTP_409_CONFLICT, "DEK proof does not match")
        return
    digest = str(item.get("content_sha256") or "")
    legacy = envelope.get("content_sha256")
    if not isinstance(legacy, str) or not legacy or not hmac.compare_digest(legacy, digest):
        raise HTTPException(status.HTTP_409_CONFLICT, "Payload hash does not match")


async def _encrypted_record(session: AsyncSession, organization_id: str, kind: str, record_id: str):
    if kind == "audit":
        row = await session.get(AuditLog, record_id)
        envelope = _envelope(row.payload) if row is not None else None
        if row is None or row.organization_id != organization_id or envelope is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Encrypted record not found")
        return row, envelope
    if kind == "action":
        row = await session.get(Action, record_id)
        envelope = _envelope(row.arguments) if row is not None else None
        if row is None or envelope is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Encrypted record not found")
        run = await session.get(Run, row.run_id)
        if run is None or run.organization_id != organization_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Encrypted record not found")
        return row, envelope
    raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown record type")


def _store_envelope(row: Any, kind: str, envelope: dict, extra: dict | None = None) -> None:
    if kind == "audit":
        row.payload = {**row.payload, "envelope": envelope, **(extra or {})}
    else:
        row.arguments = {**row.arguments, "envelope": envelope}


async def grant_recipient_wraps(
    session: AsyncSession,
    *,
    organization_id: str,
    actor_user_id: str,
    member_key_id: str,
    records: list[dict],
) -> int:
    await _member(session, actor_user_id, organization_id)
    target = await session.get(MemberPayloadKey, member_key_id)
    if target is None or target.organization_id != organization_id or target.status != "active":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Member key not found")
    if target.approval != MEMBER_KEY_APPROVED:
        raise HTTPException(status.HTTP_409_CONFLICT, "Member key is waiting for admin approval")
    changed = 0
    granted: list[str] = []
    for item in records:
        record_id = str(item.get("id") or "")
        kind = str(item.get("type") or "")
        wrapped = str(item.get("wrapped_dek") or "")
        row, envelope = await _encrypted_record(session, organization_id, kind, record_id)
        _check_possession(envelope, item)
        _check_wrapped(wrapped)
        existing = _same_wrap(envelope, target.id, wrapped)
        if existing is True:
            continue
        _store_envelope(row, kind, _append_wrap(envelope, target.id, target.user_id, wrapped))
        granted.append(f"{kind}:{record_id}")
        changed += 1
    await session.flush()
    if changed:
        await _audit(
            session,
            event=EVENT_WRAPS_GRANTED,
            actor_id=actor_user_id,
            organization_id=organization_id,
            payload={
                "member_key_id": target.id,
                "user_id": target.user_id,
                "count": changed,
                "records": granted[:_AUDIT_RECORD_IDS],
            },
        )
    remaining = await wraps_missing_for(
        session,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        member_key_id=member_key_id,
        limit=1,
    )
    if not remaining["records"] and remaining["next_cursor"] is None:
        target.assist_requested = False
        await session.flush()
    return changed


def _parse_cursor(cursor: str | None) -> tuple[str, str]:
    if not cursor:
        return _SCAN_PHASES[0], ""
    phase, _, after = cursor.partition(":")
    if phase not in _SCAN_PHASES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid cursor")
    return phase, after


def _bounded(limit: int | None) -> int:
    if limit is None:
        return SCAN_DEFAULT_LIMIT
    return max(1, min(int(limit), SCAN_MAX_LIMIT))


async def _scan_chunk(session: AsyncSession, organization_id: str, kind: str, after: str, size: int, version: int | None):
    if kind == "audit":
        stmt = select(AuditLog).where(
            AuditLog.organization_id == organization_id,
            AuditLog.payload[("envelope", "ciphertext")].as_string().is_not(None),
        )
        if version is not None:
            stmt = stmt.where(AuditLog.payload[("envelope", "org_key_version")].as_integer() == version)
        if after:
            stmt = stmt.where(AuditLog.id > after)
        return list(await session.scalars(stmt.order_by(AuditLog.id).limit(size)))
    stmt = (
        select(Action)
        .join(Run, Action.run_id == Run.id)
        .where(
            Run.organization_id == organization_id,
            Action.arguments[("envelope", "ciphertext")].as_string().is_not(None),
        )
    )
    if version is not None:
        stmt = stmt.where(Action.arguments[("envelope", "org_key_version")].as_integer() == version)
    if after:
        stmt = stmt.where(Action.id > after)
    return list(await session.scalars(stmt.order_by(Action.id).limit(size)))


async def _scan(
    session: AsyncSession,
    organization_id: str,
    *,
    cursor: str | None,
    limit: int,
    version: int | None = None,
    keep: Callable[[dict], bool] | None = None,
) -> tuple[list[tuple[str, Any, dict]], str | None]:
    """Encrypted records of the organization in (type, id) order, filtered in SQL where possible.

    Work per call is bounded: at most `limit` results and _SCAN_BUDGET rows read. When either
    bound stops the scan, the returned cursor continues it; None means the scan is finished.
    """
    phase, after = _parse_cursor(cursor)
    found: list[tuple[str, Any, dict]] = []
    scanned = 0
    for kind in _SCAN_PHASES[_SCAN_PHASES.index(phase) :]:
        last = after if kind == phase else ""
        while True:
            size = min(_SCAN_CHUNK, _SCAN_BUDGET - scanned)
            if size <= 0:
                return found, f"{kind}:{last}"
            rows = await _scan_chunk(session, organization_id, kind, last, size, version)
            for row in rows:
                scanned += 1
                last = row.id
                envelope = _envelope(row.payload if kind == "audit" else row.arguments)
                if envelope is None or (keep is not None and not keep(envelope)):
                    continue
                found.append((kind, row, envelope))
                if len(found) >= limit:
                    return found, f"{kind}:{last}"
            if len(rows) < size:
                break
    return found, None


async def wraps_missing_for(
    session: AsyncSession,
    *,
    organization_id: str,
    actor_user_id: str,
    member_key_id: str,
    cursor: str | None = None,
    limit: int | None = None,
) -> dict:
    await _member(session, actor_user_id, organization_id)
    target = await session.get(MemberPayloadKey, member_key_id)
    if target is None or target.organization_id != organization_id or target.status != "active":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Member key not found")
    if target.approval != MEMBER_KEY_APPROVED:
        raise HTTPException(status.HTTP_409_CONFLICT, "Member key is waiting for admin approval")
    actor_key = await own_member_key(session, organization_id=organization_id, user_id=actor_user_id)
    rows, next_cursor = await _scan(
        session,
        organization_id,
        cursor=cursor,
        limit=_bounded(limit),
        keep=lambda envelope: not _has_wrap(envelope, target.id),
    )
    items = [_rewrap_source(row.id, kind, envelope, actor_key.id if actor_key else "") for kind, row, envelope in rows]
    return {"records": items, "next_cursor": next_cursor}


async def assist_requests(session: AsyncSession, *, organization_id: str, actor_user_id: str) -> list[dict]:
    await _member(session, actor_user_id, organization_id)
    rows = list(
        await session.scalars(
            select(MemberPayloadKey).where(
                MemberPayloadKey.organization_id == organization_id,
                MemberPayloadKey.status == "active",
                MemberPayloadKey.approval == MEMBER_KEY_APPROVED,
                MemberPayloadKey.assist_requested.is_(True),
                MemberPayloadKey.user_id != actor_user_id,
            )
        )
    )
    found = []
    for row in rows:
        person = await session.get(User, row.user_id)
        found.append(
            {
                "user_id": row.user_id,
                "name": person.name if person is not None else row.user_id,
                "member_key_id": row.id,
                "public_key": row.public_key,
            }
        )
    return found


def _has_wrap(envelope: dict, member_key_id: str) -> bool:
    return any(
        isinstance(item, dict) and item.get("member_key_id") == member_key_id
        for item in envelope.get("recipient_wraps") or []
    )


def _rewrap_source(record_id: str, kind: str, envelope: dict, actor_key_id: str) -> dict:
    own = ""
    for item in envelope.get("recipient_wraps") or []:
        if isinstance(item, dict) and item.get("member_key_id") == actor_key_id:
            own = str(item.get("wrapped_dek") or "")
    return {
        "id": record_id,
        "type": kind,
        "content_sha256": envelope.get("content_sha256"),
        "proof_required": has_key_check(envelope),
        "org_key_version": envelope.get("org_key_version"),
        "wrapped_dek": envelope.get("wrapped_dek") or "",
        "own_wrap": own,
    }


async def add_envelope(
    session: AsyncSession,
    *,
    organization_id: str,
    actor_user_id: str,
    user_id: str,
    member_key_id: str,
    org_key_version: int,
    wrapped_private_key: str,
) -> MemberKeyEnvelope:
    actor = await _member(session, actor_user_id, organization_id)
    key = await active_key(session, organization_id)
    if key is None or actor.user_id != key.owner_user_id and not _is_key_admin(actor):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only an organization admin can grant payload access")
    recipient = await _member(session, user_id, organization_id)
    member_key = await session.get(MemberPayloadKey, member_key_id)
    if member_key is None or member_key.organization_id != organization_id or member_key.user_id != recipient.user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Member key not found")
    if member_key.status != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, "Member key is not active")
    if member_key.approval != MEMBER_KEY_APPROVED:
        # Handing a member the organization private key is a stronger grant than DEK wraps,
        # so an admin doing it approves the key as well, and that is recorded.
        await decide_member_key(
            session,
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            member_key_id=member_key.id,
            approve=True,
            via="envelope",
        )
    row = MemberKeyEnvelope(
        id=new_id("mke"),
        organization_id=organization_id,
        user_id=recipient.user_id,
        member_key_id=member_key.id,
        org_key_version=org_key_version,
        wrapped_private_key=wrapped_private_key,
        created_at=utcnow(),
    )
    session.add(row)
    await session.flush()
    await _audit(
        session,
        event=EVENT_ENVELOPE_GRANTED,
        actor_id=actor_user_id,
        organization_id=organization_id,
        payload={"member_key_id": member_key.id, "user_id": recipient.user_id, "org_key_version": org_key_version},
    )
    return row


async def own_envelope(session: AsyncSession, *, organization_id: str, user_id: str) -> MemberKeyEnvelope | None:
    return await session.scalar(
        select(MemberKeyEnvelope)
        .where(
            MemberKeyEnvelope.organization_id == organization_id,
            MemberKeyEnvelope.user_id == user_id,
            MemberKeyEnvelope.revoked_at.is_(None),
        )
        .order_by(MemberKeyEnvelope.created_at.desc())
    )


async def revoke_member(session: AsyncSession, *, organization_id: str, actor_user_id: str, user_id: str) -> dict:
    actor = await _member(session, actor_user_id, organization_id)
    key = await active_key(session, organization_id)
    if key is None or not _is_key_admin(actor):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only an organization admin can revoke payload access")
    rows = list(
        await session.scalars(
            select(MemberKeyEnvelope).where(
                MemberKeyEnvelope.organization_id == organization_id,
                MemberKeyEnvelope.user_id == user_id,
                MemberKeyEnvelope.revoked_at.is_(None),
            )
        )
    )
    moment = utcnow()
    for row in rows:
        row.revoked_at = moment
    member_keys = list(
        await session.scalars(
            select(MemberPayloadKey).where(
                MemberPayloadKey.organization_id == organization_id,
                MemberPayloadKey.user_id == user_id,
                MemberPayloadKey.status == "active",
            )
        )
    )
    for item in member_keys:
        item.status = "revoked"
    await session.flush()
    await _audit(
        session,
        event=EVENT_MEMBER_REVOKED,
        actor_id=actor_user_id,
        organization_id=organization_id,
        payload={"user_id": user_id, "envelopes": len(rows), "member_keys": [item.id for item in member_keys]},
    )
    return {"revoked": len(rows), "historical_key_retained": True}


async def start_rotation(session: AsyncSession, *, organization_id: str, actor_user_id: str, public_key: str) -> PayloadRotation:
    actor = await _member(session, actor_user_id, organization_id)
    current = await active_key(session, organization_id)
    if current is None or actor.user_id != current.owner_user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the payload owner can rotate the organization key")
    pending = await session.scalar(
        select(OrgPayloadKey).where(OrgPayloadKey.organization_id == organization_id, OrgPayloadKey.status == "pending")
    )
    if pending is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "A rotation is already in progress")
    version = current.version + 1
    session.add(
        OrgPayloadKey(
            id=new_id("opk"),
            organization_id=organization_id,
            version=version,
            public_key=_encapsulation_key(public_key, "public_key"),
            algorithm=WRAP_ALG,
            status="pending",
            owner_user_id=actor.user_id,
            created_at=utcnow(),
        )
    )
    job = PayloadRotation(
        id=new_id("prot"),
        organization_id=organization_id,
        from_version=current.version,
        to_version=version,
        status="pending",
        migrated=0,
        error="",
        created_at=utcnow(),
    )
    session.add(job)
    await session.flush()
    return job


async def rewrap_batch(
    session: AsyncSession,
    *,
    organization_id: str,
    version: int,
    records: list[dict],
    actor_user_id: str,
) -> int:
    job = await session.scalar(
        select(PayloadRotation).where(
            PayloadRotation.organization_id == organization_id,
            PayloadRotation.to_version == version,
            PayloadRotation.status == "pending",
        )
    )
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rotation not found")
    actor = await _member(session, actor_user_id, organization_id)
    current = await active_key(session, organization_id)
    if current is None or actor.user_id != current.owner_user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the payload owner can rewrap payloads")
    changed = 0
    moved: list[str] = []
    for item in records:
        record_id = str(item.get("id") or "")
        kind = str(item.get("type") or "")
        wrapped = str(item.get("wrapped_dek") or "")
        row, envelope = await _encrypted_record(session, organization_id, kind, record_id)
        _check_possession(envelope, item)
        if int(envelope.get("org_key_version") or 0) == version and envelope.get("wrapped_dek") == wrapped:
            continue
        if int(envelope.get("org_key_version") or 0) != job.from_version:
            raise HTTPException(status.HTTP_409_CONFLICT, "Record is not wrapped to the key being rotated")
        _check_wrapped(wrapped)
        updated = {**envelope, "wrapped_dek": wrapped, "org_key_version": version}
        _store_envelope(row, kind, updated, {"org_key_version": version})
        moved.append(f"{kind}:{record_id}")
        changed += 1
    job.migrated += changed
    await session.flush()
    if changed:
        await _audit(
            session,
            event=EVENT_ROTATION_REWRAPPED,
            actor_id=actor_user_id,
            organization_id=organization_id,
            payload={"org_key_version": version, "count": changed, "records": moved[:_AUDIT_RECORD_IDS]},
        )
    return changed


def _envelope(value: object) -> dict | None:
    if isinstance(value, dict) and isinstance(value.get("envelope"), dict):
        return value["envelope"]
    return None


def _wrap_item(record_id: str, kind: str, envelope: dict, version: int) -> dict | None:
    if int(envelope.get("org_key_version") or 0) != version:
        return None
    return {
        "id": record_id,
        "type": kind,
        "wrapped_dek": envelope.get("wrapped_dek"),
        "content_sha256": envelope.get("content_sha256"),
        "proof_required": has_key_check(envelope),
        "org_key_version": version,
    }


async def pending_wraps(
    session: AsyncSession,
    organization_id: str,
    version: int,
    *,
    cursor: str | None = None,
    limit: int | None = None,
) -> dict:
    rows, next_cursor = await _scan(session, organization_id, cursor=cursor, limit=_bounded(limit), version=version)
    items = []
    for kind, row, envelope in rows:
        item = _wrap_item(row.id, kind, envelope, version)
        if item is not None:
            items.append(item)
    return {"records": items, "next_cursor": next_cursor}


async def _any_wrapped_to(session: AsyncSession, organization_id: str, version: int) -> bool:
    cursor: str | None = None
    while True:
        found = await pending_wraps(session, organization_id, version, cursor=cursor, limit=1)
        if found["records"]:
            return True
        cursor = found["next_cursor"]
        if cursor is None:
            return False


async def activate_rotation(session: AsyncSession, *, organization_id: str, actor_user_id: str, backup: dict) -> OrgPayloadKey:
    actor = await _member(session, actor_user_id, organization_id)
    job = await session.scalar(
        select(PayloadRotation).where(PayloadRotation.organization_id == organization_id, PayloadRotation.status == "pending")
    )
    if job is None or actor.user_id != (await active_key(session, organization_id)).owner_user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the payload owner can finish rotation")
    pending = await session.scalar(
        select(OrgPayloadKey).where(
            OrgPayloadKey.organization_id == organization_id,
            OrgPayloadKey.version == job.to_version,
            OrgPayloadKey.status == "pending",
        )
    )
    current = await session.scalar(
        select(OrgPayloadKey).where(
            OrgPayloadKey.organization_id == organization_id,
            OrgPayloadKey.version == job.from_version,
            OrgPayloadKey.status == "active",
        )
    )
    if pending is None or current is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Rotation is incomplete")
    if await _any_wrapped_to(session, organization_id, job.from_version):
        raise HTTPException(status.HTTP_409_CONFLICT, "Historical payloads are still wrapped to the previous key")
    # Check the backup before switching keys, so a bad one leaves the rotation as it was.
    backup_row = _backup(organization_id, pending.version, backup)
    current.status = "retired"
    current.retired_at = utcnow()
    pending.status = "active"
    pending.activated_at = utcnow()
    session.add(backup_row)
    job.status = "active"
    job.finished_at = utcnow()
    await session.flush()
    return pending


async def key_audit(
    session: AsyncSession,
    *,
    organization_id: str,
    actor_user_id: str,
    limit: int,
    offset: int,
) -> list[AuditLog]:
    await _admin(session, actor_user_id, organization_id)
    return list(
        await session.scalars(
            select(AuditLog)
            .where(AuditLog.organization_id == organization_id, AuditLog.event.in_(KEY_AUDIT_EVENTS))
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .offset(offset)
            .limit(limit)
        )
    )
