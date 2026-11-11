import os
import sys
import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from app.models import Base

# Ensure root workspace directory is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture
async def test_engine():
    """Isolated async SQLite in-memory engine with PostgreSQL advisory lock mock registered."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    @event.listens_for(engine.sync_engine, "connect")
    def register_sqlite_functions(dbapi_conn, record):
        # Register PostgreSQL pg_advisory_xact_lock function on SQLite connection
        dbapi_conn.create_function("pg_advisory_xact_lock", 1, lambda x: 1)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine
    await engine.dispose()


@pytest.fixture
async def test_session_maker(test_engine):
    """Session factory for testing async database transactions."""
    return async_sessionmaker(
        bind=test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
