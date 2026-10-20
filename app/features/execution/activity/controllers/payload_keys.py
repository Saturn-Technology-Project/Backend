from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from app.features.execution.activity.services import payload_key_service as keys
from app.features.identity.auth.controllers.deps import CurrentUser, SessionDep, TeamWrite

router = APIRouter(prefix="/org/payload-keys", tags=["payload-keys"])

_SECRET_FIELDS = {"recovery_key", "private_key", "dek", "org_private_key", "member_private_key", "password"}


class BackupBody(BaseModel):
    """Recovery backup. v2 is PBKDF2-HMAC-SHA256 with `iterations`; v1 (HKDF-SHA256) is legacy."""

    model_config = ConfigDict(extra="forbid")

    v: int
    kdf: str
    iterations: int | None = None
    salt: str
    nonce: str
    ciphertext: str
    org_key_version: int


class CreateOrgKeyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    public_key: str
    backup: BackupBody


class PasswordBackupBody(BaseModel):
    """Member key backup.

    v2 (kdf "CLIENT-SECRET"): `scheme` names the client's wrapping scheme and `blob` is the
    client-wrapped private key, opaque to the server. v1 (PBKDF2 from the login password) is legacy.
    """

    model_config = ConfigDict(extra="forbid")

    v: int
    kdf: str
    iterations: int | None = None
    salt: str | None = None
    nonce: str | None = None
    ciphertext: str | None = None
    scheme: str | None = None
    blob: str | None = None


class MemberKeyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    public_key: str
    backup: PasswordBackupBody | None = None
    needs_assist: bool = False


class EnvelopeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    member_key_id: str
    org_key_version: int
    wrapped_private_key: str


class RotateStartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    public_key: str


