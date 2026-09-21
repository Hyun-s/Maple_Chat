"""Typed, fail-closed application configuration."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlparse

from pydantic import (
    Field,
    PositiveInt,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class ConfigurationError(RuntimeError):
    """A sanitized configuration failure safe to show in logs or a terminal."""


class ProcessRole(StrEnum):
    BOT = "bot"
    SCHEDULER = "scheduler"
    CRAWLER_WORKER = "crawler-worker"
    INDEX_WORKER = "index-worker"


class Settings(BaseSettings):
    """Environment-only settings with explicit redaction for secret-bearing fields."""

    model_config = SettingsConfigDict(
        case_sensitive=False,
        env_file=None,
        extra="ignore",
        populate_by_name=True,
        str_strip_whitespace=True,
        validate_default=True,
    )

    role: ProcessRole = Field(default=ProcessRole.BOT, exclude=True)
    discord_token: SecretStr | None = Field(default=None, validation_alias="DISCORD_TOKEN")
    discord_guild_id: PositiveInt | None = Field(
        default=None,
        validation_alias="DISCORD_GUILD_ID",
    )
    discord_guild_ids: Annotated[tuple[PositiveInt, ...], NoDecode] = Field(
        default=(),
        validation_alias="DISCORD_GUILD_IDS",
    )
    discord_owner_id: PositiveInt | None = Field(
        default=None,
        validation_alias="DISCORD_OWNER_ID",
    )
    discord_channel_ids: Annotated[tuple[PositiveInt, ...], NoDecode] = Field(
        default=(),
        validation_alias="DISCORD_CHANNEL_IDS",
    )
    database_url: SecretStr = Field(validation_alias="DATABASE_URL")

    nexon_api_key: SecretStr | None = Field(default=None, validation_alias="NEXON_API_KEY")
    agent_max_tool_calls: int = Field(
        default=4,
        ge=1,
        le=8,
        validation_alias="AGENT_MAX_TOOL_CALLS",
    )

    llm_base_url: str = Field(
        default="http://127.0.0.1:8001/v1",
        validation_alias="LLM_BASE_URL",
    )
    llm_model: str = Field(default="local-coder", validation_alias="LLM_MODEL")
    llm_timeout_seconds: float = Field(
        default=180.0,
        gt=0,
        le=300,
        validation_alias="LLM_TIMEOUT_SECONDS",
    )
    embedding_model: str = Field(
        default="BAAI/bge-m3",
        validation_alias="EMBEDDING_MODEL",
    )
    embedding_provider: Literal["local", "remote"] = Field(
        default="local",
        validation_alias="EMBEDDING_PROVIDER",
    )
    embedding_base_url: str = Field(
        default="http://127.0.0.1:8081/v1",
        validation_alias="EMBEDDING_BASE_URL",
    )
    embedding_remote_model: str = Field(
        default="bge-m3",
        min_length=1,
        validation_alias="EMBEDDING_REMOTE_MODEL",
    )
    embedding_timeout_seconds: float = Field(
        default=60.0,
        gt=0,
        le=300,
        validation_alias="EMBEDDING_TIMEOUT_SECONDS",
    )
    embedding_device: str = Field(
        default="auto",
        pattern=r"^(auto|cpu|cuda)$",
        validation_alias="EMBEDDING_DEVICE",
    )
    reranker_model: str = Field(
        default="BAAI/bge-reranker-v2-m3",
        validation_alias="RERANKER_MODEL",
    )
    embedding_model_commit: str = Field(
        default="5617a9f61b028005a4858fdac845db406aefb181",  # pragma: allowlist secret
        min_length=7,
        validation_alias="EMBEDDING_MODEL_COMMIT",
    )
    reranker_model_commit: str = Field(
        default="953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e",  # pragma: allowlist secret
        min_length=7,
        validation_alias="RERANKER_MODEL_COMMIT",
    )

    crawl_daily_at: str = Field(default="05:00", validation_alias="CRAWL_DAILY_AT")
    crawler_user_agent: str | None = Field(
        default=None,
        min_length=8,
        validation_alias="CRAWLER_USER_AGENT",
    )
    crawler_contact: str | None = Field(
        default=None,
        min_length=3,
        validation_alias="CRAWLER_CONTACT",
    )
    pii_hash_salt: SecretStr | None = Field(default=None, validation_alias="PII_HASH_SALT")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")

    live_crawl_enabled: bool = Field(default=False, validation_alias="LIVE_CRAWL_ENABLED")
    live_crawl_approval_file: Path | None = Field(
        default=None,
        validation_alias="LIVE_CRAWL_APPROVAL_FILE",
    )
    fixture_root: Path = Field(
        default=Path("tests/fixtures/inven"),
        validation_alias="FIXTURE_ROOT",
    )

    @field_validator("crawler_contact", "discord_guild_id", mode="before")
    @classmethod
    def normalize_optional_string(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("discord_channel_ids", mode="before")
    @classmethod
    def parse_discord_channel_ids(cls, value: object) -> object:
        if value is None or value == "":
            return ()
        if isinstance(value, str):
            try:
                return tuple(int(item.strip()) for item in value.split(",") if item.strip())
            except ValueError as exc:
                raise ValueError("DISCORD_CHANNEL_IDS must be comma-separated integers") from exc
        return value

    @field_validator("discord_guild_ids", mode="before")
    @classmethod
    def parse_discord_guild_ids(cls, value: object) -> object:
        if value is None or value == "":
            return ()
        if isinstance(value, str):
            try:
                return tuple(int(item.strip()) for item in value.split(",") if item.strip())
            except ValueError as exc:
                raise ValueError("DISCORD_GUILD_IDS must be comma-separated integers") from exc
        return value

    @property
    def effective_discord_guild_ids(self) -> tuple[PositiveInt, ...]:
        """Return plural guild configuration with legacy single-guild fallback."""
        if self.discord_guild_ids:
            return self.discord_guild_ids
        if self.discord_guild_id is not None:
            return (self.discord_guild_id,)
        return ()

    @model_validator(mode="after")
    def validate_safety_boundaries(self) -> Self:
        required: dict[ProcessRole, tuple[tuple[str, object], ...]] = {
            ProcessRole.BOT: (
                ("DISCORD_TOKEN", self.discord_token),
                ("DISCORD_OWNER_ID", self.discord_owner_id),
                ("PII_HASH_SALT", self.pii_hash_salt),
            ),
            ProcessRole.SCHEDULER: (),
            ProcessRole.CRAWLER_WORKER: (
                ("CRAWLER_USER_AGENT", self.crawler_user_agent),
                ("PII_HASH_SALT", self.pii_hash_salt),
            ),
            ProcessRole.INDEX_WORKER: (("PII_HASH_SALT", self.pii_hash_salt),),
        }
        missing = [name for name, value in required[self.role] if value is None]
        if self.role is ProcessRole.BOT and not self.effective_discord_guild_ids:
            missing.append("DISCORD_GUILD_IDS or DISCORD_GUILD_ID")
        if missing:
            raise ValueError(
                f"Required configuration for role {self.role.value}: {', '.join(missing)}"
            )

        llm_url = urlparse(self.llm_base_url)
        allowed_llm_hosts = {"127.0.0.1", "::1", "localhost", "host.docker.internal"}
        if (
            llm_url.scheme not in {"http", "https"}
            or llm_url.hostname not in allowed_llm_hosts
            or llm_url.username is not None
            or llm_url.password is not None
        ):
            raise ValueError("LLM_BASE_URL must target the approved local model boundary")

        embedding_url = urlparse(self.embedding_base_url)
        allowed_embedding_hosts = allowed_llm_hosts | {"dcm-embedding"}
        if (
            embedding_url.scheme not in {"http", "https"}
            or embedding_url.hostname not in allowed_embedding_hosts
            or embedding_url.username is not None
            or embedding_url.password is not None
            or embedding_url.query
            or embedding_url.fragment
        ):
            raise ValueError("EMBEDDING_BASE_URL must target the approved local model boundary")

        if self.live_crawl_enabled and self.live_crawl_approval_file is None:
            raise ValueError("LIVE_CRAWL_APPROVAL_FILE is required when LIVE_CRAWL_ENABLED=true")
        return self

    def safe_summary(self) -> dict[str, Any]:
        """Return operational settings without any secret material."""
        return {
            "discord_token": "<redacted>",
            "discord_guild_id": self.discord_guild_id,
            "discord_guild_ids": list(self.effective_discord_guild_ids),
            "discord_owner_id": self.discord_owner_id,
            "discord_channel_ids": list(self.discord_channel_ids),
            "database_url": "<redacted>",
            "nexon_api_key": "<configured>" if self.nexon_api_key is not None else None,
            "agent_max_tool_calls": self.agent_max_tool_calls,
            "llm_base_url": self.llm_base_url,
            "llm_model": self.llm_model,
            "embedding_provider": self.embedding_provider,
            "embedding_base_url": self.embedding_base_url,
            "embedding_remote_model": self.embedding_remote_model,
            "crawler_user_agent": self.crawler_user_agent,
            "crawler_contact": self.crawler_contact,
            "pii_hash_salt": "<redacted>",
            "live_crawl_enabled": self.live_crawl_enabled,
            "live_crawl_approval_file": (
                str(self.live_crawl_approval_file) if self.live_crawl_approval_file else None
            ),
            "fixture_root": str(self.fixture_root),
            "log_level": self.log_level,
        }

    def __repr__(self) -> str:
        return f"Settings({self.safe_summary()!r})"

    def __str__(self) -> str:
        return self.__repr__()


def _field_label(location: tuple[int | str, ...]) -> str:
    if not location:
        return "CONFIGURATION"
    field_name = str(location[0])
    field = Settings.model_fields.get(field_name)
    if field is not None and isinstance(field.validation_alias, str):
        return field.validation_alias
    return field_name.upper()


def load_settings(role: ProcessRole = ProcessRole.BOT) -> Settings:
    """Load settings while preventing Pydantic input values from reaching errors."""
    try:
        return Settings(role=role)
    except ValidationError as exc:
        labels = {_field_label(error["loc"]) for error in exc.errors(include_input=False)}
        for error in exc.errors(include_input=False):
            message = str(error.get("msg", ""))
            for field in Settings.model_fields.values():
                alias = field.validation_alias
                if isinstance(alias, str) and alias in message:
                    labels.add(alias)
        joined = ", ".join(sorted(labels))
        raise ConfigurationError(f"Invalid or missing configuration fields: {joined}") from None
