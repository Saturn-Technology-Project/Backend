from fastapi import APIRouter, Request

from app.api.deps import CurrentUser, SessionDep
from app.core.config import public_ws_url
from app.schemas import SessionPairResponse
from app.services.connector_service import connector_service

router = APIRouter(prefix="/session", tags=["session"])


def absolute_ws_url(request: Request, path: str) -> str:
    configured = public_ws_url(path)
    if configured.startswith(("ws://", "wss://")):
        return configured
    forwarded = (request.headers.get("x-forwarded-proto") or request.url.scheme or "http").split(",")[0].strip()
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
    ).split(",")[0].strip()
    scheme = "wss" if forwarded == "https" else "ws"
    suffix = path if path.startswith("/") else f"/{path}"
    return f"{scheme}://{host}{suffix}"


@router.post("/pair", response_model=SessionPairResponse)
async def pair_session(request: Request, session: SessionDep, user: CurrentUser):
    issued = await connector_service.issue_token(session, user.organization_id)
    ws_url = issued.ws_url
    if not ws_url.startswith(("ws://", "wss://")):
        ws_url = absolute_ws_url(request, ws_url)
    return SessionPairResponse(
        session_id=issued.connector_id,
        token=issued.token,
        ws_url=ws_url,
        organization_id=user.organization_id,
    )
