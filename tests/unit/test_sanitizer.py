from __future__ import annotations

from maple_chat.crawler.sanitizer import content_hash, hash_nickname, sanitize_text


def test_sensitive_identifiers_are_masked_before_result_is_returned() -> None:
    raw = "문의 user@example.invalid / 010-1234-5678 / <@123456789012345678> / player#1234"
    result = sanitize_text(raw)
    assert "example.invalid" not in result.text
    assert "010-1234-5678" not in result.text
    assert "123456789012345678" not in result.text
    assert "player#1234" not in result.text
    assert result.masked_fields == {"email", "phone", "discord_id", "discord_tag"}


def test_nickname_hash_is_salted_normalized_and_nonreversible() -> None:
    first = hash_nickname(" FixtureUser ", "x" * 32)
    second = hash_nickname("fixtureuser", "x" * 32)
    assert first == second
    assert first != hash_nickname("fixtureuser", "y" * 32)
    assert "fixture" not in first


def test_content_hash_has_unambiguous_part_boundaries() -> None:
    assert content_hash("ab", "c") != content_hash("a", "bc")
