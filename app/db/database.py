from collections.abc import AsyncGenerator

from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


def engine_options(url: str, settings) -> dict:
    """Keyword arguments for create_async_engine. Separate from the engine so tests can read them."""
    kwargs: dict = {"echo": False}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        return kwargs
    kwargs.update(
        pool_size=max(1, int(settings.db_pool_size)),
        max_overflow=max(0, int(settings.db_max_overflow)),
        pool_timeout=float(settings.db_pool_timeout),
        pool_recycle=int(settings.db_pool_recycle),
        # A connection the database or a proxy dropped is replaced instead of failing a request.
        pool_pre_ping=True,
    )
    if url.startswith("postgresql+asyncpg") and settings.db_command_timeout > 0:
        kwargs["connect_args"] = {"command_timeout": float(settings.db_command_timeout)}
    return kwargs


def _build_engine(url: str):
    return create_async_engine(url, **engine_options(url, get_settings()))


settings = get_settings()
engine = _build_engine(settings.database_url)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

if settings.database_url.startswith("sqlite"):

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_sqlite_fk(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        try:
            yield session
        except HTTPException:
            await session.commit()
            raise
        except Exception:
            await session.rollback()
            raise
        else:
            try:
                await session.commit()
            except Exception:
                await session.rollback()
                raise
