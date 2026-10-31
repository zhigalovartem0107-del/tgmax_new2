"""Async engine, session factory and schema bootstrap."""

from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import settings
from database.models import Base

_url = make_url(settings.database_url)
_is_sqlite = _url.get_backend_name() == "sqlite"

if _is_sqlite and _url.database not in (None, "", ":memory:"):
    Path(_url.database).parent.mkdir(parents=True, exist_ok=True)

engine = create_async_engine(
    _url,
    echo=settings.database_echo,
    pool_pre_ping=not _is_sqlite,
)

if _is_sqlite:

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")  # readers don't block the writer
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


# expire_on_commit=False: objects stay readable after commit without lazy IO,
# which async sessions cannot do implicitly.
session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    """Create missing tables. Replace with Alembic migrations once the schema settles."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    await engine.dispose()
