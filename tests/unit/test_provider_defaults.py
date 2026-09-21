"""Remote GPU-backed sidecars are the only supported production providers."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from maple_chat.config import ProcessRole, Settings
from maple_chat.indexing.embedding import RemoteEmbeddingProvider
from maple_chat.retrieval.hybrid import RemoteReranker
from maple_chat.runtime import build_embedding_provider, build_reranker

DEFAULT_ENV = {
    "DISCORD_TOKEN": "discord-secret-canary",  # pragma: allowlist secret
    "DISCORD_GUILD_ID": "123456789",
    "DISCORD_OWNER_ID": "987654321",
    "DATABASE_URL": "opaque-database-secret-canary",  # pragma: allowlist secret
    "CRAWLER_USER_AGENT": "MapleChat/0.1 (+https://example.invalid/contact)",
    "CRAWLER_CONTACT": "operator@example.invalid",
    "PII_HASH_SALT": "pii-secret-canary",  # pragma: allowlist secret
}


def _default_settings() -> Settings:
    with patch.dict(os.environ, DEFAULT_ENV, clear=True):
        return Settings(role=ProcessRole.BOT)


def test_default_settings_select_remote_providers_without_explicit_env() -> None:
    settings = _default_settings()
    assert settings.embedding_provider == "remote"
    assert settings.reranker_provider == "remote"


@pytest.mark.asyncio
async def test_default_settings_build_remote_reranker() -> None:
    reranker = build_reranker(_default_settings())
    assert isinstance(reranker, RemoteReranker)
    await reranker.aclose()


@pytest.mark.asyncio
async def test_default_settings_build_remote_embedding_provider() -> None:
    provider = build_embedding_provider(_default_settings())
    assert isinstance(provider, RemoteEmbeddingProvider)
    await provider.aclose()
