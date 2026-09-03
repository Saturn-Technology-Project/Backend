from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import (
    activity,
    admin,
    agents,
    approvals,
    auth,
    connectors,
    mcp_servers,
    org,
    payments,
    policies,
    runs,
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(org.router)
app.include_router(agents.router)
app.include_router(connectors.router)
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
    operational = "operational"
    return {
        "status": "ok",
        "services": [
            {"id": "runtime", "name": "Agent Runtime", "status": operational},
            {"id": "gateway", "name": "MCP Gateway", "status": operational},
            {"id": "policy", "name": "Policy Engine", "status": operational},
            {"id": "wallet", "name": "Wallet", "status": operational},
        ],
    }
