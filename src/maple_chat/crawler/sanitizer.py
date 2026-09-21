"""Deterministic PII removal performed before persistence or embedding."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass

_EMAIL = re.compile(r"(?<![\w.-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_PHONE = re.compile(r"(?<!\d)(?:\+?82[- .]?)?0?1[016789][- .]?\d{3,4}[- .]?\d{4}(?!\d)")
_DISCORD_MENTION = re.compile(r"<@!?(?:\d{15,22})>|<@&(?:\d{15,22})>")
_DISCORD_TAG = re.compile(r"(?<!\w)[\w가-힣.]{2,32}#\d{4}(?!\d)")
_WHITESPACE = re.compile(r"[ \t\f\v]+")


@dataclass(frozen=True, slots=True)
class SanitizedText:
    text: str
    masked_fields: frozenset[str]


def sanitize_text(value: str) -> SanitizedText:
    """Mask supported sensitive identifiers and normalize non-semantic whitespace."""
    masked: set[str] = set()
    result = value.replace("\x00", "")
    for label, pattern, replacement in (
        ("email", _EMAIL, "[이메일 삭제]"),
        ("phone", _PHONE, "[전화번호 삭제]"),
        ("discord_id", _DISCORD_MENTION, "[Discord 식별자 삭제]"),
        ("discord_tag", _DISCORD_TAG, "[Discord 식별자 삭제]"),
    ):
        result, count = pattern.subn(replacement, result)
        if count:
            masked.add(label)
    result = "\n".join(_WHITESPACE.sub(" ", line).strip() for line in result.splitlines())
    result = re.sub(r"\n{3,}", "\n\n", result).strip()
    return SanitizedText(text=result, masked_fields=frozenset(masked))


def hash_nickname(nickname: str, salt: str) -> str:
    """Return a domain-separated, irreversible nickname pseudonym."""
    if len(salt) < 16:
        raise ValueError("PII hash salt must contain at least 16 characters")
    normalized = nickname.strip().casefold().encode()
    return hmac.new(
        salt.encode(), b"maple-chat:nickname:v1\0" + normalized, hashlib.sha256
    ).hexdigest()


def content_hash(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        encoded = part.encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()
