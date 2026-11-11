"""Official job skill catalog loader and guide skill validator."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

CATALOG_PATH = Path(__file__).resolve().parent / "data" / "job-skill-catalog-v1.json"

_CATALOG_CACHE: dict[str, Any] | None = None

def load_skill_catalog(force_reload: bool = False) -> dict[str, Any]:
    global _CATALOG_CACHE
    if _CATALOG_CACHE is None or force_reload:
        if not CATALOG_PATH.exists():
            return {"jobs": {}, "common_skills": []}
        with open(CATALOG_PATH, "r", encoding="utf-8") as f:
            _CATALOG_CACHE = json.load(f)
    return _CATALOG_CACHE

def get_job_skill_whitelist(job_name: str) -> list[str]:
    catalog = load_skill_catalog()
    jobs = catalog.get("jobs", {})
    job_info = jobs.get(job_name)
    common = catalog.get("common_skills", [])
    if not job_info:
        return common
    return sorted(list(set(job_info.get("official_skills", []) + common)))

def get_job_skill_details(job_name: str) -> dict[str, Any] | None:
    catalog = load_skill_catalog()
    return catalog.get("jobs", {}).get(job_name)

def get_grounded_v_matrix(job_name: str) -> dict[str, Any] | None:
    details = get_job_skill_details(job_name)
    if not details:
        return None
    return details.get("v_matrix")

def validate_guide_skills(guide_json: dict[str, Any], job_name: str) -> dict[str, Any]:
    whitelist = set(get_job_skill_whitelist(job_name))
    if not whitelist:
        return {"valid": True, "unknown_skills": [], "matched_skills": [], "ratio": 1.0}

    found_skills: set[str] = set()
    # Extract skills from burst_rotation
    burst = guide_json.get("burst_rotation", {})
    for pb in burst.get("pre_buff", []):
        for part in re.split(r"[+,/]|그리고|및|의 축복", pb):
            cleaned = re.sub(r"^[0-9]+\.\s*|시퀀스 버프:|버프:|\([^)]*\)", "", part).strip()
            if len(cleaned) >= 2:
                found_skills.add(cleaned)

    # Extract skills from core_v_matrix
    vm = guide_json.get("core_v_matrix", {})
    for sc in vm.get("스킬_코어", []):
        c = re.sub(r"\([^)]*\)", "", sc).strip()
        if len(c) >= 2:
            found_skills.add(c)
    for mc in vm.get("6차_코어", []):
        c = re.sub(r"^(오리진|마스터리 코어)[\s:]*|\([^)]*\)", "", mc).strip()
        if len(c) >= 2:
            found_skills.add(c)

    matched: list[str] = []
    unknown: list[str] = []

    for skill in found_skills:
        # Exact match or substring containment
        is_matched = False
        for valid in whitelist:
            if skill == valid or valid in skill or skill in valid:
                is_matched = True
                matched.append(skill)
                break
        if not is_matched:
            unknown.append(skill)

    total = len(found_skills)
    ratio = len(matched) / total if total > 0 else 1.0
    return {
        "valid": len(unknown) == 0,
        "matched_skills": sorted(matched),
        "unknown_skills": sorted(unknown),
        "matched_count": len(matched),
        "unknown_count": len(unknown),
        "match_ratio": round(ratio, 2),
    }
