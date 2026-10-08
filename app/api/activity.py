from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import AgentsRead, CurrentUser, SessionDep
from app.models import AuditLog
from app.schemas import ActivityEventRead

router = APIRouter(prefix="/activity", tags=["activity"])


def recorded_by_saturn_mcp(entry: AuditLog) -> bool:
    payload = entry.payload if isinstance(entry.payload, dict) else {}
    return payload.get("source") == "saturn_mcp"


@router.get("", response_model=list[ActivityEventRead])
async def list_activity(session: SessionDep, user: CurrentUser, _access: AgentsRead, run_id: str | None = None):
    stmt = select(AuditLog).where(AuditLog.organization_id == user.organization_id).order_by(AuditLog.created_at.desc())
    if run_id:
        stmt = stmt.where(AuditLog.run_id == run_id)
    result = await session.execute(stmt)
    return [item for item in result.scalars().all() if recorded_by_saturn_mcp(item)]
