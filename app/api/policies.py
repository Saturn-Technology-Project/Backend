from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, PoliciesRead, PoliciesWrite, SessionDep
from app.schemas import PolicyCreate, PolicyRead, PolicyUpdate
from app.services.policy_service import policy_service, serialize_policy

router = APIRouter(prefix="/policies", tags=["policies"])


@router.get("", response_model=list[PolicyRead])
async def list_policies(session: SessionDep, user: CurrentUser, _access: PoliciesRead):
    policies = await policy_service.list_policies(session, user.organization_id)
    return [serialize_policy(policy) for policy in policies]


@router.get("/{policy_id}", response_model=PolicyRead)
async def get_policy(policy_id: str, session: SessionDep, user: CurrentUser, _access: PoliciesRead):
    policy = await policy_service.get_policy(session, policy_id, user.organization_id)
    return serialize_policy(policy)


@router.post("", response_model=PolicyRead, status_code=201)
async def create_policy(
    body: PolicyCreate, session: SessionDep, user: CurrentUser, _access: PoliciesWrite
):
    if not body.name.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Name is required")
    policy = await policy_service.create_policy(session, user.organization_id, body)
    return serialize_policy(policy)


@router.patch("/{policy_id}", response_model=PolicyRead)
async def update_policy(
    policy_id: str,
    body: PolicyUpdate,
    session: SessionDep,
    user: CurrentUser,
    _access: PoliciesWrite,
):
    policy = await policy_service.get_policy(session, policy_id, user.organization_id)
    policy = await policy_service.update_policy(session, policy, body)
    return serialize_policy(policy)
