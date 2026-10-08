from __future__ import annotations

from app.core.ids import utcnow
from app.models.org_app_state import OrgAppState
from app.models.user import User
from app.services.access import allows
from app.services.app_state_service import clean_agents
from app.services.mapping_service import mapping_service
from sqlalchemy.ext.asyncio import AsyncSession


class OrgAppStateService:
    async def read(self, session: AsyncSession, organization_id: str) -> dict:
        row = await session.get(OrgAppState, organization_id)
        if row is None:
            return {"agents": []}
        return {"agents": list(row.agents or [])}

    async def agent_ids(self, session: AsyncSession, organization_id: str) -> set[str]:
        body = await self.read(session, organization_id)
        return {str(item.get("id")) for item in body["agents"] if isinstance(item, dict) and item.get("id")}

    async def can_write(self, session: AsyncSession, user: User) -> bool:
        employee = await mapping_service.employee_for_user(session, user.id)
        if employee is None or employee.status != "active":
            return False
        return allows(employee.permissions, "agents", "write") or allows(employee.permissions, "team", "write")

    async def replace(self, session: AsyncSession, organization_id: str, body: dict) -> dict:
        agents = clean_agents(body.get("agents"), "team")
        row = await session.get(OrgAppState, organization_id)
        if row is None:
            row = OrgAppState(organization_id=organization_id, agents=[])
            session.add(row)
        row.agents = agents
        row.updated_at = utcnow()
        await session.flush()
        return {"agents": agents}


org_app_state_service = OrgAppStateService()
