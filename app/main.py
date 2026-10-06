from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import (
    activity,
    admin,
    agents,
    approvals,
    auth,
    connectors,
    credentials,
    mcp_servers,
    org,
    payments,
    policies,
    runs,
    session,
    tools,
    wallets,
    ws,
    x402,
)
from app.core.config import get_settings
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
    description="MVP execution loop: Run → Agent → Tool → Policy → Approval → Wallet/x402 → Execute → Audit",
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


app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(org.router)
app.include_router(agents.router)
app.include_router(connectors.router)
app.include_router(credentials.router)
app.include_router(session.router)
app.include_router(mcp_servers.router)
app.include_router(wallets.router)
app.include_router(runs.router)
app.include_router(tools.router)
app.include_router(policies.router)
app.include_router(approvals.router)
app.include_router(payments.router)
app.include_router(x402.router)
app.include_router(activity.router)
app.include_router(ws.router)


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