class RewrapItem(BaseModel):
    """`dek_proof` is required for envelopes that carry `key_check` (see zero_access.dek_proof).

    `content_sha256` is only checked for legacy envelopes that still carry one.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    type: str
    wrapped_dek: str
    content_sha256: str | None = None
    dek_proof: str | None = None


class RewrapBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    records: list[RewrapItem] = Field(max_length=keys.SCAN_MAX_LIMIT)


class ActivateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backup: BackupBody


class TransferBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str


def _no_secrets(body: BaseModel) -> None:
    leaked = _SECRET_FIELDS.intersection(getattr(body, "model_fields_set", set()))
    if leaked:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Private key material is not accepted")


@router.get("/backup")
async def latest_backup(session: SessionDep, user: CurrentUser, _team: TeamWrite):
    from sqlalchemy import select

    from app.features.execution.activity.models.payload_keys import OrgKeyBackup

    row = await session.scalar(
        select(OrgKeyBackup)
        .where(OrgKeyBackup.organization_id == user.organization_id)
        .order_by(OrgKeyBackup.created_at.desc())
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recovery backup not found")
    return keys.recovery_backup_view(row)


@router.get("")
async def payload_key_status(session: SessionDep, user: CurrentUser):
    key = await keys.active_key(session, user.organization_id)
    envelope = await keys.own_envelope(session, organization_id=user.organization_id, user_id=user.id)
    member = await keys._member(session, user.id, user.organization_id) if key else None
    own_key = await keys.own_member_key(session, organization_id=user.organization_id, user_id=user.id)
    return {
        "active_version": key.version if key else None,
        "public_key": key.public_key if key else None,
        "algorithm": key.algorithm if key else None,
        "owner_user_id": key.owner_user_id if key else None,
        "encryption_status": "active" if key else "unavailable",
        "member_envelope": envelope.wrapped_private_key if envelope and envelope.user_id == user.id else None,
        "member_key_id": envelope.member_key_id if envelope else None,
        "recovery_uploaded": key is not None,
        "historical_note": "A member who already exported the organization private key keeps that copy after revocation.",
        "role": member.role if member else None,
        "own_member_key_id": own_key.id if own_key else None,
        "own_member_key_approval": own_key.approval if own_key else None,
    }


@router.post("")
async def create_payload_key(body: CreateOrgKeyBody, session: SessionDep, user: CurrentUser, _team: TeamWrite):
    _no_secrets(body)
    row = await keys.create_org_key(
        session,
        organization_id=user.organization_id,
        owner_user_id=user.id,
        public_key=body.public_key,
        backup=body.backup.model_dump(),
    )
    return {"version": row.version, "public_key": row.public_key, "status": row.status}


@router.post("/member")
async def register_member(body: MemberKeyBody, session: SessionDep, user: CurrentUser):
    _no_secrets(body)
    row = await keys.register_member_key(
        session,
        organization_id=user.organization_id,
        user_id=user.id,
        public_key=body.public_key,
        backup=body.backup.model_dump() if body.backup is not None else None,
        needs_assist=body.needs_assist,
    )
    return {"id": row.id, "public_key": row.public_key, "approval": row.approval, "fingerprint": keys.key_fingerprint(row.public_key)}


@router.get("/members/pending")
async def pending_member_keys(session: SessionDep, user: CurrentUser, _team: TeamWrite):
    return {
        "keys": await keys.pending_member_keys(session, organization_id=user.organization_id, actor_user_id=user.id)
    }


@router.post("/member/{member_key_id}/approve")
async def approve_member_key(member_key_id: str, session: SessionDep, user: CurrentUser, _team: TeamWrite):
    row = await keys.decide_member_key(
        session,
        organization_id=user.organization_id,
        actor_user_id=user.id,
        member_key_id=member_key_id,
        approve=True,
    )
    return {"id": row.id, "user_id": row.user_id, "approval": row.approval}


@router.post("/member/{member_key_id}/reject")
async def reject_member_key(member_key_id: str, session: SessionDep, user: CurrentUser, _team: TeamWrite):
    row = await keys.decide_member_key(
        session,
        organization_id=user.organization_id,
        actor_user_id=user.id,
        member_key_id=member_key_id,
        approve=False,
    )
    return {"id": row.id, "user_id": row.user_id, "approval": row.approval}


@router.get("/audit")
async def key_audit(
    session: SessionDep,
    user: CurrentUser,
    _team: TeamWrite,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    rows = await keys.key_audit(session, organization_id=user.organization_id, actor_user_id=user.id, limit=limit, offset=offset)
    return {
        "events": [
            {
                "id": row.id,
                "event": row.event,
                "actor_id": row.actor_id,
                "result": row.result,
                "payload": row.payload,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]
    }


@router.post("/envelopes")
async def create_envelope(body: EnvelopeBody, session: SessionDep, user: CurrentUser, _team: TeamWrite):
    _no_secrets(body)
    row = await keys.add_envelope(
        session,
        organization_id=user.organization_id,
        actor_user_id=user.id,
        user_id=body.user_id,
        member_key_id=body.member_key_id,
        org_key_version=body.org_key_version,
        wrapped_private_key=body.wrapped_private_key,
    )
    return {"id": row.id, "user_id": row.user_id}


@router.get("/member/assist")
async def member_assist(session: SessionDep, user: CurrentUser):
    return {
        "requests": await keys.assist_requests(
            session,
            organization_id=user.organization_id,
            actor_user_id=user.id,
        )
    }


@router.get("/member/backup")
async def member_backup(session: SessionDep, user: CurrentUser):
    found = await keys.own_password_backup(session, organization_id=user.organization_id, user_id=user.id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Password backup not found")
    key, backup = found
    return keys.member_backup_view(key, backup)


@router.get("/member/rewrap")
async def member_rewrap_sources(
    member_key_id: str,
    session: SessionDep,
    user: CurrentUser,
    cursor: str | None = None,
    limit: int = Query(keys.SCAN_DEFAULT_LIMIT, ge=1, le=keys.SCAN_MAX_LIMIT),
):
    return await keys.wraps_missing_for(
        session,
        organization_id=user.organization_id,
        actor_user_id=user.id,
        member_key_id=member_key_id,
        cursor=cursor,
        limit=limit,
    )


class MemberRewrapBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    member_key_id: str
    records: list[RewrapItem] = Field(max_length=keys.SCAN_MAX_LIMIT)


@router.post("/member/rewrap")
async def member_rewrap(body: MemberRewrapBody, session: SessionDep, user: CurrentUser):
    _no_secrets(body)
    changed = await keys.grant_recipient_wraps(
        session,
        organization_id=user.organization_id,
        actor_user_id=user.id,
        member_key_id=body.member_key_id,
        records=[item.model_dump() for item in body.records],
    )
    return {"migrated": changed}


@router.get("/envelopes/{user_id}")
async def read_envelope(user_id: str, session: SessionDep, user: CurrentUser):
    if user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Envelope not found")
    row = await keys.own_envelope(session, organization_id=user.organization_id, user_id=user.id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Envelope not found")
    return {
        "id": row.id,
        "member_key_id": row.member_key_id,
        "org_key_version": row.org_key_version,
        "wrapped_private_key": row.wrapped_private_key,
    }


@router.post("/members/{user_id}/revoke")
async def revoke_member(user_id: str, session: SessionDep, user: CurrentUser, _team: TeamWrite):
    return await keys.revoke_member(session, organization_id=user.organization_id, actor_user_id=user.id, user_id=user_id)


@router.post("/rotate")
async def start_rotation(body: RotateStartBody, session: SessionDep, user: CurrentUser, _team: TeamWrite):
    job = await keys.start_rotation(session, organization_id=user.organization_id, actor_user_id=user.id, public_key=body.public_key)
    return {"id": job.id, "from_version": job.from_version, "to_version": job.to_version, "status": job.status}


@router.get("/rotate/pending")
async def rotation_pending(
    version: int,
    session: SessionDep,
    user: CurrentUser,
    _team: TeamWrite,
    cursor: str | None = None,
    limit: int = Query(keys.SCAN_DEFAULT_LIMIT, ge=1, le=keys.SCAN_MAX_LIMIT),
):
    return await keys.pending_wraps(session, user.organization_id, version, cursor=cursor, limit=limit)


@router.post("/rotate/batch")
async def rotate_batch(body: RewrapBody, session: SessionDep, user: CurrentUser, _team: TeamWrite):
    _no_secrets(body)
    changed = await keys.rewrap_batch(
        session,
        organization_id=user.organization_id,
        version=body.version,
        records=[item.model_dump() for item in body.records],
        actor_user_id=user.id,
    )
    return {"migrated": changed}


@router.post("/rotate/activate")
async def activate_rotation(body: ActivateBody, session: SessionDep, user: CurrentUser, _team: TeamWrite):
    row = await keys.activate_rotation(session, organization_id=user.organization_id, actor_user_id=user.id, backup=body.backup.model_dump())
    return {"version": row.version, "status": row.status}


@router.post("/transfer")
async def transfer_owner(body: TransferBody, session: SessionDep, user: CurrentUser, _team: TeamWrite):
    key = await keys.active_key(session, user.organization_id)
    if key is None or key.owner_user_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the payload owner can transfer ownership")
    envelope = await keys.own_envelope(session, organization_id=user.organization_id, user_id=body.user_id)
    if envelope is None or envelope.org_key_version != key.version:
        raise HTTPException(status.HTTP_409_CONFLICT, "The new owner has no envelope for the active key")
    await keys._member(session, body.user_id, user.organization_id)
    key.owner_user_id = body.user_id
    await session.flush()
    return {"owner_user_id": key.owner_user_id, "version": key.version}
