#!/usr/bin/env python3
"""Batch generate combat guides for all MapleStory jobs using DB candidates + Gemini API."""

from __future__ import annotations

import argparse
import asyncio
import datetime
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import requests
from sqlalchemy import text

from maple_chat.config import ProcessRole, load_settings
from maple_chat.db.session import create_engine, create_session_factory
from maple_chat.knowledge.skill_catalog import (
    get_grounded_v_matrix,
    get_job_skill_whitelist,
    validate_guide_skills,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TAXONOMY_PATH = PROJECT_ROOT / "src" / "maple_chat" / "knowledge" / "data" / "job-taxonomy-v1.json"
GUIDES_DIR = PROJECT_ROOT / "src" / "maple_chat" / "skills" / "guides"
HISTORY_DIR = GUIDES_DIR / "history"


def load_taxonomy_jobs() -> list[dict[str, Any]]:
    with open(TAXONOMY_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    ent_map = {e['entity_id']: e['canonical_name'] for e in data['entities']}
    rel_map = {r['subject_entity_id']: ent_map.get(r['object_entity_id'], '기타') for r in data.get('relations', []) if r.get('predicate') == 'is_a'}
    
    jobs: list[dict[str, Any]] = []
    for e in data['entities']:
        if e.get('entity_type') in ('job', 'character_job'):
            name = e['canonical_name']
            family = rel_map.get(e['entity_id']) or e.get('category') or e.get('attributes', {}).get('job_family', '기타')
            jobs.append({
                'canonical_name': name,
                'job_family': family,
                'aliases': e.get('aliases', [])
            })
    return jobs


async def extract_db_candidates(
    job_name: str,
    aliases: list[str],
    limit: int = 4,
    half_life_days: float = 45.0,
) -> list[dict[str, Any]]:
    try:
        settings = load_settings(ProcessRole.INDEX_WORKER)
        engine = create_engine(settings)
        factory = create_session_factory(engine)
    except Exception as exc:
        print(f"[WARN] DB connection failed: {exc}")
        return []

    search_terms = [job_name] + [a for a in aliases if len(a) >= 2][:2]
    like_clauses = " OR ".join([f"title ILIKE :term_{i} OR sanitized_body ILIKE :term_{i}" for i in range(len(search_terms))])
    params = {f"term_{i}": f"%{t}%" for i, t in enumerate(search_terms)}
    params["primary_term"] = f"%{job_name}%"
    params["half_life_days"] = half_life_days
    params["limit"] = limit

    article_sql = text(f"""
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
        WHERE ({like_clauses})
          AND (
              title ILIKE '%극딜%' OR title ILIKE '%딜사이클%' OR title ILIKE '%공략%'
              OR title ILIKE '%순서%' OR title ILIKE '%코강%' OR title ILIKE '%스킬%'
              OR (sanitized_body ILIKE '%극딜%' AND sanitized_body ILIKE '%순서%')
              OR (sanitized_body ILIKE '%오리진%' AND sanitized_body ILIKE '%버프%')
          )
        ORDER BY (CASE WHEN title ILIKE :primary_term THEN 1 ELSE 0 END) DESC, recency_score DESC
        LIMIT :limit;
    """)

    comment_sql = text("""
        SELECT id, sanitized_body
        FROM comments
        WHERE article_id = :article_id
        ORDER BY id ASC
        LIMIT 5;
    """)

    results = []
    try:
        async with factory() as session:
            res = await session.execute(article_sql, params)
            articles = res.fetchall()
            for row in articles:
                comm_res = await session.execute(comment_sql, {"article_id": row.id})
                comments = [c.sanitized_body.strip() for c in comm_res.fetchall() if c.sanitized_body.strip()]
                dt_str = row.article_date.strftime("%Y-%m-%d") if row.article_date else "N/A"
                results.append({
                    "id": row.id,
                    "title": row.title,
                    "recs": row.recommendation_count,
                    "views": row.view_count,
                    "date": dt_str,
                    "score": float(row.recency_score or 0.0),
                    "body": row.sanitized_body[:1500],
                    "comments": comments[:3],
                })
    except Exception as exc:
        print(f"[WARN] Query error for {job_name}: {exc}")
    finally:
        await engine.dispose()

    return results


def call_local_llm_for_guide(
    job_name: str,
    job_family: str,
    candidates: list[dict[str, Any]],
    endpoint: str = "http://127.0.0.1:8001/v1",
    model: str = "local-coder",
) -> dict[str, Any]:
    context_str = ""
    source_ids = []
    if candidates:
        context_str = "### 크롤링된 커뮤니티 공략 및 분석글 데이터 (최신성 가중 순위):\n"
        for c in candidates[:3]:
            source_ids.append(c['id'])
            body_snippet = c['body'][:800]
            context_str += f"[글 #{c['id']} - {c['title']} (작성일: {c['date']}, 추천 {c['recs']}, 조회 {c['views']}, 최신성 점수 {c['score']})]\n"
            context_str += f"{body_snippet}\n"
            if c['comments']:
                context_str += "[핵심 댓글]\n" + "\n".join(f"- {cm}" for cm in c['comments'][:2]) + "\n"
            context_str += "---\n"
    else:
        context_str = "(해당 직업에 대한 커뮤니티 데이터가 적거나 없음. 메이플스토리 최신 6차 전직, 오리진 스킬, 마스터리 코어, 시드링, 링크/유니온 메커니즘 공식 지식을 바탕으로 정확히 작성할 것)\n"

    whitelist = get_job_skill_whitelist(job_name)
    whitelist_str = ""
    if whitelist:
        whitelist_str = f"""
[공식 스킬 화이트리스트 (스킬명 환각 방지 엄격 제약)]
아래 목록은 메이플스토리 공식 인게임 스킬명 목록입니다.
burst_rotation의 steps, pre_buff 및 core_v_matrix 작성 시 반드시 아래 목록에 존재하는 스킬명만 정확히 사용해야 하며, 목록에 없는 가상 스킬명(예: 킬링 커트, 어드레날린 러쉬, 인텐시브 인스팅트 등)을 임의로 지어내는 것을 엄격히 금지합니다:
- {', '.join(whitelist)}
"""

    today_str = datetime.date.today().isoformat()
    prompt = f"""당신은 메이플스토리 최고 수준의 전투 메커니즘 및 직업 공략 전문가입니다.
제공된 커뮤니티 실전 공략 데이터와 메이플스토리 최신 게임 지식(6차 전직 오리진, 마스터리 코어, 리레/웨펖 딜압축, 딜사이클 순서, 코어강화)을 바탕으로 아래 직업의 완벽한 실전 공략 가이드 JSON을 작성하세요.

[대상 직업]
- 직업명: {job_name}
- 직업군: {job_family}

{whitelist_str}

{context_str}

[작성 규칙]
1. 반드시 오직 유효한 단일 JSON 객체만을 출력해야 합니다. 마크다운 따옴표(```json)나 부가 설명 없이 순수 JSON만 출력하세요.
2. 환각(없는 스킬명 지어내기)을 절대 금지하며, 반드시 해당 직업의 실제 5차/6차/오리진/하이퍼/주력기 스킬명을 정확히 사용하세요.
3. burst_rotation의 steps는 1단계부터 번호를 매겨 버프 시전 순서, 디버프/설치, 오리진 및 바인드, 핵심 딜압축(키다운/연계), 시드링 활용, 마무리까지 실전에서 쓰이는 완벽한 순서로 작성하세요.
4. critical_tip에는 유저들이 자주 실수하거나 딜 누수를 방지하는 실전 팁(예: 캔슬법, 특정 스킬 동시 시전, 수동 발동 요령 등)을 명확히 기재하세요.
5. 최신 정보 우선 원칙: 각 공략글의 작성일자(date)와 최신성 점수(score)를 최우선으로 고려하세요. 가장 최근에 작성된 공략글의 6차 마스터리 코어, 최신 밸런스 패치 후 딜사이클 및 코강 우선순위를 최우선으로 반영하세요.
6. 필수 필드 스키마:
{{
  "job_name": "{job_name}",
  "job_family": "{job_family}",
  "summary": "직업의 전투 스타일, 극딜 및 평딜 주기(예: 120초/180초), 특성 한 줄 요약",
  "burst_rotation": {{
    "title": "오리진 포함 풀 극딜 사이클 (시드링 압축 연계)",
    "pre_buff": ["사전 시전해야 할 버프 목록"],
    "steps": ["1. 단계별 설명", "2. 단계별 설명"],
    "critical_tip": "실전 필수 팁 및 주의사항"
  }},
  "sub_burst_and_normal": {{
    "title": "준극딜 및 평딜 사이클",
    "steps": ["평딜 및 준극딜 운용법"]
  }},
  "core_v_matrix": {{
    "스킬_코어": ["우선순위 스킬 코어 목록"],
    "강화_코어": ["3줄 유효 필수 강화 코어 목록"],
    "6차_코어": ["오리진 및 마스터리 코어"]
  }},
  "metadata": {{
    "last_updated": "{today_str}",
    "verified": true,
    "recency_weighted": true,
    "model": "{model}",
    "provider": "local",
    "source_article_ids": {json.dumps(source_ids)}
  }},
  "data_source": "Maple Chat 크롤링 DB 및 최신 실전 공략 기반 정제 데이터 (Local LLM)"
}}
"""

    url = f"{endpoint.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "당신은 메이플스토리 최고 수준의 전투 메커니즘 및 직업 공략 전문가입니다. 반드시 오직 유효한 단일 JSON 객체만을 출력하세요."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 3000,
        "temperature": 0.1,
        "chat_template_kwargs": {"enable_thinking": False},
    }

    print(f"  [LOCAL-LLM] Sending request to {url} (model={model}, prompt_len={len(prompt)} chars)...")
    for attempt in range(3):
        try:
            t_req = time.time()
            resp = requests.post(url, json=payload, timeout=180)
            print(f"  [LOCAL-LLM] Response received in {round(time.time()-t_req, 2)}s (status {resp.status_code})")
            if resp.status_code == 200:
                data = resp.json()
                msg = data['choices'][0]['message']
                text_content = msg.get('content') or ""
                clean_text = text_content.strip()
                if clean_text.startswith("```json"):
                    clean_text = clean_text[7:]
                if clean_text.startswith("```"):
                    clean_text = clean_text[3:]
                if clean_text.endswith("```"):
                    clean_text = clean_text[:-3]
                clean_text = clean_text.strip()
                # Find the outermost JSON block
                start_idx = clean_text.find("{")
                end_idx = clean_text.rfind("}")
                if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                    clean_text = clean_text[start_idx:end_idx+1]
                parsed = json.loads(clean_text)
                parsed["job_name"] = job_name
                parsed["job_family"] = job_family

                # Grounding: Enforce verified core_v_matrix from skill catalog if available
                grounded_vm = get_grounded_v_matrix(job_name)
                if grounded_vm:
                    parsed["core_v_matrix"] = grounded_vm
                    print(f"  [GROUNDING] Overwrote core_v_matrix with verified catalog data for {job_name}")

                val_res = validate_guide_skills(parsed, job_name)
                parsed.setdefault("metadata", {})["skill_validation"] = val_res
                print(f"  [SKILL-VALIDATION] Valid: {val_res['valid']} | Match: {val_res['matched_count']} / {val_res['matched_count'] + val_res['unknown_count']} ({val_res['match_ratio']*100:.1f}%)")
                if val_res['unknown_skills']:
                    print(f"  [SKILL-VALIDATION] Unknown detected: {val_res['unknown_skills']}")
                return parsed
            else:
                print(f"[WARN] Local LLM error (attempt {attempt+1}): {resp.status_code} {resp.text[:200]}")
                time.sleep(2)
        except Exception as exc:
            print(f"[WARN] Local LLM request failed (attempt {attempt+1}): {exc}")
            time.sleep(3)

    raise RuntimeError(f"Failed to generate guide for {job_name} using Local LLM ({model})")


def call_gemini_for_guide(job_name: str, job_family: str, candidates: list[dict[str, Any]], api_key: str) -> dict[str, Any]:
    context_str = ""
    source_ids = []
    if candidates:
        context_str = "### 크롤링된 커뮤니티 공략 및 분석글 데이터 (최신성 가중 순위):\n"
        for c in candidates:
            source_ids.append(c['id'])
            context_str += f"[글 #{c['id']} - {c['title']} (작성일: {c['date']}, 추천 {c['recs']}, 조회 {c['views']}, 최신성 점수 {c['score']})]\n"
            context_str += f"{c['body']}\n"
            if c['comments']:
                context_str += "[핵심 댓글]\n" + "\n".join(f"- {cm}" for cm in c['comments']) + "\n"
            context_str += "---\n"
    else:
        context_str = "(해당 직업에 대한 커뮤니티 데이터가 적거나 없음. 메이플스토리 최신 6차 전직, 오리진 스킬, 마스터리 코어, 시드링, 링크/유니온 메커니즘 공식 지식을 바탕으로 정확히 작성할 것)\n"

    today_str = datetime.date.today().isoformat()
    prompt = f"""당신은 메이플스토리 최고 수준의 전투 메커니즘 및 직업 공략 전문가입니다.
제공된 커뮤니티 실전 공략 데이터와 메이플스토리 최신 게임 지식(6차 전직 오리진, 마스터리 코어, 리레/웨펖 딜압축, 딜사이클 순서, 코어강화)을 바탕으로 아래 직업의 완벽한 실전 공략 가이드 JSON을 작성하세요.

[대상 직업]
- 직업명: {job_name}
- 직업군: {job_family}

{context_str}

[작성 규칙]
1. 반드시 오직 유효한 단일 JSON 객체만을 출력해야 합니다. 마크다운 따옴표(```json)나 부가 설명 없이 순수 JSON만 출력하세요.
2. 환각(없는 스킬명 지어내기)을 절대 금지하며, 반드시 해당 직업의 실제 5차/6차/오리진/하이퍼/주력기 스킬명을 정확히 사용하세요.
3. burst_rotation의 steps는 1단계부터 번호를 매겨 버프 시전 순서, 디버프/설치, 오리진 및 바인드, 핵심 딜압축(키다운/연계), 시드링 활용, 마무리까지 실전에서 쓰이는 완벽한 순서로 작성하세요.
4. critical_tip에는 유저들이 자주 실수하거나 딜 누수를 방지하는 실전 팁(예: 캔슬법, 특정 스킬 동시 시전, 수동 발동 요령 등)을 명확히 기재하세요.
5. 최신 정보 우선 원칙: 각 공략글의 작성일자(date)와 최신성 점수(score)를 최우선으로 고려하세요. 가장 최근에 작성된 공략글의 6차 마스터리 코어, 최신 밸런스 패치 후 딜사이클 및 코강 우선순위를 최우선으로 반영하세요. 과거 글과 최근 글의 내용이 상충할 경우 반드시 최근 글을 기준으로 채택하세요.
6. 필수 필드 스키마:
{{
  "job_name": "{job_name}",
  "job_family": "{job_family}",
  "summary": "직업의 전투 스타일, 극딜 및 평딜 주기(예: 120초/180초), 특성 한 줄 요약",
  "burst_rotation": {{
    "title": "오리진 포함 풀 극딜 사이클 (시드링 압축 연계)",
    "pre_buff": ["사전 시전해야 할 버프 목록"],
    "steps": ["1. 단계별 설명", "2. 단계별 설명"],
    "critical_tip": "실전 필수 팁 및 주의사항"
  }},
  "sub_burst_and_normal": {{
    "title": "준극딜 및 평딜 사이클",
    "steps": ["평딜 및 준극딜 운용법"]
  }},
  "core_v_matrix": {{
    "스킬_코어": ["우선순위 스킬 코어 목록"],
    "강화_코어": ["3줄 유효 필수 강화 코어 목록"],
    "6차_코어": ["오리진 및 마스터리 코어"]
  }},
  "metadata": {{
    "last_updated": "{today_str}",
    "verified": true,
    "recency_weighted": true,
    "source_article_ids": {json.dumps(source_ids)}
  }},
  "data_source": "Maple Chat 크롤링 DB 및 최신 실전 공략 기반 정제 데이터"
}}
"""

    models = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.2
        }
    }

    for model_name in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
        for attempt in range(3):
            try:
                resp = requests.post(url, json=payload, timeout=60)
                if resp.status_code == 200:
                    data = resp.json()
                    text_content = data['candidates'][0]['content']['parts'][0]['text']
                    clean_text = text_content.strip()
                    if clean_text.startswith("```json"):
                        clean_text = clean_text[7:]
                    if clean_text.startswith("```"):
                        clean_text = clean_text[3:]
                    if clean_text.endswith("```"):
                        clean_text = clean_text[:-3]
                    clean_text = clean_text.strip()
                    parsed = json.loads(clean_text)
                    parsed["job_name"] = job_name
                    parsed["job_family"] = job_family
                    return parsed
                elif resp.status_code in (429, 503):
                    print(f"[WARN] {model_name} rate limit ({resp.status_code})...")
                    time.sleep(5)
                else:
                    print(f"[WARN] {model_name} error: {resp.status_code}")
                    time.sleep(3)
            except Exception as exc:
                print(f"[WARN] Request failed: {exc}")
                time.sleep(4)

    raise RuntimeError(f"Failed to generate guide for {job_name} after 3 attempts")


