"""Sequential, per-job summaries of same-day community patch reactions."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import InstrumentedAttribute

from maple_chat.db.models import Article, Board, Category, Comment, SourceStatus
from maple_chat.knowledge.catalog import load_job_catalog
from maple_chat.llm.client import ChatMessage, GenerationResult, LLMUnavailable
from maple_chat.time import KST, to_kst

_FAMILY_BOARD_IDS = {
    "job-family:warrior": 2294,
    "job-family:mage": 2295,
    "job-family:archer": 2296,
    "job-family:thief": 2297,
    "job-family:pirate": 2298,
}
_SEARCHABLE_STATUSES = (
    SourceStatus.SANITIZED,
    SourceStatus.CHUNKED,
    SourceStatus.INDEXED,
)
_PATCH_TERMS = (
    "패치",
    "밸패",
    "테섭",
    "테스트 서버",
    "상향",
    "하향",
    "너프",
    "버프",
    "개선",
    "변경",
    "수정",
)


@dataclass(frozen=True, slots=True)
class JobTarget:
    """One unique playable job and every family board where it belongs."""

    entity_id: str
    name: str
    aliases: tuple[str, ...]
    families: tuple[str, ...]
    board_ids: tuple[int, ...]

    @property
    def search_terms(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.name, *self.aliases)))


@dataclass(frozen=True, slots=True)
class PatchReactionEvidence:
    source_key: str
    title: str
    board: str
    published_at: str
    url: str
    article_text: str
    comments: tuple[str, ...]
    view_count: int
    recommendation_count: int


class ReactionStatus(StrEnum):
    SUMMARIZED = "summarized"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    GENERATION_UNAVAILABLE = "generation_unavailable"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class PatchReactionReport:
    index: int
    target: JobTarget
    patch_date: date
    status: ReactionStatus
    summary: str
    evidence: tuple[PatchReactionEvidence, ...]
    output_path: Path


@dataclass(frozen=True, slots=True)
class PatchReactionBatchResult:
    reports: tuple[PatchReactionReport, ...]

    @property
    def total(self) -> int:
        return len(self.reports)

    @property
    def summarized(self) -> int:
        return sum(report.status is ReactionStatus.SUMMARIZED for report in self.reports)

    @property
    def insufficient(self) -> int:
        return sum(report.status is ReactionStatus.INSUFFICIENT_EVIDENCE for report in self.reports)

    @property
    def unavailable(self) -> int:
        return sum(
            report.status is ReactionStatus.GENERATION_UNAVAILABLE for report in self.reports
        )

    @property
    def skipped(self) -> int:
        return sum(report.status is ReactionStatus.SKIPPED for report in self.reports)


class PatchReactionSearch(Protocol):
    async def search(
        self,
        target: JobTarget,
        patch_date: date,
        *,
        limit: int,
        max_comments_per_article: int,
    ) -> tuple[PatchReactionEvidence, ...]: ...


class ReactionGenerator(Protocol):
    async def generate(
        self, messages: tuple[ChatMessage, ...], *, max_tokens: int = 1024
    ) -> str | GenerationResult: ...


def load_patch_reaction_targets() -> tuple[JobTarget, ...]:
    """Load official job order while de-duplicating dual-family jobs such as Xenon."""

    catalog = load_job_catalog()
    entities = {entity.entity_id: entity for entity in catalog.entities}
    memberships: dict[str, list[str]] = defaultdict(list)
    for relation in catalog.relations:
        if relation.predicate == "is_a" and relation.object_entity_id in _FAMILY_BOARD_IDS:
            memberships[relation.subject_entity_id].append(relation.object_entity_id)

    targets: list[JobTarget] = []
    for entity in catalog.entities:
        family_ids = memberships.get(entity.entity_id)
        if entity.entity_type != "job" or not family_ids:
            continue
        targets.append(
            JobTarget(
                entity_id=entity.entity_id,
                name=entity.canonical_name,
                aliases=entity.aliases,
                families=tuple(entities[family_id].canonical_name for family_id in family_ids),
                board_ids=tuple(_FAMILY_BOARD_IDS[family_id] for family_id in family_ids),
            )
        )
    return tuple(targets)


def kst_day_window(value: date) -> tuple[datetime, datetime]:
    start = datetime.combine(value, time.min, tzinfo=KST).astimezone(UTC)
    return start, start + timedelta(days=1)


def _escaped_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _contains_any(
    column: sa.ColumnElement[str] | InstrumentedAttribute[str], terms: Sequence[str]
) -> sa.ColumnElement[bool]:
    return sa.or_(*(column.ilike(f"%{_escaped_like(term)}%", escape="\\") for term in terms))


def _truncate(value: str, limit: int) -> str:
    compact = re.sub(r"\s+", " ", value).strip()
    return compact if len(compact) <= limit else compact[: limit - 1].rstrip() + "…"


class DatabasePatchReactionSearch:
    """Search one job at a time in its family board's same-day articles and comments."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory

    async def search(
        self,
        target: JobTarget,
        patch_date: date,
        *,
        limit: int,
        max_comments_per_article: int,
    ) -> tuple[PatchReactionEvidence, ...]:
        if limit < 1 or limit > 30:
            raise ValueError("reaction evidence limit must be between 1 and 30")
        if max_comments_per_article < 0 or max_comments_per_article > 50:
            raise ValueError("reaction comment limit must be between 0 and 50")
        start, end = kst_day_window(patch_date)
        terms = target.search_terms
        effective_at = sa.func.coalesce(Article.published_at, Article.first_observed_at)
        article_text = Article.title + sa.literal("\n") + Article.sanitized_body
        matching_comment = sa.exists(
            sa.select(Comment.id).where(
                Comment.article_id == Article.id,
                Comment.status.in_(_SEARCHABLE_STATUSES),
                _contains_any(Comment.sanitized_body, terms),
            )
        )
        job_match = sa.or_(
            _contains_any(article_text, terms),
            _contains_any(Category.remote_name, terms),
            matching_comment,
        )
        patch_match = _contains_any(article_text, _PATCH_TERMS)

        async with self.factory() as session:
            rows = (
                await session.execute(
                    sa.select(Article, Board.name, Category.remote_name)
                    .join(Board, Board.board_id == Article.board_id)
                    .outerjoin(Category, Category.id == Article.category_id)
                    .where(
                        Article.board_id.in_(target.board_ids),
                        Article.status.in_(_SEARCHABLE_STATUSES),
                        effective_at >= start,
                        effective_at < end,
                        job_match,
                    )
                    .order_by(
                        patch_match.desc(),
                        Article.recommendation_count.desc(),
                        Article.comment_count.desc(),
                        Article.view_count.desc(),
                        effective_at.desc(),
                        Article.id.desc(),
                    )
                    .limit(limit * 4)
                )
            ).all()
            selected = rows[:limit]
            article_ids = [article.id for article, _board, _category in selected]
            comments_by_article: dict[int, list[Comment]] = defaultdict(list)
            if article_ids and max_comments_per_article:
                comments = (
                    await session.scalars(
                        sa.select(Comment)
                        .where(
                            Comment.article_id.in_(article_ids),
                            Comment.status.in_(_SEARCHABLE_STATUSES),
                        )
                        .order_by(
                            Comment.article_id,
                            Comment.score.desc(),
                            Comment.published_at.desc().nullslast(),
                            Comment.id,
                        )
                    )
                ).all()
                for comment in comments:
                    bucket = comments_by_article[comment.article_id]
                    if len(bucket) < max_comments_per_article:
                        bucket.append(comment)

        evidence: list[PatchReactionEvidence] = []
        for article, board_name, _category_name in selected:
            source_time = article.published_at or article.first_observed_at
            evidence.append(
                PatchReactionEvidence(
                    source_key=f"article:{article.board_id}:{article.remote_article_id}",
                    title=article.title,
                    board=board_name,
                    published_at=to_kst(source_time).isoformat(timespec="seconds"),
                    url=article.url,
                    article_text=_truncate(article.sanitized_body, 2_400),
                    comments=tuple(
                        _truncate(comment.sanitized_body, 500)
                        for comment in comments_by_article[article.id]
                        if comment.sanitized_body.strip()
                    ),
                    view_count=article.view_count,
                    recommendation_count=article.recommendation_count,
                )
            )
        return tuple(evidence)


