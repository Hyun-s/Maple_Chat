"""Ground truth skill catalog loader, per-job taxonomy, and guide validator."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

CATALOG_PATH = Path(__file__).resolve().parent / "data" / "job-skill-catalog-v1.json"

_CATALOG_CACHE: dict[str, Any] | None = None


def load_skill_catalog(force_reload: bool = False) -> dict[str, Any]:
    """Load the ground truth skill catalog from JSON file."""
    global _CATALOG_CACHE
    if _CATALOG_CACHE is not None and not force_reload:
        return _CATALOG_CACHE

    if not CATALOG_PATH.exists():
        _CATALOG_CACHE = {"jobs": {}, "common_skills": [], "total_jobs": 0}
        return _CATALOG_CACHE

    try:
        with open(CATALOG_PATH, "r", encoding="utf-8") as f:
            _CATALOG_CACHE = json.load(f)
    except Exception:
        _CATALOG_CACHE = {"jobs": {}, "common_skills": [], "total_jobs": 0}

    return _CATALOG_CACHE


def get_job_skill_details(job_name: str) -> dict[str, Any]:
    cat = load_skill_catalog()
    if not cat or "jobs" not in cat or job_name not in cat["jobs"]:
        return {}
    return cat["jobs"][job_name]


def get_job_taxonomy(job_name: str) -> dict[str, Any]:
    """Get verified skill taxonomy for a job."""
    details = get_job_skill_details(job_name)
    if "taxonomy" in details:
        return details["taxonomy"]
    if details and any(k in details for k in ["1차", "2차", "3차", "4차", "5차", "6차", "하이퍼"]):
        return details
    return {
        "1차": [],
        "2차": [],
        "3차": [],
        "4차": [],
        "하이퍼": [],
        "5차": [],
        "6차": []
    }


def get_grounded_v_matrix(job_name: str) -> dict[str, list[str]] | None:
    details = get_job_skill_details(job_name)
    return details.get("v_matrix")


def get_job_skill_whitelist(job_name: str) -> list[str]:
    """Get complete isolated skill whitelist for a job."""
    cat = load_skill_catalog()
    details = get_job_skill_details(job_name)
    if details and "official_skills" in details:
        return sorted(list(set(details["official_skills"])))

    # Fallback to computing whitelist
    commons = list(cat.get("universal_common_skills", []))
    if not commons:
        commons = list(cat.get("common_skills", []))

    family_map = cat.get("family_common_skills", {})
    whitelist = set(commons)

    if details:
        for sec, slist in details.items():
            if isinstance(slist, list):
                whitelist.update(slist)
            elif isinstance(slist, dict):
                for sub_list in slist.values():
                    if isinstance(sub_list, list):
                        whitelist.update(sub_list)

    THIEF_JOBS = ["나이트로드", "섀도어", "듀얼블레이더", "팬텀", "카데나", "칼리", "호영", "제논"]
    MAGE_JOBS = ["아크메이지(불,독)", "아크메이지(썬,콜)", "비숍", "플레임위자드", "배틀메이지", "에반", "루미너스", "일리움", "라라", "키네시스"]
    WARRIOR_JOBS = ["히어로", "팔라딘", "다크나이트", "소울마스터", "미하일", "블래스터", "데몬슬레이어", "데몬어벤져", "아란", "카이저", "아델", "제로", "렌"]
    ARCHER_JOBS = ["보우마스터", "신궁", "패스파인더", "윈드브레이커", "와일드헌터", "메르세데스", "카인"]
    PIRATE_JOBS = ["바이퍼", "캡틴", "캐논슈터", "스트라이커", "메카닉", "은월", "엔젤릭버스터", "아크", "제논"]

    if job_name in THIEF_JOBS:
        whitelist.update(family_map.get("도적", []))
    elif job_name in MAGE_JOBS:
        whitelist.update(family_map.get("마법사", []))
    elif job_name in WARRIOR_JOBS:
        whitelist.update(family_map.get("전사", []))
    elif job_name in ARCHER_JOBS:
        whitelist.update(family_map.get("궁수", []))
    elif job_name in PIRATE_JOBS:
        whitelist.update(family_map.get("해적", []))

    return sorted(list(whitelist))


def validate_guide_skills(guide_dict: dict[str, Any], job_name: str) -> dict[str, Any]:
    whitelist = set(get_job_skill_whitelist(job_name))
    if not whitelist:
        return {"valid": True, "unverified": True, "match_ratio": 1.0, "matched_skills": [], "unknown_skills": []}

    guide_skills = set()
    br = guide_dict.get("burst_rotation", {})
    if isinstance(br, dict):
        for pb in br.get("pre_buff", []):
            guide_skills.add(pb)
        for step in br.get("steps", []):
            for s in whitelist:
                if s in step:
                    guide_skills.add(s)
    elif isinstance(br, list):
        for step in br:
            for s in whitelist:
                if s in step:
                    guide_skills.add(s)

    buff_seq = guide_dict.get("buff_sequence", {})
    if isinstance(buff_seq, dict):
        for buff_list in buff_seq.values():
            if isinstance(buff_list, list):
                for b in buff_list:
                    guide_skills.add(b)

    dc = guide_dict.get("deal_cycle", {})
    if isinstance(dc, dict):
        for cycle_steps in dc.values():
            if isinstance(cycle_steps, list):
                for step in cycle_steps:
                    for s in whitelist:
                        if s in step:
                            guide_skills.add(s)

    cv = guide_dict.get("core_v_matrix", {})
    if isinstance(cv, dict):
        for core_list in cv.values():
            if isinstance(core_list, list):
                for core_str in core_list:
                    cleaned = re.sub(r'^(오리진|마스터리 코어|스킬 코어|강화 코어)[\s:]*', '', core_str).strip()
                    matched_core = False
                    for s in whitelist:
                        if s in cleaned or cleaned in s:
                            guide_skills.add(s)
                            matched_core = True
                    if not matched_core and cleaned:
                        guide_skills.add(cleaned)

    matched = guide_skills & whitelist
    unknown = guide_skills - whitelist
    match_ratio = len(matched) / max(1, len(guide_skills))
    return {
        "valid": len(unknown) == 0,
        "match_ratio": round(match_ratio, 2),
        "matched_skills": sorted(list(matched)),
        "unknown_skills": sorted(list(unknown)),
    }

