from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from maple_chat.db.models import Base


def _database_identity(url: URL) -> tuple[str | None, int | None, str | None, str | None]:
    return (url.host, url.port, url.database, url.username)


def _destructive_test_database_url() -> str:
    if os.environ.get("G002_ALLOW_DESTRUCTIVE_TEST_DATABASE") != "1":
        raise RuntimeError("integration DB reset requires G002_ALLOW_DESTRUCTIVE_TEST_DATABASE=1")
    test_database_url = os.environ["G002_DATABASE_URL"]
    # A blank DATABASE_URL names no production database (compose supplies it per
    # service, so the host .env commonly leaves it empty); only a real value can
    # collide with the destructive test target.
    production_database_url = os.environ.get("DATABASE_URL")
    if production_database_url and _database_identity(
        make_url(test_database_url)
    ) == _database_identity(make_url(production_database_url)):
        raise RuntimeError("G002_DATABASE_URL matches DATABASE_URL; refusing destructive reset")
    return test_database_url


@pytest.fixture
async def factory() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(_destructive_test_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        table_names = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
        await connection.execute(sa.text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"))
    yield session_factory
    await engine.dispose()
