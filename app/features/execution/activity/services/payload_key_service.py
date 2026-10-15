from __future__ import annotations

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
    FORMAT_VERSION,
    PASSWORD_ITERATIONS,
    PASSWORD_KDF,
    PUBLIC_KEY_BYTES,
    RECOVERY_KDF,
    WRAP_ALG,
)
from app.models import Action, AuditLog, Employee, User


def _encapsulation_key(value: str, label: str) -> str:
    try:
        raw = bytes.fromhex(value)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{label} is not hex") from exc
    if len(raw) != PUBLIC_KEY_BYTES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{label} must be an ML-KEM-1024 public key")
    return raw.hex()


async def _member(session: AsyncSession, user_id: str, organization_id: str) -> Employee:
    employee = await session.scalar(
        select(Employee).where(Employee.user_id == user_id, Employee.organization_id == organization_id, Employee.status == "active")
    )
    if employee is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not an organization member")
    return employee


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
    row = OrgPayloadKey(
        id=new_id("opk"),
        organization_id=organization_id,
        version=version,
        public_key=_encapsulation_key(public_key, "public_key"),
        algorithm=WRAP_ALG,
        status="active",
        owner_user_id=owner_user_id,
        created_at=utcnow(),
        activated_at=utcnow(),
    )
    session.add(row)
    session.add(_backup(organization_id, version, backup))
    await session.flush()
    return row


