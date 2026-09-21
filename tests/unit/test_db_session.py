from __future__ import annotations

import pytest

from maple_chat.config import ProcessRole, Settings
from maple_chat.db.session import create_engine, create_session_factory


@pytest.mark.asyncio
async def test_async_engine_and_session_factory_are_secret_safe_and_nonexpiring() -> None:
    settings = Settings(
        role=ProcessRole.SCHEDULER,
        database_url="postgresql+asyncpg://maple_chat@localhost/maple_chat",
    )
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    assert engine.echo is False
    assert factory.kw["expire_on_commit"] is False
    await engine.dispose()
