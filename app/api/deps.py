from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_session
from app.models import Employee, User
from app.services.access import allows
from app.services.mapping_service import mapping_service

SessionDep = Annotated[AsyncSession, Depends(get_session)]
_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    session: SessionDep,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> User:
    from app.services.auth_service import auth_service

    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = await auth_service.user_from_token(session, creds.credentials)
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid or expired session",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


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


async def require_platform_admin(user: CurrentUser) -> User:
    if not user.is_platform_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Platform admin required")
    return user


PlatformAdmin = Annotated[User, Depends(require_platform_admin)]
