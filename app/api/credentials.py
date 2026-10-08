from fastapi import APIRouter, Response, status

from app.api.deps import CurrentUser, SessionDep
from app.models.credential import OrgCredential
from app.schemas import CredentialRead, CredentialSecretRead, CredentialWrite
from app.models.enums import ActorType, AuditEvent
from app.services.audit_service import audit_service
from app.services.credential_service import credential_service, open_secret
from app.services.secret_vault import get_secret, put_secret

router = APIRouter(prefix="/org/credentials", tags=["credentials"])


def serialize_credential(row: OrgCredential) -> CredentialRead:
    return CredentialRead(
        id=row.id,
        kind=row.kind,
        name=row.name,
        provider=row.provider,
        model=row.model_name,
        base_url=row.base_url,
        secret_name=row.secret_name,
        broker_id=row.broker_id,
        allowed_paths=list(row.allowed_paths or []),
        has_secret=bool(row.vault_id or row.secret_ciphertext),
    )


@router.get("", response_model=list[CredentialRead])
async def list_credentials(session: SessionDep, user: CurrentUser):
    rows = await credential_service.list_credentials(session, user.organization_id)
    return [serialize_credential(row) for row in rows]


@router.get("/secrets", response_model=list[CredentialSecretRead])
async def list_credential_secrets(session: SessionDep, user: CurrentUser):
    rows = await credential_service.list_credentials(session, user.organization_id)
    secrets: list[CredentialSecretRead] = []
    for row in rows:
        secret = ""
        if row.vault_id:
            secret = await get_secret(session, row.vault_id)
        elif row.secret_ciphertext:
            secret = open_secret(row.secret_ciphertext)
            if secret:
                vault_id = f"org-{row.id}"
                await put_secret(session, vault_id, secret)
                row.vault_id = vault_id
                row.secret_ciphertext = ""
        if not secret:
            continue
        await audit_service.log(
            session,
            event=AuditEvent.CREDENTIAL_ACCESSED,
            actor_type=ActorType.USER,
            actor_id=user.id,
            organization_id=user.organization_id,
            result=row.name,
            payload={"credential_id": row.vault_id or row.id, "purpose": "org_credential"},
        )
        secrets.append(CredentialSecretRead(id=row.id, secret=secret))
    return secrets


@router.post("", response_model=CredentialRead)
async def upsert_credential(body: CredentialWrite, session: SessionDep, user: CurrentUser):
    row = await credential_service.upsert(session, user, body)
    return serialize_credential(row)


@router.delete("/{credential_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_credential(credential_id: str, session: SessionDep, user: CurrentUser):
    await credential_service.delete(session, user, credential_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
