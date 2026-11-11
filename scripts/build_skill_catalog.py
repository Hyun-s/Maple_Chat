#!/usr/bin/env python3
"""Build job skill catalog including verified core_v_matrix ground truth."""
from __future__ import annotations

import glob
import json
import os
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GUIDES_DIR = PROJECT_ROOT / "src" / "maple_chat" / "skills" / "guides"
HISTORY_DIR = GUIDES_DIR / "history"
OUTPUT_PATH = PROJECT_ROOT / "src" / "maple_chat" / "knowledge" / "data" / "job-skill-catalog-v1.json"

COMMON_SKILLS = [
    "소울 컨트랙트", "에픽 어드벤처", "글로리 오브 가디언즈", "레디 투 다이",
    "메이플 여신의 축복", "여제의 축복", "이계의 여신 축복", "오버로드 마나",
    "리스트레인트 링", "웨폰퍼프 링", "컨티뉴어스 링", "크라이시스-HM링",
    "메이플 용사", "쓸만한 샤프 아이즈", "쓸만한 컴뱃 오더스", "쓸만한 윈드 부스터",
    "에르다 노바", "에르다 파운틴", "블링크"
]

def clean_skill_name(raw: str) -> str:
    s = re.sub(r"^[0-9]+\.\s*", "", raw)
    s = re.sub(r"^(오리진|마스터리 코어|스킬 코어|시퀀스 버프|하이퍼|5차|6차)[\s:]*", "", s)
    s = re.sub(r"\([^)]*\)", "", s)
    s = re.sub(r"\[[^\]]*\]", "", s)
    return s.strip()

def build_catalog():
    jobs_catalog = {}
    guide_files = sorted(glob.glob(str(GUIDES_DIR / "*.json")))

    for gf in guide_files:
        try:
            with open(gf, "r", encoding="utf-8") as f:
                data = json.load(f)
            job_name = data.get("job_name")
            if not job_name:
                continue

            family = data.get("job_family", "전사")
            vm = data.get("core_v_matrix", {})

            # Special clean curated ground-truth for 섀도어
            if job_name == "섀도어":
                v_matrix_ground_truth = {
                    "스킬_코어": ["소닉 블로우", "절개", "얼티밋 다크사이드", "쉐도우 어썰트", "메이플 여신의 축복"],
                    "강화_코어": ["암살 / 메소 익스플로전 / 섀도어 인스팅트", "베일 오브 섀도우 / 크루얼 스테브 / 그리드"],
                    "6차_코어": ["오리진: 일격필살 (멸귀강참)", "마스터리 코어: 암살 VI", "마스터리 코어: 메소 익스플로전 VI", "무아지경 (:멸)"]
                }
            else:
                v_matrix_ground_truth = {
                    "스킬_코어": vm.get("스킬_코어", []),
                    "강화_코어": vm.get("강화_코어", []),
                    "6차_코어": vm.get("6차_코어", []),
                }

            s_core = [clean_skill_name(s) for s in v_matrix_ground_truth["스킬_코어"] if clean_skill_name(s)]
            m_core = [clean_skill_name(s) for s in v_matrix_ground_truth["6차_코어"] if clean_skill_name(s)]
            e_core = []
            for ec in v_matrix_ground_truth["강화_코어"]:
                for part in re.split(r"[/,+]", ec):
                    cp = clean_skill_name(part)
                    if cp and len(cp) >= 2:
                        e_core.append(cp)

            burst = data.get("burst_rotation", {})
            pb = [clean_skill_name(s) for s in burst.get("pre_buff", []) if clean_skill_name(s)]

            all_set = set(s_core + m_core + e_core + pb + COMMON_SKILLS)
            jobs_catalog[job_name] = {
                "job_family": family,
                "v_matrix": v_matrix_ground_truth,
                "official_skills": sorted(list(all_set)),
            }
        except Exception as e:
            print(f"Error reading {gf}: {e}")

    catalog_data = {
        "schema_version": 1,
        "common_skills": COMMON_SKILLS,
        "total_jobs": len(jobs_catalog),
        "jobs": jobs_catalog,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(catalog_data, f, ensure_ascii=False, indent=2)

    print(f"Successfully generated {OUTPUT_PATH} with {len(jobs_catalog)} jobs.")

if __name__ == "__main__":
    build_catalog()
