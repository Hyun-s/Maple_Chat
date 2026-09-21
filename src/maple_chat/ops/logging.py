"""Structured JSON logging that redacts secrets and common PII."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from maple_chat.crawler.sanitizer import sanitize_text

_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "content",
        "database_url",
        "discord_token",
        "message",
        "nickname",
        "ocr_text",
        "password",
        "pii_hash_salt",
        "query",
        "raw_body",
        "token",
    }
)
_CREDENTIAL_URL = re.compile(r"(?i)\b(?:postgres(?:ql)?|https?)://[^\s/@:]+:[^\s/@]+@")


def redact(value: Any, *, key: str | None = None) -> Any:
    if key is not None and key.casefold() in _SENSITIVE_KEYS:
        return "<redacted>"
    if isinstance(value, dict):
        return {str(item_key): redact(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        cleaned = _CREDENTIAL_URL.sub("<redacted-url>", value)
        return sanitize_text(cleaned).text
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "event": redact(record.getMessage()),
        }
        context = getattr(record, "context", {})
        if isinstance(context, dict):
            payload.update(redact(context))
        if record.exc_info and record.exc_info[0] is not None:
            payload["error_type"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def configure_json_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
