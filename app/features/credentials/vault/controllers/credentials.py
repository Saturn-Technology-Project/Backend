from fastapi import APIRouter, HTTPException, Response, status

from app.features.identity.auth.controllers.deps import CurrentUser, SessionDep, TeamWrite
from app.features.credentials.vault.models.credential import OrgCredential
from app.schemas import CredentialRead, CredentialWrite
from app.features.credentials.vault.services.credential_service import credential_service

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


@router.get("/secrets")
async def list_credential_secrets(_user: CurrentUser):
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Secrets are not readable")


@router.post("", response_model=CredentialRead)
async def upsert_credential(body: CredentialWrite, session: SessionDep, user: CurrentUser, _access: TeamWrite):
    row = await credential_service.upsert(session, user, body)
    return serialize_credential(row)


@router.delete("/{credential_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_credential(credential_id: str, session: SessionDep, user: CurrentUser, _access: TeamWrite):
    await credential_service.delete(session, user, credential_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
