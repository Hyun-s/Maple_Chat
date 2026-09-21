from __future__ import annotations

import os
from unittest.mock import patch

from maple_chat.cli import _listing_is_exhausted, main
from maple_chat.crawler.live import LiveIngestResult

from .test_config import VALID_ENV


def test_validate_config_reports_only_safe_state(capsys: object) -> None:
    with patch.dict(os.environ, VALID_ENV, clear=True):
        result = main(["validate-config", "--role", "crawler-worker"])

    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert result == 0
    assert "role=crawler-worker" in output
    assert "live_crawl_enabled=False" in output
    assert VALID_ENV["DISCORD_TOKEN"] not in output
    assert VALID_ENV["DATABASE_URL"] not in output


def test_validate_config_fails_safely(capsys: object) -> None:
    with patch.dict(os.environ, {"DATABASE_URL": "opaque-db-canary"}, clear=True):
        result = main(["validate-config", "--role", "bot"])

    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert result == 2
    assert "configuration error" in output
    assert "DISCORD_TOKEN" in output


def test_approved_sample_cli_rejects_scope_expansion_before_network(
    capsys: object,
    tmp_path: object,
) -> None:
    approval = str(tmp_path) + "/approval.json"
    env = VALID_ENV | {
        "LIVE_CRAWL_ENABLED": "true",
        "LIVE_CRAWL_APPROVAL_FILE": approval,
    }
    with patch.dict(os.environ, env, clear=True):
        result = main(
            [
                "crawl-live",
                "--scope",
                "approved_sample",
                "--board",
                "2304",
                "--board",
                "2300",
            ]
        )

    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert result == 3
    assert "approved_sample is limited" in output


def test_listing_exhaustion_uses_discovery_not_successful_ingestion() -> None:
    denied_page = LiveIngestResult(2304, 1, 0, 3, 0, 0, 0)
    empty_page = LiveIngestResult(2304, 2, 0, 0, 0, 0, 0)

    assert _listing_is_exhausted(denied_page) is False
    assert _listing_is_exhausted(empty_page) is True
