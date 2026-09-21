from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from maple_chat.llm.client import ChatMessage
from maple_chat.patch_reactions import (
    JobTarget,
    PatchReactionEvidence,
    PatchReactionReportWriter,
    PatchReactionRunner,
    ReactionStatus,
    kst_day_window,
    load_patch_reaction_targets,
)


def _target(entity_id: str, name: str) -> JobTarget:
    return JobTarget(
        entity_id=entity_id,
        name=name,
        aliases=(),
        families=("전사",),
        board_ids=(2294,),
    )


def _evidence(job: str) -> PatchReactionEvidence:
    return PatchReactionEvidence(
        source_key=f"article:2294:{job}",
        title=f"{job} 패치 반응",
        board="전사",
        published_at="2026-09-10T12:00:00+09:00",
        url=f"https://www.inven.co.kr/board/maple/2294/{job}",
        article_text=f"{job} 변경점이 체감된다.",
        comments=("조작감은 좋아졌다.", "수치 조정은 더 필요하다."),
        view_count=100,
        recommendation_count=3,
    )


def test_job_targets_cover_every_unique_subjob_once() -> None:
    targets = load_patch_reaction_targets()

    assert len(targets) == 48
    assert len({target.entity_id for target in targets}) == 48
    assert targets[0].name == "히어로"
    xenon = next(target for target in targets if target.name == "제논")
    assert xenon.families == ("도적", "해적")
    assert xenon.board_ids == (2297, 2298)


def test_kst_day_window_returns_utc_half_open_interval() -> None:
    start, end = kst_day_window(date(2026, 9, 10))

    assert start == datetime(2026, 9, 9, 15, tzinfo=UTC)
    assert end == datetime(2026, 9, 10, 15, tzinfo=UTC)


@pytest.mark.asyncio
async def test_runner_searches_and_summarizes_each_job_sequentially(tmp_path: Path) -> None:
    events: list[str] = []

    class Search:
        async def search(
            self,
            target: JobTarget,
            patch_date: date,
            *,
            limit: int,
            max_comments_per_article: int,
        ) -> tuple[PatchReactionEvidence, ...]:
            assert patch_date == date(2026, 9, 10)
            assert limit == 8
            assert max_comments_per_article == 12
            events.append(f"search:{target.name}")
            return (_evidence(target.name),)

    class Generator:
        async def generate(
            self, messages: tuple[ChatMessage, ...], *, max_tokens: int = 1024
        ) -> str:
            job = messages[-1].content.split('"job":"', 1)[1].split('"', 1)[0]
            events.append(f"generate:{job}")
            assert max_tokens == 700
            return f"{job} 이용자 반응 요약"

    targets = (_target("job:hero", "히어로"), _target("job:paladin", "팔라딘"))
    runner = PatchReactionRunner(
        Search(),
        Generator(),
        PatchReactionReportWriter(tmp_path),
    )

    result = await runner.run(targets, patch_date=date(2026, 9, 10))

    assert events == [
        "search:히어로",
        "generate:히어로",
        "search:팔라딘",
        "generate:팔라딘",
    ]
    assert result.total == 2
    assert result.summarized == 2
    assert result.insufficient == 0
    assert (tmp_path / "2026-09-10" / "001-hero.md").is_file()
    assert (tmp_path / "2026-09-10" / "002-paladin.md").is_file()
    assert "히어로 이용자 반응 요약" in (tmp_path / "2026-09-10" / "001-hero.md").read_text()


@pytest.mark.asyncio
async def test_runner_writes_an_individual_insufficient_report_without_generation(
    tmp_path: Path,
) -> None:
    class EmptySearch:
        async def search(
            self,
            target: JobTarget,
            patch_date: date,
            *,
            limit: int,
            max_comments_per_article: int,
        ) -> tuple[PatchReactionEvidence, ...]:
            return ()

    class UnexpectedGenerator:
        async def generate(
            self, messages: tuple[ChatMessage, ...], *, max_tokens: int = 1024
        ) -> str:
            raise AssertionError("insufficient evidence must not invoke the model")

    runner = PatchReactionRunner(
        EmptySearch(),
        UnexpectedGenerator(),
        PatchReactionReportWriter(tmp_path),
    )
    target = _target("job:hero", "히어로")

    result = await runner.run((target,), patch_date=date(2026, 9, 10))

    assert result.total == 1
    assert result.insufficient == 1
    report = result.reports[0]
    assert report.status is ReactionStatus.INSUFFICIENT_EVIDENCE
    rendered = (tmp_path / "2026-09-10" / "001-hero.md").read_text()
    assert "직접 연결되는 금일 반응 근거를 찾지 못했습니다" in rendered


@pytest.mark.asyncio
async def test_existing_job_report_is_resumed_without_search_or_generation(tmp_path: Path) -> None:
    output = tmp_path / "2026-09-10"
    output.mkdir()
    existing = output / "001-hero.md"
    existing.write_text("already complete", encoding="utf-8")

    class UnexpectedSearch:
        async def search(
            self,
            target: JobTarget,
            patch_date: date,
            *,
            limit: int,
            max_comments_per_article: int,
        ) -> tuple[PatchReactionEvidence, ...]:
            raise AssertionError("completed reports must be skipped on resume")

    class UnexpectedGenerator:
        async def generate(
            self, messages: tuple[ChatMessage, ...], *, max_tokens: int = 1024
        ) -> str:
            raise AssertionError("completed reports must be skipped on resume")

    runner = PatchReactionRunner(
        UnexpectedSearch(),
        UnexpectedGenerator(),
        PatchReactionReportWriter(tmp_path),
    )

    result = await runner.run((_target("job:hero", "히어로"),), patch_date=date(2026, 9, 10))

    assert result.skipped == 1
    assert result.reports[0].status is ReactionStatus.SKIPPED
    assert existing.read_text(encoding="utf-8") == "already complete"
