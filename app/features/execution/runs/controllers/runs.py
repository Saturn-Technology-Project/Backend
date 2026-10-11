from __future__ import annotations

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from app.features.identity.auth.controllers.deps import ActorDep, AgentsRead, AgentsWrite, CurrentUser, SessionDep
from app.features.identity.auth.services.notices import finish_task_notice
from app.schemas import ActionRead, RunCreate, RunRead
from app.features.execution.runs.services.run_service import pending_approval_id, run_service

router = APIRouter(prefix="/runs", tags=["runs"])


def serialize_run(run) -> RunRead:
    return RunRead(
        id=run.id,
        agent_id=run.agent_id,
        input=run.input,
        status=run.status,
        approval_id=pending_approval_id(run),
        actions=[ActionRead.model_validate(action) for action in run.actions],
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


@router.get("", response_model=list[RunRead])
async def list_runs(session: SessionDep, user: CurrentUser, _access: AgentsRead):
    runs = await run_service.list_runs(session, user)
    return [serialize_run(run) for run in runs]


@router.get("/{run_id}", response_model=RunRead)
async def get_run(run_id: str, session: SessionDep, user: CurrentUser, _access: AgentsRead):
    run = await run_service.get_run(session, user, run_id)
    return serialize_run(run)


class TaskNotice(BaseModel):
    status: str = Field(pattern="^(completed|failed)$")
    summary: str = ""


@router.post("/{run_id}/notice", status_code=204)
async def finish_task(run_id: str, body: TaskNotice, session: SessionDep, user: CurrentUser, _access: AgentsRead):
    await finish_task_notice(session, user.organization_id, run_id, body.status, body.summary)
    return Response(status_code=204)


@router.post("", response_model=RunRead, status_code=201)
async def create_run(body: RunCreate, session: SessionDep, actor_id: ActorDep, user: CurrentUser, _access: AgentsWrite):
    run = await run_service.create_run(
        session,
        user,
        agent_id=body.agent_id,
        prompt=body.input,
        actor_id=actor_id,
    )
    return serialize_run(run)
