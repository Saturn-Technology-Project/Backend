import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import get_settings
from app.core.http_security import HttpSecurityMiddleware
from app.core.origin_auth import OriginAuthMiddleware
from app.features.agent.agents.routes import router as agents_router
from app.features.billing.payments.routes import router as payments_router
from app.features.credentials.vault.routes import router as credentials_router
from app.features.execution.activity.routes import router as activity_router
from app.features.execution.activity.controllers.payload_keys import router as payload_keys_router
from app.features.execution.approvals.controllers.approval_policies import router as approval_policies_router
from app.features.execution.approvals.routes import router as approvals_router
from app.features.execution.runs.routes import router as runs_router
from app.features.identity.auth.routes import router as auth_router
from app.features.identity.oauth.routes import router as oauth_router
from app.features.mcp.saturn.routes import router as saturn_mcp_router
from app.features.mcp.servers.routes import router as mcp_servers_router
from app.features.organization.org.controllers.groups import router as groups_router
from app.features.organization.org.routes import router as org_router
from app.features.webhooks.controllers.webhooks import router as webhooks_router
from app.features.platform.admin.routes import router as admin_router
from app.features.policy.policies.routes import router as policies_router
from app.features.tooling.tool_sets.routes import router as tool_sets_router
from app.features.tooling.tools.routes import router as tools_router
from app.db.database import SessionLocal, engine
from app.db.seed import seed_if_empty
from app.models import *  # noqa: F401,F403

settings = get_settings()


def _cors_origins() -> list[str]:
    raw = (settings.cors_origins or "").strip()
    if not raw:
        return ["http://localhost:3000", "http://localhost:3001"]
    if raw == "*":
        return ["*"]
    return [item.strip() for item in raw.split(",") if item.strip()]


def _sqlite() -> bool:
    return settings.database_url.startswith("sqlite")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from app.features.credentials.vault.services.pq_crypto import require_root

    require_root()
    from app.features.mcp.saturn.services.mcp_sessions import mcp_sessions

    await mcp_sessions.start()
    worker = None
    try:
        if not settings.skip_db_bootstrap:
            # Postgres schema is owned by Alembic (`alembic upgrade head` in Docker).
            # create_all races with those migrations and crashes on existing tables.
            if _sqlite():
                from app.db.database import Base

                async with engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
            async with SessionLocal() as session:
                await seed_if_empty(session)
                await session.commit()
        if settings.saturn_embed_worker and not settings.skip_db_bootstrap:
            from app.features.webhooks.services.worker import run_webhook_worker

            worker = asyncio.create_task(run_webhook_worker())
        yield
    finally:
        if worker is not None:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
        await mcp_sessions.stop()
        await engine.dispose()


app = FastAPI(
    title="Saturn Backend",
    version="0.1.0",
    description="Saturn MCP gateway and team permissions",
    lifespan=lifespan,
)

_origins = _cors_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials="*" not in _origins,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(HttpSecurityMiddleware)
app.add_middleware(OriginAuthMiddleware)


@app.exception_handler(Exception)
async def unhandled_error(_request: Request, exc: Exception):
    if isinstance(exc, (HTTPException, StarletteHTTPException)):
        raise exc
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(org_router)
app.include_router(webhooks_router)
app.include_router(groups_router)
app.include_router(approval_policies_router)
app.include_router(agents_router)
app.include_router(credentials_router)
app.include_router(mcp_servers_router)
app.include_router(oauth_router)
app.include_router(tool_sets_router)
app.include_router(saturn_mcp_router)
app.include_router(runs_router)
app.include_router(tools_router)
app.include_router(policies_router)
app.include_router(approvals_router)
app.include_router(payments_router)
app.include_router(activity_router)
app.include_router(payload_keys_router)


@app.get("/")
async def root():
    return {"status": "ok"}


@app.get("/healthz")
async def healthz():
    return await health()


@app.get("/health")
async def health():
    db_status = "down"
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        db_status = "operational"
    except Exception:
        db_status = "down"
    ok = db_status == "operational"
    return {
        "status": "ok" if ok else "error",
        "services": [
            {"id": "api", "name": "API", "status": "operational"},
            {"id": "database", "name": "Database", "status": db_status},
        ],
    }
