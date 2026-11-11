#!/usr/bin/env python3
"""Extract candidate job guide posts and comments from PostgreSQL articles table."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from sqlalchemy import text

from maple_chat.config import ProcessRole, load_settings
from maple_chat.db.session import create_engine, create_session_factory


async def extract_candidates(
    job_name: str,
    *,
    limit: int = 5,
    min_recs: int = 0,
    half_life_days: float = 45.0,
) -> list[dict[str, Any]]:
    settings = load_settings(ProcessRole.INDEX_WORKER)
    engine = create_engine(settings)
    factory = create_session_factory(engine)

    article_sql = text("""
        SELECT
            id,
            board_id,
            title,
            view_count,
            recommendation_count,
            comment_count,
            COALESCE(published_at, created_at) AS article_date,
            ROUND(
                (
                    (recommendation_count * 5.0 + LN(view_count + 1) * 2.0 + comment_count * 1.0)
                    * POWER(0.5, GREATEST(0.0, EXTRACT(EPOCH FROM (NOW() - COALESCE(published_at, created_at))) / 86400.0) / :half_life_days)
                )::numeric, 2
            ) AS recency_score,
            sanitized_body
        FROM articles
        WHERE (title ILIKE :job_pattern OR sanitized_body ILIKE :job_pattern)
          AND (
              title ILIKE '%극딜%'
              OR title ILIKE '%딜사이클%'
              OR title ILIKE '%공략%'
              OR title ILIKE '%순서%'
              OR title ILIKE '%코강%'
              OR title ILIKE '%스킬%'
              OR (sanitized_body ILIKE '%극딜%' AND sanitized_body ILIKE '%순서%')
              OR (sanitized_body ILIKE '%오리진%' AND sanitized_body ILIKE '%버프%')
          )
          AND recommendation_count >= :min_recs
        ORDER BY (CASE WHEN title ILIKE :job_pattern THEN 1 ELSE 0 END) DESC, recency_score DESC
        LIMIT :limit;
    """)

    comment_sql = text("""
        SELECT id, sanitized_body
        FROM comments
        WHERE article_id = :article_id
        ORDER BY id ASC
        LIMIT 10;
    """)

    results: list[dict[str, Any]] = []
    async with factory() as session:
        res = await session.execute(
            article_sql,
            {"job_pattern": f"%{job_name}%", "limit": limit, "min_recs": min_recs, "half_life_days": half_life_days},
        )
        articles = res.fetchall()
        for row in articles:
            article_id = row.id
            comm_res = await session.execute(comment_sql, {"article_id": article_id})
            comments = [c.sanitized_body for c in comm_res.fetchall()]
            dt_str = row.article_date.strftime("%Y-%m-%d") if row.article_date else "N/A"
            results.append({
                "id": article_id,
                "title": row.title,
                "board_id": row.board_id,
                "views": row.view_count,
                "recs": row.recommendation_count,
                "date": dt_str,
                "recency_score": float(row.recency_score or 0.0),
                "body": row.sanitized_body,
                "comments": comments,
            })

    await engine.dispose()
    return results


def main():
    parser = argparse.ArgumentParser(description="Extract job guide candidates from DB")
    parser.add_argument("--job", required=True, help="Job canonical name (e.g. 나이트로드, 나이트워커, 히어로)")
    parser.add_argument("--limit", type=int, default=5, help="Max articles to extract")
    parser.add_argument("--min-recs", type=int, default=0, help="Min recommendations")
    parser.add_argument("--out", help="Output JSON file path")
    args = parser.parse_args()

    candidates = asyncio.run(extract_candidates(args.job, limit=args.limit, min_recs=args.min_recs))
    print(f"Found {len(candidates)} candidate articles for '{args.job}'.")
    for idx, c in enumerate(candidates):
        print(f"[{idx + 1}] ID: {c['id']} | Title: {c['title']} (recs: {c['recs']}, views: {c['views']})")
        print(f"    Body snippet: {c['body'][:200]}...")
        if c['comments']:
            print(f"    Comments: {len(c['comments'])} (sample: {c['comments'][0][:100]}...)")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(candidates, f, ensure_ascii=False, indent=2)
        print(f"Saved candidates to {args.out}")


if __name__ == "__main__":
    main()
