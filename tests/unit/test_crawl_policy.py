from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from maple_chat.config import Settings
from maple_chat.crawler.policy import CrawlScope, LiveCrawlDenied, require_live_crawl


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "discord_token": SecretStr("token-canary"),
        "discord_guild_id": 1,
        "discord_owner_id": 2,
        "database_url": SecretStr("db-canary"),
        "crawler_user_agent": "MapleChat/0.1 (+https://example.invalid/contact)",
        "crawler_contact": "operator@example.invalid",
        "pii_hash_salt": SecretStr("salt-canary"),
        "fixture_root": tmp_path / "fixtures",
        "live_crawl_enabled": False,
        "live_crawl_approval_file": None,
        "reranker_provider": "remote",
    }
    values.update(overrides)
    return Settings.model_validate(values)


def write_approval(path: Path, scopes: list[str] | None = None) -> None:
    path.write_text(
        json.dumps(
            {
                "status": "approved",
                "evidence_type": "legal_review",
                "approved_at": "2026-08-04T00:00:00Z",
                "approved_by": "qualified-reviewer",
                "scopes": scopes or ["approved_sample"],
            }
        ),
        encoding="utf-8",
    )


def test_live_http_is_denied_by_default(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    with pytest.raises(LiveCrawlDenied, match="LIVE_CRAWL_ENABLED=false"):
        require_live_crawl(
            settings,
            "https://www.inven.co.kr/board/maple/2304/1",
            CrawlScope.APPROVED_SAMPLE,
        )


def test_live_http_needs_valid_recorded_approval(tmp_path: Path) -> None:
    approval = tmp_path / "approval.json"
    approval.write_text("{}", encoding="utf-8")
    settings = make_settings(
        tmp_path,
        live_crawl_enabled=True,
        live_crawl_approval_file=approval,
    )

    with pytest.raises(LiveCrawlDenied, match="approval evidence"):
        require_live_crawl(
            settings,
            "https://www.inven.co.kr/board/maple/2304/1",
            CrawlScope.APPROVED_SAMPLE,
        )


def test_scope_and_hostname_are_fail_closed(tmp_path: Path) -> None:
    approval = tmp_path / "approval.json"
    write_approval(approval)
    settings = make_settings(
        tmp_path,
        live_crawl_enabled=True,
        live_crawl_approval_file=approval,
    )

    with pytest.raises(LiveCrawlDenied, match="scope"):
        require_live_crawl(
            settings,
            "https://www.inven.co.kr/board/maple/2304/1",
            CrawlScope.FULL_BACKFILL,
        )
    with pytest.raises(LiveCrawlDenied, match="allowlisted"):
        require_live_crawl(
            settings,
            "https://example.com/not-inven",
            CrawlScope.APPROVED_SAMPLE,
        )
    with pytest.raises(LiveCrawlDenied, match="path"):
        require_live_crawl(
            settings,
            "https://www.inven.co.kr/webzine/news/",
            CrawlScope.APPROVED_SAMPLE,
        )
    with pytest.raises(LiveCrawlDenied, match="board"):
        require_live_crawl(
            settings,
            "https://www.inven.co.kr/board/maple/9999/1",
            CrawlScope.APPROVED_SAMPLE,
        )


def test_allowlisted_https_sample_with_matching_approval_is_allowed(tmp_path: Path) -> None:
    approval = tmp_path / "approval.json"
    write_approval(approval)
    settings = make_settings(
        tmp_path,
        live_crawl_enabled=True,
        live_crawl_approval_file=approval,
    )

    require_live_crawl(
        settings,
        "https://www.inven.co.kr/board/maple/2304/1",
        CrawlScope.APPROVED_SAMPLE,
    )


def test_live_gate_rejects_missing_approval_and_plain_http(tmp_path: Path) -> None:
    missing_approval = make_settings(tmp_path).model_copy(
        update={"live_crawl_enabled": True, "live_crawl_approval_file": None}
    )
    with pytest.raises(LiveCrawlDenied, match="approval evidence"):
        require_live_crawl(
            missing_approval,
            "https://www.inven.co.kr/board/maple/2304/1",
            CrawlScope.APPROVED_SAMPLE,
        )

    approval = tmp_path / "approval.json"
    write_approval(approval)
    settings = make_settings(
        tmp_path,
        live_crawl_enabled=True,
        live_crawl_approval_file=approval,
    )
    with pytest.raises(LiveCrawlDenied, match="HTTPS"):
        require_live_crawl(
            settings,
            "http://www.inven.co.kr/board/maple/2304/1",
            CrawlScope.APPROVED_SAMPLE,
        )
    with pytest.raises(LiveCrawlDenied, match="HTTPS"):
        require_live_crawl(
            settings,
            "https://www.inven.co.kr:444/board/maple/2304/1",
            CrawlScope.APPROVED_SAMPLE,
        )
    with pytest.raises(LiveCrawlDenied, match="credentials"):
        require_live_crawl(
            settings,
            "https://user@www.inven.co.kr/board/maple/2304/1",
            CrawlScope.APPROVED_SAMPLE,
        )
    with pytest.raises(LiveCrawlDenied, match="malformed"):
        require_live_crawl(
            settings,
            "https://www.inven.co.kr:invalid/board/maple/2304/1",
            CrawlScope.APPROVED_SAMPLE,
        )


def test_comment_and_inven_upload_paths_are_narrowly_allowlisted(tmp_path: Path) -> None:
    approval = tmp_path / "approval.json"
    write_approval(approval)
    settings = make_settings(
        tmp_path,
        live_crawl_enabled=True,
        live_crawl_approval_file=approval,
    )
    require_live_crawl(
        settings,
        "https://www.inven.co.kr/common/board/comment.json.php",
        CrawlScope.APPROVED_SAMPLE,
    )
    require_live_crawl(
        settings,
        "https://upload2.inven.co.kr/upload/2026/08/fixture.png",
        CrawlScope.APPROVED_SAMPLE,
    )
    require_live_crawl(
        settings,
        "https://static.inven.co.kr/image_2011/maple/common/icon.png",
        CrawlScope.APPROVED_SAMPLE,
    )
    with pytest.raises(LiveCrawlDenied, match="media path"):
        require_live_crawl(
            settings,
            "https://upload2.inven.co.kr/private/fixture.png",
            CrawlScope.APPROVED_SAMPLE,
        )
    with pytest.raises(LiveCrawlDenied, match="static media path"):
        require_live_crawl(
            settings,
            "https://static.inven.co.kr/private/fixture.png",
            CrawlScope.APPROVED_SAMPLE,
        )


def test_full_backfill_scope_with_matching_approval_is_allowed(tmp_path: Path) -> None:
    approval = tmp_path / "approval.json"
    write_approval(approval, scopes=["full_backfill"])
    settings = make_settings(
        tmp_path,
        live_crawl_enabled=True,
        live_crawl_approval_file=approval,
    )

    require_live_crawl(
        settings,
        "https://www.inven.co.kr/board/maple/2304?p=1",
        CrawlScope.FULL_BACKFILL,
    )
