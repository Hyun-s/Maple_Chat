from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from maple_chat.config import ConfigurationError, ProcessRole, Settings, load_settings

VALID_ENV = {
    "DISCORD_TOKEN": "discord-secret-canary",  # pragma: allowlist secret
    "DISCORD_GUILD_ID": "123456789",
    "DISCORD_OWNER_ID": "987654321",
    "DATABASE_URL": "opaque-database-secret-canary",  # pragma: allowlist secret
    "CRAWLER_USER_AGENT": "MapleChat/0.1 (+https://example.invalid/contact)",
    "CRAWLER_CONTACT": "operator@example.invalid",
    "PII_HASH_SALT": "pii-secret-canary",  # pragma: allowlist secret
    "RERANKER_PROVIDER": "remote",
}


def test_required_settings_load_without_exposing_secrets() -> None:
    with patch.dict(os.environ, VALID_ENV, clear=True):
        settings = load_settings()

    rendered = f"{settings!r} {settings} {settings.safe_summary()}"
    assert "discord-secret-canary" not in rendered
    assert "database-secret-canary" not in rendered
    assert "pii-secret-canary" not in rendered
    assert rendered.count("<redacted>") >= 3
    assert settings.live_crawl_enabled is False
    assert settings.llm_base_url == "http://127.0.0.1:8001/v1"
    assert settings.llm_model == "local-coder"
    assert settings.embedding_device == "auto"
    assert settings.embedding_provider == "remote"
    assert settings.embedding_base_url == "http://127.0.0.1:8081/v1"
    assert settings.embedding_remote_model == "bge-m3"
    assert settings.nexon_api_key is None
    assert settings.agent_max_tool_calls == 4


def test_nexon_api_key_is_optional_and_redacted() -> None:
    env = VALID_ENV | {
        "NEXON_API_KEY": "nexon-secret-canary"  # pragma: allowlist secret
    }
    with patch.dict(os.environ, env, clear=True):
        settings = load_settings()

    assert settings.nexon_api_key is not None
    assert "nexon-secret-canary" not in repr(settings)
    assert settings.safe_summary()["nexon_api_key"] == "<configured>"


def test_missing_settings_fail_with_field_names_only() -> None:
    with patch.dict(os.environ, {}, clear=True), pytest.raises(ConfigurationError) as raised:
        load_settings(ProcessRole.CRAWLER_WORKER)

    message = str(raised.value)
    assert "DATABASE_URL" in message
    assert "input_value" not in message


def test_missing_role_settings_fail_with_field_names_only() -> None:
    with (
        patch.dict(os.environ, {"DATABASE_URL": "opaque-db-canary"}, clear=True),
        pytest.raises(ConfigurationError) as raised,
    ):
        load_settings(ProcessRole.CRAWLER_WORKER)

    message = str(raised.value)
    assert "PII_HASH_SALT" in message
    assert "CRAWLER_USER_AGENT" in message
    assert "CRAWLER_CONTACT" not in message
    assert "opaque-db-canary" not in message


def test_invalid_secret_bearing_configuration_never_echoes_inputs() -> None:
    invalid = VALID_ENV | {
        "DISCORD_GUILD_ID": "not-an-integer",
        "DISCORD_TOKEN": "should-never-appear",  # pragma: allowlist secret
        "DATABASE_URL": "database-url-should-never-appear",  # pragma: allowlist secret
    }
    with patch.dict(os.environ, invalid, clear=True), pytest.raises(ConfigurationError) as raised:
        load_settings()

    message = str(raised.value)
    assert "should-never-appear" not in message
    assert "database-url-should-never-appear" not in message
    assert "DISCORD_GUILD_ID" in message


def test_live_crawl_requires_an_approval_file_setting(tmp_path: Path) -> None:
    env = VALID_ENV | {"LIVE_CRAWL_ENABLED": "true"}
    with patch.dict(os.environ, env, clear=True), pytest.raises(ConfigurationError) as raised:
        load_settings(ProcessRole.CRAWLER_WORKER)

    assert "LIVE_CRAWL_APPROVAL_FILE" in str(raised.value)

    approval_path = tmp_path / "approval.json"
    env["LIVE_CRAWL_APPROVAL_FILE"] = str(approval_path)
    with patch.dict(os.environ, env, clear=True):
        settings = Settings()
    assert settings.live_crawl_approval_file == approval_path


def test_raw_validation_error_masks_secrets() -> None:
    env = VALID_ENV | {"LIVE_CRAWL_ENABLED": "true"}
    with patch.dict(os.environ, env, clear=True), pytest.raises(ValidationError) as raised:
        Settings()

    rendered = str(raised.value)
    assert VALID_ENV["DISCORD_TOKEN"] not in rendered
    assert VALID_ENV["DATABASE_URL"] not in rendered
    assert VALID_ENV["PII_HASH_SALT"] not in rendered


def test_scheduler_does_not_require_discord_or_pii_secrets() -> None:
    with patch.dict(os.environ, {"DATABASE_URL": "scheduler-db-canary"}, clear=True):
        settings = load_settings(ProcessRole.SCHEDULER)

    assert settings.discord_token is None
    assert settings.pii_hash_salt is None


