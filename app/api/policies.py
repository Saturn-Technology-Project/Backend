from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, PoliciesRead, PoliciesWrite, SaturnCloud, SessionDep
from app.schemas import (
    PolicyActivate,
    PolicyCreate,
    PolicyGenerate,
    PolicyOptimize,
    PolicyRead,
    PolicySimulate,
    PolicyUpdate,
)
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


@router.post("/generate", response_model=PolicyRead, status_code=201)
async def generate_policy(
    body: PolicyGenerate, session: SessionDep, user: CurrentUser, _access: PoliciesWrite, _cloud: SaturnCloud
):
    policy = await policy_service.generate_from_set(
        session, user.organization_id, body.tool_set_id, body.name, body.instructions
    )
    return serialize_policy(policy)


@router.post("", response_model=PolicyRead, status_code=201)
async def create_policy(
    body: PolicyCreate, session: SessionDep, user: CurrentUser, _access: PoliciesWrite, _cloud: SaturnCloud
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
    _cloud: SaturnCloud,
):
    policy = await policy_service.get_policy(session, policy_id, user.organization_id)
    policy = await policy_service.update_policy(session, policy, body)
    return serialize_policy(policy)


@router.delete("/{policy_id}", status_code=204)
async def delete_policy(
    policy_id: str, session: SessionDep, user: CurrentUser, _access: PoliciesWrite, _cloud: SaturnCloud
):
    policy = await policy_service.get_policy(session, policy_id, user.organization_id)
    await policy_service.delete_policy(session, policy)


@router.post("/{policy_id}/validate")
async def validate_policy(
    policy_id: str, session: SessionDep, user: CurrentUser, _access: PoliciesWrite, _cloud: SaturnCloud
):
    policy = await policy_service.get_policy(session, policy_id, user.organization_id)
    report = await policy_service.validate_policy(session, policy)
    fresh = await policy_service.get_policy(session, policy_id, user.organization_id)
    return {"report": report, "policy": serialize_policy(fresh)}


@router.post("/{policy_id}/activate", response_model=PolicyRead)
async def activate_policy(
    policy_id: str,
    body: PolicyActivate,
    session: SessionDep,
    user: CurrentUser,
    _access: PoliciesWrite,
    _cloud: SaturnCloud,
):
    policy = await policy_service.get_policy(session, policy_id, user.organization_id)
    policy = await policy_service.activate_policy(session, policy, body.confirm)
    return serialize_policy(policy)


@router.post("/{policy_id}/optimize")
async def optimize_policy(
    policy_id: str,
    body: PolicyOptimize,
    session: SessionDep,
    user: CurrentUser,
    _access: PoliciesWrite,
    _cloud: SaturnCloud,
):
    policy = await policy_service.get_policy(session, policy_id, user.organization_id)
    return await policy_service.optimize_policy(session, policy, body.definition, body.instructions)


@router.post("/{policy_id}/simulate")
async def simulate_policy(
    policy_id: str,
    body: PolicySimulate,
    session: SessionDep,
    user: CurrentUser,
    _access: PoliciesRead,
):
    policy = await policy_service.get_policy(session, policy_id, user.organization_id)
    if not body.tool.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Tool is required")
    return await policy_service.simulate_policy(policy, body.tool.strip(), body.arguments, body.agent_id)
