from fastapi import APIRouter

from app.core.enums import AuditEvent
from app.features.execution.activity.services.change_log import record_change
from app.features.identity.auth.controllers.deps import CurrentUser, PoliciesRead, PoliciesWrite, SaturnCloud, SessionDep
from app.models import ToolSet
from app.schemas import ToolSetItemRead, ToolSetRead, ToolSetUpdate, ToolSetWrite
from app.features.tooling.tool_sets.services.tool_set_service import tool_set_service
from app.shared.pagination import Page

router = APIRouter(prefix="/tool-sets", tags=["tool-sets"])


def serialize_tool_set(tool_set: ToolSet) -> ToolSetRead:
    items: list[ToolSetItemRead] = []
    for item in tool_set.items or []:
        tool = item.tool
        if tool is None or tool.status != "active":
            continue
        server = tool.mcp_server
        items.append(
            ToolSetItemRead(
                tool_id=tool.id,
                name=tool.name,
                description=tool.description or "",
                server_id=tool.mcp_server_id,
                server_name=server.name if server is not None else "",
                approval=bool(item.approval),
                input_schema=tool.input_schema or {"type": "object", "properties": {}},
            )
        )
    items.sort(key=lambda item: (item.server_name, item.name))
    return ToolSetRead(
        id=tool_set.id,
        name=tool_set.name,
        description=tool_set.description or "",
        items=items,
    )


@router.get("", response_model=list[ToolSetRead])
async def list_tool_sets(session: SessionDep, user: CurrentUser, _access: PoliciesRead, page: Page):
    sets = await tool_set_service.list_sets(
        session, user.organization_id, limit=page.limit, offset=page.offset
    )
    return [serialize_tool_set(item) for item in sets]


@router.post("", response_model=ToolSetRead, status_code=201)
async def create_tool_set(body: ToolSetWrite, session: SessionDep, user: CurrentUser, _access: PoliciesWrite, _cloud: SaturnCloud):
    tool_set = await tool_set_service.create(
        session,
        user,
        name=body.name,
        description=body.description,
        items=[item.model_dump() for item in body.items],
    )
    await _record(session, user, AuditEvent.TOOL_SET_CREATED, tool_set)
    return serialize_tool_set(tool_set)


async def _record(session, user, event: AuditEvent, tool_set: ToolSet, **details) -> None:
    await record_change(
        session,
        user,
        event,
        result=tool_set.name,
        tool_set_id=tool_set.id,
        # Which tools the set exposes, and which of them always ask for approval.
        tools=sorted(item.tool.name for item in tool_set.items or [] if item.tool is not None),
        approval_tools=sorted(
            item.tool.name for item in tool_set.items or [] if item.tool is not None and item.approval
        ),
        **details,
    )


@router.patch("/{tool_set_id}", response_model=ToolSetRead)
async def update_tool_set(
    tool_set_id: str,
    body: ToolSetUpdate,
    session: SessionDep,
    user: CurrentUser,
    _access: PoliciesWrite,
    _cloud: SaturnCloud,
):
    tool_set = await tool_set_service.update(
        session,
        user,
        tool_set_id,
        name=body.name,
        description=body.description,
        items=None if body.items is None else [item.model_dump() for item in body.items],
    )
    await _record(session, user, AuditEvent.TOOL_SET_UPDATED, tool_set, changed=sorted(body.model_fields_set))
    return serialize_tool_set(tool_set)


@router.delete("/{tool_set_id}", status_code=204)
async def delete_tool_set(tool_set_id: str, session: SessionDep, user: CurrentUser, _access: PoliciesWrite, _cloud: SaturnCloud):
    removed = await tool_set_service.delete(session, user, tool_set_id)
    await record_change(
        session, user, AuditEvent.TOOL_SET_DELETED, result=removed["name"], tool_set_id=tool_set_id, agent_ids=removed["agent_ids"]
    )