def build_patch_reaction_messages(
    target: JobTarget,
    patch_date: date,
    evidence: tuple[PatchReactionEvidence, ...],
) -> tuple[ChatMessage, ...]:
    payload = {
        "job": target.name,
        "aliases": target.aliases,
        "families": target.families,
        "patch_date_kst": patch_date.isoformat(),
        "evidence": [asdict(item) for item in evidence],
    }
    system = (
        "당신은 메이플스토리 커뮤니티 반응을 근거 중심으로 정리한다. 이번 호출에서는 오직 "
        "한 직업만 다룬다. 제공된 evidence_json 밖의 패치 내용, 수치, 여론을 추측하지 않는다. "
        "같은 게시글의 본문과 댓글은 하나의 출처 묶음으로 세고, 소수 의견을 전체 이용자 여론으로 "
        "일반화하지 않는다. 해당 날짜의 패치와 직접 관련 없는 글은 무시한다. 긍정 반응, 부정 반응, "
        "추가 개선 요구가 실제 근거에 있을 때만 구분하고, 표본 수와 근거 한계를 반드시 밝힌다. "
        "한국어 Markdown으로 8문장 이내에 간결하게 작성한다."
    )
    user = (
        "아래 한 직업의 금일 패치 반응만 정리해 주세요. 다른 직업의 반응을 섞지 마세요.\n"
        f"<evidence_json>{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
        "</evidence_json>"
    )
    return (ChatMessage("system", system), ChatMessage("user", user))


