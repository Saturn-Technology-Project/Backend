from __future__ import annotations

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from app.features.identity.auth.controllers.deps import ActorDep, CurrentUser, SessionDep, TeamRead, TeamWrite
from app.features.organization.org.models.group import Group
from app.features.organization.org.services.group_service import group_service

router = APIRouter(prefix="/org/groups", tags=["groups"])


class GroupMemberRead(BaseModel):
    employee_id: str


class GroupRead(BaseModel):
    id: str
    name: str
    description: str
    status: str
    members: list[GroupMemberRead]


class GroupWrite(BaseModel):
    name: str = Field(max_length=255)
    description: str = Field(default="", max_length=20_000)


class GroupUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=20_000)
    status: str | None = Field(default=None, max_length=32)


class GroupMemberWrite(BaseModel):
    employee_id: str = Field(max_length=64)


def serialize_group(group: Group) -> GroupRead:
    return GroupRead(
        id=group.id,
        name=group.name,
        description=group.description or "",
        status=group.status,
        members=[GroupMemberRead(employee_id=item.employee_id) for item in group.members],
    )


@router.get("", response_model=list[GroupRead])
async def list_groups(session: SessionDep, user: CurrentUser, _access: TeamRead):
    groups = await group_service.list_groups(session, user.organization_id)
    return [serialize_group(item) for item in groups]


@router.post("", response_model=GroupRead, status_code=201)
async def create_group(
    body: GroupWrite,
    session: SessionDep,
    user: CurrentUser,
    actor_id: ActorDep,
    _access: TeamWrite,
):
    group = await group_service.create_group(
        session,
        user.organization_id,
        actor_id=actor_id,
        name=body.name,
        description=body.description,
    )
    return serialize_group(await group_service.get_group(session, user.organization_id, group.id))


@router.get("/{group_id}", response_model=GroupRead)
async def get_group(group_id: str, session: SessionDep, user: CurrentUser, _access: TeamRead):
    return serialize_group(await group_service.get_group(session, user.organization_id, group_id))


@router.patch("/{group_id}", response_model=GroupRead)
async def update_group(
    group_id: str,
    body: GroupUpdate,
    session: SessionDep,
    user: CurrentUser,
    actor_id: ActorDep,
    _access: TeamWrite,
):
    group = await group_service.update_group(
        session,
        user.organization_id,
        group_id,
        actor_id=actor_id,
        name=body.name,
        description=body.description,
        status_name=body.status,
    )
    return serialize_group(group)


@router.post("/{group_id}/members", response_model=GroupRead)
async def add_group_member(
    group_id: str,
    body: GroupMemberWrite,
    session: SessionDep,
    user: CurrentUser,
    actor_id: ActorDep,
    _access: TeamWrite,
):
    group = await group_service.add_member(
        session,
        user.organization_id,
        group_id,
        body.employee_id,
        actor_id=actor_id,
    )
    return serialize_group(group)


@router.delete("/{group_id}/members/{employee_id}", response_model=GroupRead)
async def remove_group_member(
    group_id: str,
    employee_id: str,
    session: SessionDep,
    user: CurrentUser,
    actor_id: ActorDep,
    _access: TeamWrite,
):
    group = await group_service.remove_member(
        session,
        user.organization_id,
        group_id,
        employee_id,
        actor_id=actor_id,
    )
    if group is None:
        return Response(status_code=204)
    return serialize_group(group)


@router.delete("/{group_id}", status_code=204)
async def delete_group(
    group_id: str,
    session: SessionDep,
    user: CurrentUser,
    actor_id: ActorDep,
    _access: TeamWrite,
):
    await group_service.delete_group(
        session,
        user.organization_id,
        group_id,
        actor_id=actor_id,
    )