def _backup(organization_id: str, version: int, backup: dict) -> OrgKeyBackup:
    if backup.get("kdf") != RECOVERY_KDF or int(backup.get("v") or 0) != FORMAT_VERSION:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unsupported recovery backup")
    for field in ("salt", "nonce", "ciphertext"):
        if not isinstance(backup.get(field), str) or not backup[field]:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Recovery backup is incomplete")
    return OrgKeyBackup(
        id=new_id("okb"),
        organization_id=organization_id,
        key_version=version,
        format_version=FORMAT_VERSION,
        kdf=RECOVERY_KDF,
        salt=backup["salt"],
        nonce=backup["nonce"],
        ciphertext=backup["ciphertext"],
        created_at=utcnow(),
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
    await _member(session, user_id, organization_id)
    current = await session.scalar(
        select(MemberPayloadKey).where(
            MemberPayloadKey.organization_id == organization_id,
            MemberPayloadKey.user_id == user_id,
            MemberPayloadKey.status == "active",
        )
    )
    replaced = current is not None
    if current is not None:
        current.status = "replaced"
        current.assist_requested = False
    row = MemberPayloadKey(
        id=new_id("mpk"),
        organization_id=organization_id,
        user_id=user_id,
        public_key=_encapsulation_key(public_key, "public_key"),
        status="active",
        assist_requested=bool(needs_assist and replaced),
        created_at=utcnow(),
    )
    session.add(row)
    if backup is not None:
        session.add(_password_backup(organization_id, user_id, row.id, backup))
    await session.flush()
    return row


def _password_backup(organization_id: str, user_id: str, member_key_id: str, backup: dict) -> MemberKeyBackup:
    if backup.get("kdf") != PASSWORD_KDF or int(backup.get("v") or 0) != FORMAT_VERSION:
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


async def own_password_backup(session: AsyncSession, *, organization_id: str, user_id: str) -> tuple[MemberPayloadKey, MemberKeyBackup] | None:
    key = await session.scalar(
        select(MemberPayloadKey).where(
            MemberPayloadKey.organization_id == organization_id,
            MemberPayloadKey.user_id == user_id,
            MemberPayloadKey.status == "active",
        )
    )
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


def _append_wrap(envelope: dict, member_key_id: str, user_id: str, wrapped: str) -> dict:
    wraps = [item for item in envelope.get("recipient_wraps") or [] if isinstance(item, dict) and item.get("member_key_id") != member_key_id]
    wraps.append({"user_id": user_id, "member_key_id": member_key_id, "wrapped_dek": wrapped})
    return {**envelope, "recipient_wraps": wraps}


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
    changed = 0
    for item in records:
        record_id = str(item.get("id") or "")
        kind = str(item.get("type") or "")
        wrapped = str(item.get("wrapped_dek") or "")
        digest = str(item.get("content_sha256") or "")
        if kind == "audit":
            row = await session.get(AuditLog, record_id)
            envelope = _envelope(row.payload) if row is not None else None
            if row is None or row.organization_id != organization_id or envelope is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Encrypted record not found")
            if envelope.get("content_sha256") != digest:
                raise HTTPException(status.HTTP_409_CONFLICT, "Payload hash does not match")
            row.payload = {**row.payload, "envelope": _append_wrap(envelope, target.id, target.user_id, wrapped)}
        elif kind == "action":
            row = await session.get(Action, record_id)
            envelope = _envelope(row.arguments) if row is not None else None
            if row is None or envelope is None or envelope.get("content_sha256") != digest:
                raise HTTPException(status.HTTP_409_CONFLICT, "Action payload hash does not match")
            from app.models import Run

            run = await session.get(Run, row.run_id)
            if run is None or run.organization_id != organization_id:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Encrypted record not found")
            row.arguments = {**row.arguments, "envelope": _append_wrap(envelope, target.id, target.user_id, wrapped)}
        else:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown record type")
        changed += 1
    await session.flush()
    if not await wraps_missing_for(
        session,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        member_key_id=member_key_id,
    ):
        target.assist_requested = False
        await session.flush()
    return changed


async def wraps_missing_for(
    session: AsyncSession,
    *,
    organization_id: str,
    actor_user_id: str,
    member_key_id: str,
) -> list[dict]:
    await _member(session, actor_user_id, organization_id)
    target = await session.get(MemberPayloadKey, member_key_id)
    if target is None or target.organization_id != organization_id or target.status != "active":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Member key not found")
    actor_key = await session.scalar(
        select(MemberPayloadKey).where(
            MemberPayloadKey.organization_id == organization_id,
            MemberPayloadKey.user_id == actor_user_id,
            MemberPayloadKey.status == "active",
        )
    )
    items: list[dict] = []
    logs = list(await session.scalars(select(AuditLog).where(AuditLog.organization_id == organization_id)))
    for row in logs:
        envelope = _envelope(row.payload)
        if envelope is None or _has_wrap(envelope, target.id):
            continue
        items.append(_rewrap_source(row.id, "audit", envelope, actor_key.id if actor_key else ""))
    from app.models import Run

    run_ids = list(await session.scalars(select(Run.id).where(Run.organization_id == organization_id)))
    if run_ids:
        actions = await session.scalars(select(Action).where(Action.run_id.in_(run_ids)))
        for row in actions:
            envelope = _envelope(row.arguments)
            if envelope is None or _has_wrap(envelope, target.id):
                continue
            items.append(_rewrap_source(row.id, "action", envelope, actor_key.id if actor_key else ""))
    return items


async def assist_requests(session: AsyncSession, *, organization_id: str, actor_user_id: str) -> list[dict]:
    await _member(session, actor_user_id, organization_id)
    rows = list(
        await session.scalars(
            select(MemberPayloadKey).where(
                MemberPayloadKey.organization_id == organization_id,
                MemberPayloadKey.status == "active",
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
    if key is None or actor.user_id != key.owner_user_id and actor.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only an organization admin can grant payload access")
    recipient = await _member(session, user_id, organization_id)
    member_key = await session.get(MemberPayloadKey, member_key_id)
    if member_key is None or member_key.organization_id != organization_id or member_key.user_id != recipient.user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Member key not found")
    if member_key.status != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, "Member key is not active")
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
    if key is None or actor.role != "admin":
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


async def rewrap_batch(session: AsyncSession, *, organization_id: str, version: int, records: list[dict]) -> int:
    job = await session.scalar(
        select(PayloadRotation).where(
            PayloadRotation.organization_id == organization_id,
            PayloadRotation.to_version == version,
            PayloadRotation.status == "pending",
        )
    )
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rotation not found")
    changed = 0
    for item in records:
        record_id = str(item.get("id") or "")
        kind = str(item.get("type") or "")
        wrapped = str(item.get("wrapped_dek") or "")
        digest = str(item.get("content_sha256") or "")
        if kind == "audit":
            row = await session.get(AuditLog, record_id)
            envelope = (row.payload or {}).get("envelope") if row and isinstance(row.payload, dict) else None
            if row is None or row.organization_id != organization_id or not isinstance(envelope, dict):
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Encrypted record not found")
            if envelope.get("content_sha256") != digest:
                raise HTTPException(status.HTTP_409_CONFLICT, "Payload hash does not match")
            if int(envelope.get("org_key_version") or 0) == version and envelope.get("wrapped_dek") == wrapped:
                continue
            envelope = {**envelope, "wrapped_dek": wrapped, "org_key_version": version}
            row.payload = {**row.payload, "envelope": envelope, "org_key_version": version}
        elif kind == "action":
            row = await session.get(Action, record_id)
            envelope = row.arguments.get("envelope") if row and isinstance(row.arguments, dict) else None
            if row is None or not isinstance(envelope, dict) or envelope.get("content_sha256") != digest:
                raise HTTPException(status.HTTP_409_CONFLICT, "Action payload hash does not match")
            from app.models import Run

            run = await session.get(Run, row.run_id)
            if run is None or run.organization_id != organization_id:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Encrypted record not found")
            if int(envelope.get("org_key_version") or 0) == version and envelope.get("wrapped_dek") == wrapped:
                continue
            row.arguments = {**row.arguments, "envelope": {**envelope, "wrapped_dek": wrapped, "org_key_version": version}}
        else:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown record type")
        changed += 1
    job.migrated += changed
    await session.flush()
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
        "org_key_version": version,
    }


async def pending_wraps(session: AsyncSession, organization_id: str, version: int) -> list[dict]:
    items: list[dict] = []
    logs = await session.scalars(select(AuditLog).where(AuditLog.organization_id == organization_id))
    for row in logs:
        envelope = _envelope(row.payload)
        if envelope is None:
            continue
        item = _wrap_item(row.id, "audit", envelope, version)
        if item is not None:
            items.append(item)
    from app.models import Run

    run_ids = list(await session.scalars(select(Run.id).where(Run.organization_id == organization_id)))
    if run_ids:
        actions = await session.scalars(select(Action).where(Action.run_id.in_(run_ids)))
        for row in actions:
            envelope = _envelope(row.arguments)
            if envelope is None:
                continue
            item = _wrap_item(row.id, "action", envelope, version)
            if item is not None:
                items.append(item)
    return items


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
    remaining = await pending_wraps(session, organization_id, job.from_version)
    if remaining:
        raise HTTPException(status.HTTP_409_CONFLICT, "Historical payloads are still wrapped to the previous key")
    current.status = "retired"
    current.retired_at = utcnow()
    pending.status = "active"
    pending.activated_at = utcnow()
    session.add(_backup(organization_id, pending.version, backup))
    job.status = "active"
    job.finished_at = utcnow()
    await session.flush()
    return pending

