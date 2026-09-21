"""Operational CLI for configuration, live ingestion, indexing, and Discord."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from dataclasses import asdict
from datetime import date
from pathlib import Path

import httpx
import sqlalchemy as sa

from maple_chat.config import ConfigurationError, ProcessRole, load_settings
from maple_chat.crawler.circuit import CrawlCircuitOpen
from maple_chat.crawler.http import RespectfulInvenClient
from maple_chat.crawler.live import LiveIngestResult, ingest_live_board_page
from maple_chat.crawler.nexon_updates import OfficialNexonClient, sync_recent_patch_notes
from maple_chat.crawler.parser import ParserDriftError
from maple_chat.crawler.policy import CrawlScope, LiveCrawlDenied
from maple_chat.db.models import Board
from maple_chat.db.session import create_engine, create_session_factory
from maple_chat.indexing.embedding import EmbeddingProvider
from maple_chat.indexing.service import index_sanitized_sources
from maple_chat.knowledge.audit import audit_knowledge_terms
from maple_chat.knowledge.service import sync_builtin_knowledge
from maple_chat.llm.client import LocalLLMClient
from maple_chat.patch_reactions import (
    DatabasePatchReactionSearch,
    PatchReactionReport,
    PatchReactionReportWriter,
    PatchReactionRunner,
    load_patch_reaction_targets,
)
from maple_chat.runtime import (
    build_embedding_provider,
    check_database,
    embedding_spec,
    run_discord_bot,
)
from maple_chat.scheduler.planner import BOARD_NAMES, seed_boards
from maple_chat.time import KST, utc_now


def _listing_is_exhausted(result: LiveIngestResult) -> bool:
    return result.discovered_articles == 0


def _add_live_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--scope",
        choices=[scope.value for scope in CrawlScope],
        default=CrawlScope.APPROVED_SAMPLE.value,
    )
    parser.add_argument(
        "--board",
        dest="boards",
        action="append",
        type=int,
        choices=sorted(BOARD_NAMES),
        help="repeat for multiple boards; defaults to board 2304",
    )
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--max-pages", type=int, default=1)
    parser.add_argument("--max-articles", type=int, default=5)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--preserve-backfill-checkpoint", action="store_true")
    parser.add_argument("--no-comments", action="store_true")
    parser.add_argument("--no-ocr", action="store_true")
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="fetch details only for listing article IDs not already stored",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="maple-chat")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser(
        "validate-config",
        help="validate environment configuration without exposing secrets",
    )
    validate.add_argument("--role", choices=[role.value for role in ProcessRole], required=True)

    crawl = subparsers.add_parser("crawl-live", help="run one rights-gated live crawl slice")
    _add_live_arguments(crawl)

    sync = subparsers.add_parser(
        "sync-live",
        help="crawl a bounded live slice and immediately index changed sources",
    )
    _add_live_arguments(sync)
    sync.add_argument("--index-batch-size", type=int, default=32)

    index = subparsers.add_parser("index", help="embed and index all sanitized sources")
    index.add_argument("--batch-size", type=int, default=32)

    subparsers.add_parser(
        "sync-knowledge",
        help="validate and upsert the bundled provenance-bearing knowledge graph",
    )
    patch_notes = subparsers.add_parser(
        "sync-patch-notes",
        help="collect the rolling official MapleStory patch-note window and index changes",
    )
    patch_notes.add_argument("--since-days", type=int, default=365)
    patch_notes.add_argument("--max-pages", type=int, default=60)
    patch_notes.add_argument("--index-batch-size", type=int, default=32)
    patch_notes.add_argument("--no-index", action="store_true")
    patch_reactions = subparsers.add_parser(
        "summarize-patch-reactions",
        help="write one same-day community reaction report per unique playable job",
    )
    patch_reactions.add_argument(
        "--date",
        dest="patch_date",
        type=date.fromisoformat,
        default=None,
        help="KST patch date in YYYY-MM-DD form; defaults to today",
    )
    patch_reactions.add_argument(
        "--output-dir",
        type=Path,
        default=Path("reports/patch-reactions"),
    )
    patch_reactions.add_argument("--evidence-limit", type=int, default=8)
    patch_reactions.add_argument("--max-comments-per-article", type=int, default=12)
    patch_reactions.add_argument(
        "--job",
        dest="jobs",
        action="append",
        help="repeat to run selected canonical job names; defaults to all 48 unique jobs",
    )
    patch_reactions.add_argument("--overwrite", action="store_true")
    subparsers.add_parser(
        "audit-knowledge",
        help="compare collected terminology uses with curated canonical relations",
    )
    subparsers.add_parser("preflight", help="check database, local LLM, and model configuration")
    subparsers.add_parser("run-bot", help="start the single-guild Discord bot")
    return parser


def _validate_live_bounds(args: argparse.Namespace, scope: CrawlScope) -> tuple[int, ...]:
    boards = tuple(args.boards or [2304])
    if args.start_page < 1 or args.max_pages < 1 or args.max_pages > 10_000:
        raise ValueError("page bounds must be between 1 and 10000")
    if args.max_articles < 1 or args.max_articles > 100:
        raise ValueError("max articles must be between 1 and 100")
    if scope is CrawlScope.APPROVED_SAMPLE and (
        len(boards) != 1
        or args.start_page != 1
        or args.max_pages != 1
        or args.max_articles > 5
        or args.resume
    ):
        raise LiveCrawlDenied(
            "approved_sample is limited to one board, page 1, and at most five articles"
        )
    return boards


async def _crawl_live(args: argparse.Namespace) -> None:
    settings = load_settings(ProcessRole.CRAWLER_WORKER)
    if settings.pii_hash_salt is None:
        raise RuntimeError("crawler PII hash salt is unavailable")
    scope = CrawlScope(args.scope)
    boards = _validate_live_bounds(args, scope)
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        await check_database(factory)
        async with factory() as session, session.begin():
            await seed_boards(session)
        async with RespectfulInvenClient(settings) as client:
            for board_id in boards:
                first_page = args.start_page
                if args.resume:
                    async with factory() as session:
                        checkpoint = await session.scalar(
                            sa.select(Board.crawl_checkpoint).where(Board.board_id == board_id)
                        )
                    if checkpoint and checkpoint.get("backfill_complete") is True:
                        print(
                            json.dumps(
                                {"board_id": board_id, "status": "backfill_complete"},
                                sort_keys=True,
                            )
                        )
                        continue
                    if checkpoint:
                        first_page = int(checkpoint.get("next_backfill_page", first_page))
                for page in range(first_page, first_page + args.max_pages):
                    async with factory() as session, session.begin():
                        result = await ingest_live_board_page(
                            session,
                            client=client,
                            board_id=board_id,
                            page=page,
                            observed_at=utc_now(),
                            nickname_salt=settings.pii_hash_salt.get_secret_value(),
                            scope=scope,
                            max_articles=args.max_articles,
                            include_comments=not args.no_comments,
                            include_ocr=not args.no_ocr,
                            preserve_backfill_checkpoint=args.preserve_backfill_checkpoint,
                            skip_existing=args.skip_existing,
                        )
                    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
                    if _listing_is_exhausted(result):
                        break
    finally:
        await engine.dispose()


async def _index(batch_size: int) -> None:
    settings = load_settings(ProcessRole.INDEX_WORKER)
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    provider: EmbeddingProvider | None = None
    try:
        await check_database(factory)
        provider = build_embedding_provider(settings)
        result = await index_sanitized_sources(
            factory,
            provider=provider,
            activated_at=utc_now(),
            batch_size=batch_size,
        )
        print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
    finally:
        if provider is not None:
            await provider.aclose()
        await engine.dispose()


async def _preflight() -> None:
    settings = load_settings(ProcessRole.BOT)
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    llm = LocalLLMClient(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
        retries=0,
    )
    try:
        await check_database(factory)
        embedding_spec(settings).validate()
        ready = await llm.ready()
        status = {
            "database": "ready",
            "discord_configuration": "ready",
            "allowed_channel_count": len(settings.discord_channel_ids),
            "embedding_revision": embedding_spec(settings).name,
            "local_llm": "ready" if ready else "retrieval-only",
            "live_crawl_enabled": settings.live_crawl_enabled,
        }
        print(json.dumps(status, ensure_ascii=False, sort_keys=True))
    finally:
        await llm.aclose()
        await engine.dispose()


async def _sync_knowledge() -> None:
    settings = load_settings(ProcessRole.SCHEDULER)
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        await check_database(factory)
        async with factory() as session, session.begin():
            result = await sync_builtin_knowledge(session, synced_at=utc_now())
        print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
    finally:
        await engine.dispose()


async def _sync_patch_notes(args: argparse.Namespace) -> None:
    settings = load_settings(ProcessRole.SCHEDULER)
    if settings.crawler_user_agent is None:
        raise RuntimeError("CRAWLER_USER_AGENT is required for the official patch-note collector")
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        await check_database(factory)
        async with (
            OfficialNexonClient(
                settings.crawler_user_agent,
                contact=settings.crawler_contact,
            ) as client,
            factory() as session,
            session.begin(),
        ):
            result = await sync_recent_patch_notes(
                session,
                client=client,
                observed_at=utc_now(),
                since_days=args.since_days,
                max_pages=args.max_pages,
            )
        print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
    finally:
        await engine.dispose()
    if not args.no_index:
        await _index(args.index_batch_size)


def _reaction_progress(report: PatchReactionReport) -> None:
    print(
        json.dumps(
            {
                "index": report.index,
                "job": report.target.name,
                "families": report.target.families,
                "status": report.status.value,
                "evidence_count": len(report.evidence),
                "output_path": str(report.output_path),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


async def _summarize_patch_reactions(args: argparse.Namespace) -> None:
    if args.evidence_limit < 1 or args.evidence_limit > 30:
        raise ValueError("evidence limit must be between 1 and 30")
    if args.max_comments_per_article < 0 or args.max_comments_per_article > 50:
        raise ValueError("comment limit must be between 0 and 50")
    settings = load_settings(ProcessRole.SCHEDULER)
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    llm = LocalLLMClient(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
    )
    try:
        await check_database(factory)
        if not await llm.ready():
            raise RuntimeError("local LLM must be ready before patch reaction summaries start")
        targets = load_patch_reaction_targets()
        if args.jobs:
            requested = set(args.jobs)
            known = {target.name for target in targets}
            unknown = requested - known
            if unknown:
                raise ValueError(f"unknown canonical job names: {', '.join(sorted(unknown))}")
            targets = tuple(target for target in targets if target.name in requested)
        patch_date = args.patch_date or utc_now().astimezone(KST).date()
        runner = PatchReactionRunner(
            DatabasePatchReactionSearch(factory),
            llm,
            PatchReactionReportWriter(args.output_dir),
        )
        result = await runner.run(
            targets,
            patch_date=patch_date,
            evidence_limit=args.evidence_limit,
            max_comments_per_article=args.max_comments_per_article,
            overwrite=args.overwrite,
            on_progress=_reaction_progress,
        )
        print(
            json.dumps(
                {
                    "status": "complete",
                    "patch_date_kst": patch_date.isoformat(),
                    "total": result.total,
                    "summarized": result.summarized,
                    "insufficient": result.insufficient,
                    "generation_unavailable": result.unavailable,
                    "skipped": result.skipped,
                    "output_dir": str(args.output_dir / patch_date.isoformat()),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    finally:
        await llm.aclose()
        await engine.dispose()


async def _audit_knowledge() -> None:
    settings = load_settings(ProcessRole.SCHEDULER)
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        await check_database(factory)
        async with factory() as session:
            result = await audit_knowledge_terms(session, checked_at=utc_now())
        print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True, default=str))
    finally:
        await engine.dispose()


async def _dispatch(args: argparse.Namespace) -> None:
    if args.command == "crawl-live":
        await _crawl_live(args)
    elif args.command == "sync-live":
        await _crawl_live(args)
        await _index(args.index_batch_size)
    elif args.command == "index":
        await _index(args.batch_size)
    elif args.command == "sync-knowledge":
        await _sync_knowledge()
    elif args.command == "sync-patch-notes":
        await _sync_patch_notes(args)
    elif args.command == "summarize-patch-reactions":
        await _summarize_patch_reactions(args)
    elif args.command == "audit-knowledge":
        await _audit_knowledge()
    elif args.command == "preflight":
        await _preflight()
    elif args.command == "run-bot":
        await run_discord_bot(load_settings(ProcessRole.BOT))
    else:  # pragma: no cover - argparse prevents this
        raise RuntimeError("unknown command")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate-config":
        try:
            settings = load_settings(ProcessRole(args.role))
        except ConfigurationError as exc:
            print(f"configuration error: {exc}")
            return 2
        print(
            f"configuration valid for role={args.role}; "
            f"live_crawl_enabled={settings.live_crawl_enabled}"
        )
        return 0

    try:
        asyncio.run(_dispatch(args))
    except ConfigurationError as exc:
        print(f"configuration error: {exc}")
        return 2
    except LiveCrawlDenied as exc:
        print(f"live crawl denied: {exc}")
        return 3
    except (CrawlCircuitOpen, ParserDriftError) as exc:
        print(f"live crawl stopped safely: {exc}")
        return 4
    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
        print(f"runtime error [{type(exc).__name__}]: {exc}")
        return 5
    except KeyboardInterrupt:
        return 130
    return 0
