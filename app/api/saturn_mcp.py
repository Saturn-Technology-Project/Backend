from typing import Any

from fastapi import APIRouter, Request

from app.api.deps import CurrentUser, SaturnCloud, SessionDep
from app.services.saturn_mcp import saturn_mcp

router = APIRouter(tags=["saturn-mcp"])


@router.post("/mcp")
async def saturn_mcp_endpoint(request: Request, session: SessionDep, user: CurrentUser, _cloud: SaturnCloud) -> dict[str, Any]:
    try:
        message = await request.json()
    except Exception:
        message = {}
    if not isinstance(message, dict):
        message = {}
    return await saturn_mcp.handle(
        session,
        user,
        agent_id=request.headers.get("x-saturn-agent-id", ""),
        run_id=request.headers.get("x-saturn-run-id", ""),
        action_id=request.headers.get("x-saturn-action-id", ""),
        message=message,
    )