def test_cloud_llm_endpoint_is_rejected_without_echoing_it() -> None:
    cloud_url = "https://api.openai.com/v1"
    env = VALID_ENV | {"LLM_BASE_URL": cloud_url}
    with patch.dict(os.environ, env, clear=True), pytest.raises(ConfigurationError) as raised:
        load_settings()

    message = str(raised.value)
    assert "LLM_BASE_URL" in message
    assert cloud_url not in message


def test_remote_embedding_endpoint_is_loopback_only() -> None:
    env = VALID_ENV | {
        "EMBEDDING_PROVIDER": "remote",
        "EMBEDDING_BASE_URL": "http://dcm-embedding:80/v1",
        "EMBEDDING_REMOTE_MODEL": "bge-m3",
    }
    with patch.dict(os.environ, env, clear=True):
        settings = load_settings()

    assert settings.embedding_provider == "remote"
    assert settings.embedding_base_url == "http://dcm-embedding:80/v1"
    assert settings.embedding_remote_model == "bge-m3"

    cloud_url = "https://example.com/v1"
    with (
        patch.dict(os.environ, VALID_ENV | {"EMBEDDING_BASE_URL": cloud_url}, clear=True),
        pytest.raises(ConfigurationError) as raised,
    ):
        load_settings()

    assert "EMBEDDING_BASE_URL" in str(raised.value)
    assert cloud_url not in str(raised.value)


def test_discord_channel_ids_and_model_revisions_are_operationally_pinned() -> None:
    env = VALID_ENV | {"DISCORD_CHANNEL_IDS": "10, 20,30"}
    with patch.dict(os.environ, env, clear=True):
        settings = load_settings()

    assert settings.discord_channel_ids == (10, 20, 30)
    assert (
        settings.embedding_model_commit
        == "5617a9f61b028005a4858fdac845db406aefb181"  # pragma: allowlist secret
    )
    assert (
        settings.reranker_model_commit
        == "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"  # pragma: allowlist secret
    )


def test_multiple_discord_guild_ids_are_supported() -> None:
    env = VALID_ENV | {"DISCORD_GUILD_ID": "", "DISCORD_GUILD_IDS": "111, 222"}
    with patch.dict(os.environ, env, clear=True):
        settings = load_settings()

    assert settings.effective_discord_guild_ids == (111, 222)


def test_single_discord_guild_id_remains_backward_compatible() -> None:
    with patch.dict(os.environ, VALID_ENV, clear=True):
        settings = load_settings()

    assert settings.effective_discord_guild_ids == (123456789,)


def test_invalid_discord_channel_ids_fail_without_echoing_secret() -> None:
    env = VALID_ENV | {"DISCORD_CHANNEL_IDS": "10,not-an-id"}
    with patch.dict(os.environ, env, clear=True), pytest.raises(ConfigurationError) as raised:
        load_settings()

    assert "DISCORD_CHANNEL_IDS" in str(raised.value)
    assert VALID_ENV["DISCORD_TOKEN"] not in str(raised.value)


def test_crawler_contact_is_optional() -> None:
    env = VALID_ENV.copy()
    env.pop("CRAWLER_CONTACT")
    with patch.dict(os.environ, env, clear=True):
        settings = load_settings(ProcessRole.CRAWLER_WORKER)

    assert settings.crawler_contact is None


def test_embedding_device_accepts_cuda_and_rejects_unknown_values() -> None:
    with patch.dict(os.environ, VALID_ENV | {"EMBEDDING_DEVICE": "cuda"}, clear=True):
        assert load_settings().embedding_device == "cuda"

    with pytest.raises(ValidationError):
        Settings(**(VALID_ENV | {"EMBEDDING_DEVICE": "tpu"}))


def test_bot_role_requires_remote_reranker() -> None:
    without_provider = {
        key: value for key, value in VALID_ENV.items() if key != "RERANKER_PROVIDER"
    }
    with patch.dict(os.environ, without_provider, clear=True):
        settings = load_settings()
    assert settings.reranker_provider == "remote"

    with (
        patch.dict(os.environ, VALID_ENV | {"RERANKER_PROVIDER": "local"}, clear=True),
        pytest.raises(ConfigurationError) as raised,
    ):
        load_settings()
    assert "RERANKER_PROVIDER" in str(raised.value)
    assert "pii-secret-canary" not in str(raised.value)


def test_every_process_role_rejects_local_providers() -> None:
    for role in ProcessRole:
        with (
            patch.dict(os.environ, VALID_ENV | {"RERANKER_PROVIDER": "local"}, clear=True),
            pytest.raises(ConfigurationError) as raised,
        ):
            load_settings(role)
        assert "RERANKER_PROVIDER" in str(raised.value)
        assert "pii-secret-canary" not in str(raised.value)
        with (
            patch.dict(os.environ, VALID_ENV | {"EMBEDDING_PROVIDER": "local"}, clear=True),
            pytest.raises(ConfigurationError) as raised,
        ):
            load_settings(role)
        assert "EMBEDDING_PROVIDER" in str(raised.value)
        assert "pii-secret-canary" not in str(raised.value)
