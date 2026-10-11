from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import get_settings
from app.features.agent.agents.routes import router as agents_router
from app.features.billing.payments.routes import router as payments_router
from app.features.billing.wallets.routes import router as wallets_router
from app.features.billing.x402.routes import router as x402_router
from app.features.credentials.vault.routes import router as credentials_router
from app.features.execution.activity.routes import router as activity_router
from app.features.execution.approvals.routes import router as approvals_router
from app.features.execution.runs.routes import router as runs_router
from app.features.identity.auth.routes import router as auth_router
from app.features.identity.oauth.routes import router as oauth_router
from app.features.mcp.saturn.routes import router as saturn_mcp_router
from app.features.mcp.servers.routes import router as mcp_servers_router
from app.features.organization.org.routes import router as org_router
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
    yield
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


@app.exception_handler(Exception)
async def unhandled_error(_request: Request, exc: Exception):
    if isinstance(exc, (HTTPException, StarletteHTTPException)):
        raise exc
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(org_router)
app.include_router(agents_router)
app.include_router(credentials_router)
app.include_router(mcp_servers_router)
app.include_router(oauth_router)
app.include_router(tool_sets_router)
app.include_router(saturn_mcp_router)
app.include_router(wallets_router)
app.include_router(runs_router)
app.include_router(tools_router)
app.include_router(policies_router)
app.include_router(approvals_router)
app.include_router(payments_router)
app.include_router(x402_router)
app.include_router(activity_router)


@app.get("/")
async def root():
    return {"name": "Saturn Backend", "version": "0.1.0"}


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