def validate_sync_and_commit(updated_jobs: list[str]) -> bool:
    if not updated_jobs:
        print("[INFO] No jobs updated, skipping commit.")
        return True

    print("\n--- [AUTO-COMMIT] 1. Running Catalog & Skills Unit Tests ---")
    ret = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/unit/test_job_guide_catalog.py",
            "tests/unit/test_job_guide_skills.py",
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    if ret.returncode != 0:
        print("[ERROR] Unit tests failed! Aborting auto-commit and rolling back.")
        print(ret.stdout)
        print(ret.stderr)
        # Rollback modified guides
        for j in updated_jobs:
            p = GUIDES_DIR / f"{j}.json"
            subprocess.run(["git", "checkout", "--", str(p)], cwd=str(PROJECT_ROOT), capture_output=True)
        return False

    print("[PASS] Catalog and skill unit tests passed successfully.")

    # 2. Sync to production directory if separate
    prod_root = Path("/home/hyuns/project/Maple_Chat")
    if prod_root.exists() and prod_root != PROJECT_ROOT:
        print(f"--- [AUTO-COMMIT] 2. Syncing updated guides to production: {prod_root} ---")
        prod_guides = prod_root / "src" / "maple_chat" / "skills" / "guides"
        prod_guides.mkdir(parents=True, exist_ok=True)
        prod_history = prod_guides / "history"
        prod_history.mkdir(parents=True, exist_ok=True)
        if HISTORY_DIR.exists():
            for h_file in HISTORY_DIR.glob("*.json"):
                shutil.copy2(h_file, prod_history / h_file.name)
        for j in updated_jobs:
            src_f = GUIDES_DIR / f"{j}.json"
            if src_f.exists():
                shutil.copy2(src_f, prod_guides / f"{j}.json")
        for f_rel in ["src/maple_chat/skills/job_guide.py", "scripts/generate_all_job_guides.py", "scripts/extract_job_guide_candidates.py", "src/maple_chat/knowledge/skill_catalog.py", "src/maple_chat/knowledge/data/job-skill-catalog-v1.json", "scripts/build_skill_catalog.py"]:
            s = PROJECT_ROOT / f_rel
            d = prod_root / f_rel
            if s.exists():
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(s, d)

    # 3. Git add and commit in current project worktree
    print("--- [AUTO-COMMIT] 3. Committing changes to git ---")
    add_cmd = subprocess.run(
        ["git", "add", "src/maple_chat/skills/", "src/maple_chat/knowledge/", "scripts/", "tests/unit/test_job_guide*"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    if add_cmd.returncode != 0:
        print(f"[WARN] git add failed: {add_cmd.stderr}")
        return False

    job_summary = ", ".join(updated_jobs[:3]) + (f" 외 {len(updated_jobs)-3}개" if len(updated_jobs) > 3 else "")
    commit_msg = f"chore(guide): auto-update combat guides with recency weighting ({job_summary})"
    commit_cmd = subprocess.run(
        ["git", "commit", "-m", commit_msg],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    if commit_cmd.returncode == 0:
        print(f"[SUCCESS] Auto-commit completed:\n{commit_cmd.stdout.strip()}")
        return True
    else:
        if "nothing to commit" in commit_cmd.stdout or "nothing to commit" in commit_cmd.stderr:
            print("[INFO] Clean working directory, nothing new to commit.")
            return True
        print(f"[WARN] git commit failed: {commit_cmd.stdout} {commit_cmd.stderr}")
        return False


def print_guide_comparison(job_name: str, old_guide: dict[str, Any], new_guide: dict[str, Any]):
    print(f"\n=======================================================")
    print(f"   [COMPARE] {job_name} 가이드 비교 (Gemini vs Local LLM)")
    print(f"=======================================================")
    old_provider = old_guide.get("metadata", {}).get("provider", "gemini")
    new_provider = new_guide.get("metadata", {}).get("provider", "local")
    old_model = old_guide.get("metadata", {}).get("model", "gemini-flash")
    new_model = new_guide.get("metadata", {}).get("model", "local-coder")
    print(f"[기존 ({old_provider} / {old_model})] vs [신규 ({new_provider} / {new_model})]\n")

    print(f"■ 요약 (Summary):")
    print(f"  - 기존: {old_guide.get('summary')}")
    print(f"  - 신규: {new_guide.get('summary')}\n")

    old_burst = old_guide.get("burst_rotation", {})
    new_burst = new_guide.get("burst_rotation", {})
    print(f"■ 극딜 사이클 타이틀:")
    print(f"  - 기존: {old_burst.get('title')}")
    print(f"  - 신규: {new_burst.get('title')}\n")

    print(f"■ 사전 버프 (Pre-buff):")
    print(f"  - 기존: {old_burst.get('pre_buff')}")
    print(f"  - 신규: {new_burst.get('pre_buff')}\n")

    print(f"■ 극딜 순서 (Steps):")
    print(f"  [기존 Gemini (총 {len(old_burst.get('steps', []))}단계)]")
    for s in old_burst.get('steps', []):
        print(f"    {s}")
    print(f"  [신규 Local LLM (총 {len(new_burst.get('steps', []))}단계)]")
    for s in new_burst.get('steps', []):
        print(f"    {s}")
    print()

    print(f"■ 실전 핵심 팁 (Critical Tip):")
    print(f"  - 기존: {old_burst.get('critical_tip')}")
    print(f"  - 신규: {new_burst.get('critical_tip')}\n")

    old_v = old_guide.get("core_v_matrix", {})
    new_v = new_guide.get("core_v_matrix", {})
    print(f"■ V 매트릭스 & 6차 코어:")
    print(f"  - 기존: 스킬={old_v.get('스킬_코어')}, 6차={old_v.get('6차_코어')}")
    print(f"  - 신규: 스킬={new_v.get('스킬_코어')}, 6차={new_v.get('6차_코어')}")
    print(f"=======================================================\n")


async def process_all_jobs(
    target_jobs: list[str] | None = None,
    delay: float = 2.0,
    half_life_days: float = 45.0,
    auto_commit: bool = False,
    force: bool = False,
    provider: str = "local",
    endpoint: str = "http://127.0.0.1:8001/v1",
    model: str = "local-coder",
    compare: bool = True,
    output_dir: Path | None = None,
):
    target_guides_dir = output_dir or GUIDES_DIR
    target_guides_dir.mkdir(parents=True, exist_ok=True)

    api_key = None
    if provider == "gemini":
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY environment variable is missing")

    all_jobs = load_taxonomy_jobs()
    existing_files = {os.path.splitext(os.path.basename(f))[0] for f in glob.glob(str(target_guides_dir / "*.json"))}

    jobs_to_run = []
    for j in all_jobs:
        name = j['canonical_name']
        if target_jobs and name not in target_jobs:
            continue
        if name in existing_files and not target_jobs and not force:
            continue
        jobs_to_run.append(j)

    print(f"Total taxonomy jobs: {len(all_jobs)}, Existing: {len(existing_files)}, To process: {len(jobs_to_run)}")
    print(f"Provider: {provider} ({model if provider=='local' else 'gemini-flash'}) | Half-life: {half_life_days}d | Auto-commit: {auto_commit}")

    updated_jobs = []
    for idx, j in enumerate(jobs_to_run):
        name = j['canonical_name']
        family = j['job_family']
        aliases = j['aliases']
        print(f"\n[{idx+1}/{len(jobs_to_run)}] Processing '{name}' ({family}) via {provider}...")

        candidates = await extract_db_candidates(name, aliases, limit=4, half_life_days=half_life_days)
        print(f"  Extracted {len(candidates)} DB candidates with recency weighting")

        try:
            out_path = target_guides_dir / f"{name}.json"
            prev_data = None
            if out_path.exists():
                try:
                    with open(out_path, "r", encoding="utf-8") as f_prev:
                        prev_data = json.load(f_prev)
                    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
                    prev_date = prev_data.get("metadata", {}).get("last_updated", "legacy")
                    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                    history_file = HISTORY_DIR / f"{name}_{prev_date}_{ts}.json"
                    with open(history_file, "w", encoding="utf-8") as f_hist:
                        json.dump(prev_data, f_hist, ensure_ascii=False, indent=2)
                    print(f"  [ARCHIVE] Previous version saved: history/{history_file.name}")
                except Exception as h_err:
                    print(f"  [WARN] Failed to archive previous version: {h_err}")

            if provider == "local":
                guide_json = call_local_llm_for_guide(name, family, candidates, endpoint=endpoint, model=model)
            else:
                guide_json = call_gemini_for_guide(name, family, candidates, api_key)

            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(guide_json, f, ensure_ascii=False, indent=2)
            print(f"  Successfully generated and saved: {out_path.name}")
            updated_jobs.append(name)

            if compare and prev_data:
                print_guide_comparison(name, prev_data, guide_json)

        except Exception as exc:
            print(f"  [ERROR] Failed to generate guide for {name}: {exc}")

        time.sleep(delay)

    print(f"\n=== Generation Complete ({len(updated_jobs)} guides updated) ===")

    if auto_commit and updated_jobs:
        validate_sync_and_commit(updated_jobs)


def main():
    parser = argparse.ArgumentParser(description="Generate/Update job combat guides with recency weighting & auto-commit")
    parser.add_argument("jobs", nargs="*", help="Optional job canonical names")
    parser.add_argument("--jobs", dest="jobs_list", nargs="+", help="List of jobs")
    parser.add_argument("--all", action="store_true", help="Process all 48 jobs")
    parser.add_argument("--force", action="store_true", help="Force regenerate existing guides")
    parser.add_argument("--auto-commit", action="store_true", default=False, help="Automatically run tests, sync, and git commit upon success")
    parser.add_argument("--half-life-days", type=float, default=45.0, help="Recency decay half-life in days (default: 45.0)")
    parser.add_argument("--delay", type=float, default=2.0, help="Delay between API calls in seconds (default: 2.0)")
    parser.add_argument("--provider", choices=["local", "gemini"], default="local", help="LLM provider: 'local' (vLLM local-coder) or 'gemini' (default: local)")
    parser.add_argument("--endpoint", default="http://127.0.0.1:8001/v1", help="Local LLM API endpoint (default: http://127.0.0.1:8001/v1)")
    parser.add_argument("--model", default="local-coder", help="Model ID for Local LLM (default: local-coder)")
    parser.add_argument("--no-compare", action="store_true", help="Disable printing comparison with previous guide")
    args = parser.parse_args()

    targets = []
    if args.jobs:
        targets.extend(args.jobs)
    if args.jobs_list:
        targets.extend(args.jobs_list)
    target_param = targets if targets and not args.all else None

    asyncio.run(
        process_all_jobs(
            target_jobs=target_param,
            delay=args.delay,
            half_life_days=args.half_life_days,
            auto_commit=args.auto_commit,
            force=args.force,
            provider=args.provider,
            endpoint=args.endpoint,
            model=args.model,
            compare=not args.no_compare,
        )
    )


if __name__ == "__main__":
    main()
