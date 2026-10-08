from __future__ import annotations

from app.core.ids import utcnow
from app.models.org_app_mcp import OrgAppMcp
from app.services.app_mcp_service import clean_app_mcp
from sqlalchemy.ext.asyncio import AsyncSession


class OrgAppMcpService:
    async def read(self, session: AsyncSession, organization_id: str) -> dict:
        row = await session.get(OrgAppMcp, organization_id)
        if row is None:
            return {"servers": [], "attachments": {}}
        return {"servers": list(row.servers or []), "attachments": dict(row.attachments or {})}

    async def replace(self, session: AsyncSession, organization_id: str, body: dict) -> dict:
        cleaned = clean_app_mcp(body)
        row = await session.get(OrgAppMcp, organization_id)
        if row is None:
            row = OrgAppMcp(organization_id=organization_id, servers=[], attachments={})
            session.add(row)
        row.servers = cleaned["servers"]
        row.attachments = cleaned["attachments"]
        row.updated_at = utcnow()
        await session.flush()
        return cleaned


org_app_mcp_service = OrgAppMcpService()
