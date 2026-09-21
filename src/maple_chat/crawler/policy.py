"""Rights-aware policy for every live Inven HTTP request."""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from maple_chat.config import Settings


class LiveCrawlDenied(RuntimeError):
    """A live request was denied before network I/O."""


class CrawlScope(StrEnum):
    APPROVED_SAMPLE = "approved_sample"
    FULL_BACKFILL = "full_backfill"


class ApprovalEvidence(BaseModel):
    """Minimal auditable record for an externally granted crawl scope."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["approved"]
    evidence_type: Literal["written_permission", "legal_review", "operator_authorization"]
    approved_at: AwareDatetime
    approved_by: str = Field(min_length=3)
    scopes: set[CrawlScope] = Field(min_length=1)


_ALLOWED_HOSTS = frozenset({"inven.co.kr", "www.inven.co.kr"})
_ALLOWED_MEDIA_HOSTS = frozenset(
    {"upload.inven.co.kr", "upload2.inven.co.kr", "upload3.inven.co.kr"}
)
_STATIC_MEDIA_HOST = "static.inven.co.kr"
_ALLOWED_BOARD_IDS = frozenset({"2294", "2295", "2296", "2297", "2298", "2300", "2304"})


def _load_approval(path: Path) -> ApprovalEvidence:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return ApprovalEvidence.model_validate(raw)
    except (OSError, json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
        raise LiveCrawlDenied("Live crawl approval evidence is missing or invalid") from exc


def require_live_crawl(settings: Settings, url: str, scope: CrawlScope) -> None:
    """Authorize one live URL or fail before an HTTP client can be called."""
    if not settings.live_crawl_enabled:
        raise LiveCrawlDenied("Live HTTP denied because LIVE_CRAWL_ENABLED=false")
    if settings.live_crawl_approval_file is None:
        raise LiveCrawlDenied("Live crawl approval evidence is missing or invalid")

    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise LiveCrawlDenied("Live crawl URL is malformed") from exc
    if parsed.scheme != "https" or port not in {None, 443}:
        raise LiveCrawlDenied("Live crawl requires HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise LiveCrawlDenied("Live crawl URL credentials are forbidden")
    hostname = (parsed.hostname or "").lower()
    if hostname in _ALLOWED_MEDIA_HOSTS:
        if not parsed.path.startswith("/upload/"):
            raise LiveCrawlDenied("Live crawl media path is not allowlisted")
    elif hostname == _STATIC_MEDIA_HOST:
        if not parsed.path.startswith("/image_2011/"):
            raise LiveCrawlDenied("Live crawl static media path is not allowlisted")
    elif hostname in _ALLOWED_HOSTS and parsed.path == "/common/board/comment.json.php":
        pass
    elif hostname not in _ALLOWED_HOSTS:
        raise LiveCrawlDenied(f"Live crawl target host is not allowlisted: {hostname}")
    else:
        path_parts = tuple(part for part in parsed.path.split("/") if part)
        if len(path_parts) < 3 or path_parts[:2] != ("board", "maple"):
            raise LiveCrawlDenied("Live crawl path is not allowlisted")
        if path_parts[2] not in _ALLOWED_BOARD_IDS:
            raise LiveCrawlDenied("Live crawl board is not allowlisted")

    approval = _load_approval(settings.live_crawl_approval_file)
    if scope not in approval.scopes:
        raise LiveCrawlDenied(f"Live crawl scope is not approved: {scope.value}")
