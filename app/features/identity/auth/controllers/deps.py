from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.features.identity.auth.controllers.session_cookie import COOKIE_NAME
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_session
from app.models import Employee, Organization, User
from app.features.organization.org.services.access import allows
from app.features.organization.org.services.mapping_service import mapping_service
from app.features.organization.org.services.plans import PRO_REQUIRED, allows_pro, allows_saturn_cloud

SessionDep = Annotated[AsyncSession, Depends(get_session)]
_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request,
    session: SessionDep,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> User:
    from app.features.identity.auth.services.auth_service import auth_service

    token = ""
    if creds is not None and creds.scheme.lower() == "bearer":
        token = creds.credentials
    if not token:
        token = request.cookies.get(COOKIE_NAME, "")
    if not token:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = await auth_service.user_from_token(session, token)
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid or expired session",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not _totp_exempt(request.url.path) and await _admin_needs_totp(session, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "MFA setup is required")
    return user


def _totp_exempt(path: str) -> bool:
    if path in {"/", "/health", "/healthz", "/auth/me"}:
        return True
    return path.startswith("/auth/logout") or path.startswith("/auth/mfa") or path.startswith("/auth/totp")


async def _admin_needs_totp(session: AsyncSession, user: User) -> bool:
    if user.totp_enabled:
        return False
    if user.is_platform_admin:
        return True
    from app.features.organization.org.services.access import is_org_admin

    employee = await session.scalar(select(Employee).where(Employee.user_id == user.id))
    return employee is not None and is_org_admin(employee.permissions)


CurrentUser = Annotated[User, Depends(get_current_user)]
AuthRequired = [Depends(get_current_user)]


def get_actor_id(user: CurrentUser) -> str:
    return user.id


ActorDep = Annotated[str, Depends(get_actor_id)]


def require_access(resource: str, level: str):
    async def _check(session: SessionDep, user: CurrentUser) -> Employee:
        employee = await mapping_service.require_employee(session, user)
        if not allows(employee.permissions, resource, level):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"Missing {resource}.{level} access",
            )
        return employee

    return _check


AgentsRead = Annotated[Employee, Depends(require_access("agents", "read"))]
AgentsWrite = Annotated[Employee, Depends(require_access("agents", "write"))]
PoliciesRead = Annotated[Employee, Depends(require_access("policies", "read"))]
PoliciesWrite = Annotated[Employee, Depends(require_access("policies", "write"))]
ApprovalsRead = Annotated[Employee, Depends(require_access("approvals", "read"))]
ApprovalsWrite = Annotated[Employee, Depends(require_access("approvals", "write"))]
TeamRead = Annotated[Employee, Depends(require_access("team", "read"))]
TeamWrite = Annotated[Employee, Depends(require_access("team", "write"))]


async def require_saturn_cloud(session: SessionDep, user: CurrentUser) -> Organization:
    org = await session.get(Organization, user.organization_id)
    if org is None or not allows_saturn_cloud(org.plan):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Saturn MCP is unavailable")
    return org


SaturnCloud = Annotated[Organization, Depends(require_saturn_cloud)]


async def require_pro(session: SessionDep, user: CurrentUser) -> Organization:
    org = await session.get(Organization, user.organization_id)
    if org is None or not allows_pro(org.plan):
        raise HTTPException(status.HTTP_403_FORBIDDEN, PRO_REQUIRED)
    return org


ProPlan = Annotated[Organization, Depends(require_pro)]


async def require_platform_admin(user: CurrentUser) -> User:
    if not user.is_platform_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Platform admin required")
    return user


PlatformAdmin = Annotated[User, Depends(require_platform_admin)]