class PatchReactionReportWriter:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path_for(self, patch_date: date, index: int, target: JobTarget) -> Path:
        slug = target.entity_id.removeprefix("job:")
        return self.root / patch_date.isoformat() / f"{index:03d}-{slug}.md"

    def exists(self, patch_date: date, index: int, target: JobTarget) -> bool:
        return self.path_for(patch_date, index, target).is_file()

    def write(self, report: PatchReactionReport) -> None:
        path = report.output_path
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"# {report.target.name} — {report.patch_date.isoformat()} 패치 반응",
            "",
            f"- 직업군: {' · '.join(report.target.families)}",
            f"- 상태: `{report.status.value}`",
            f"- 근거 게시글: {len(report.evidence)}건",
            "",
            report.summary,
        ]
        if report.evidence:
            lines.extend(("", "## 확인한 게시글", ""))
            for rank, item in enumerate(report.evidence, start=1):
                lines.append(
                    f"{rank}. [{item.title}]({item.url}) — {item.board}, "
                    f"{item.published_at}, 댓글 {len(item.comments)}개"
                )
        self._atomic_write(path, "\n".join(lines).rstrip() + "\n")

    def write_manifest(
        self,
        patch_date: date,
        reports: Sequence[PatchReactionReport],
        *,
        expected_total: int,
    ) -> None:
        directory = self.root / patch_date.isoformat()
        directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "patch_date_kst": patch_date.isoformat(),
            "expected_total": expected_total,
            "completed": len(reports),
            "reports": [
                {
                    "index": report.index,
                    "job": report.target.name,
                    "families": report.target.families,
                    "status": report.status.value,
                    "evidence_count": len(report.evidence),
                    "path": report.output_path.name,
                }
                for report in reports
            ],
        }
        self._atomic_write(
            directory / "manifest.json",
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)


class PatchReactionRunner:
    """Run one complete search→summary→write transaction before starting the next job."""

    def __init__(
        self,
        search: PatchReactionSearch,
        generator: ReactionGenerator,
        writer: PatchReactionReportWriter,
    ) -> None:
        self.search = search
        self.generator = generator
        self.writer = writer

    async def run(
        self,
        targets: Sequence[JobTarget],
        *,
        patch_date: date,
        evidence_limit: int = 8,
        max_comments_per_article: int = 12,
        overwrite: bool = False,
        on_progress: Callable[[PatchReactionReport], None] | None = None,
    ) -> PatchReactionBatchResult:
        reports: list[PatchReactionReport] = []
        for index, target in enumerate(targets, start=1):
            output_path = self.writer.path_for(patch_date, index, target)
            if output_path.is_file() and not overwrite:
                report = PatchReactionReport(
                    index=index,
                    target=target,
                    patch_date=patch_date,
                    status=ReactionStatus.SKIPPED,
                    summary="기존 개별 보고서를 유지했습니다.",
                    evidence=(),
                    output_path=output_path,
                )
            else:
                evidence = await self.search.search(
                    target,
                    patch_date,
                    limit=evidence_limit,
                    max_comments_per_article=max_comments_per_article,
                )
                status = ReactionStatus.INSUFFICIENT_EVIDENCE
                summary = (
                    "수집된 게시글·댓글에서 이 직업에 직접 연결되는 금일 반응 근거를 "
                    "찾지 못했습니다. 반응이 없다는 뜻이 아니라 현재 수집 범위에서 확인되지 "
                    "않았다는 뜻입니다."
                )
                if evidence:
                    try:
                        generated = await self.generator.generate(
                            build_patch_reaction_messages(target, patch_date, evidence),
                            max_tokens=700,
                        )
                        summary = (
                            generated.text if isinstance(generated, GenerationResult) else generated
                        ).strip()
                        if not summary:
                            raise LLMUnavailable("local generation returned an empty summary")
                        status = ReactionStatus.SUMMARIZED
                    except LLMUnavailable:
                        status = ReactionStatus.GENERATION_UNAVAILABLE
                        summary = self._retrieval_only_summary(evidence)
                report = PatchReactionReport(
                    index=index,
                    target=target,
                    patch_date=patch_date,
                    status=status,
                    summary=summary,
                    evidence=evidence,
                    output_path=output_path,
                )
                self.writer.write(report)
            reports.append(report)
            self.writer.write_manifest(patch_date, reports, expected_total=len(targets))
            if on_progress is not None:
                on_progress(report)
        return PatchReactionBatchResult(tuple(reports))

    @staticmethod
    def _retrieval_only_summary(evidence: tuple[PatchReactionEvidence, ...]) -> str:
        lines = ["로컬 모델을 사용할 수 없어 검색된 게시글 제목만 제공합니다."]
        lines.extend(f"- {item.title}" for item in evidence[:5])
        lines.append(
            "위 목록은 반응 요약이 아니며, 모델이 준비되면 해당 직업만 다시 실행해야 합니다."
        )
        return "\n".join(lines)
