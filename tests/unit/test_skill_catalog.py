"""Unit tests for job skill catalog, grounding, and guide skill validation."""
from __future__ import annotations

from maple_chat.knowledge.skill_catalog import (
    get_grounded_v_matrix,
    get_job_skill_details,
    get_job_skill_whitelist,
    load_skill_catalog,
    validate_guide_skills,
)

def test_load_skill_catalog():
    cat = load_skill_catalog(force_reload=True)
    assert "jobs" in cat
    assert "common_skills" in cat
    assert cat["total_jobs"] >= 48
    assert "섀도어" in cat["jobs"]

def test_get_job_skill_whitelist_shadower():
    wl = get_job_skill_whitelist("섀도어")
    assert len(wl) > 10
    assert "암살" in wl
    assert "소닉 블로우" in wl
    assert "레디 투 다이" in wl

def test_get_grounded_v_matrix():
    vm = get_grounded_v_matrix("섀도어")
    assert vm is not None
    assert "스킬_코어" in vm
    assert "강화_코어" in vm
    assert "6차_코어" in vm
    assert any("암살" in c for c in vm["강화_코어"])
    assert any("멸귀강참" in c or "일격필살" in c for c in vm["6차_코어"])

def test_validate_guide_skills_valid():
    sample_guide = {
        "job_name": "섀도어",
        "burst_rotation": {
            "pre_buff": ["레디 투 다이", "얼티밋 다크사이드", "섀도어 인스팅트"],
            "steps": ["1. 소닉 블로우 시전", "2. 절개 및 암살 연계"],
        },
        "core_v_matrix": {
            "스킬_코어": ["소닉 블로우", "절개"],
            "6차_코어": ["마스터리 코어: 암살 VI", "오리진: 일격필살"]
        }
    }
    res = validate_guide_skills(sample_guide, "섀도어")
    assert res["match_ratio"] >= 0.8
    assert "소닉 블로우" in res["matched_skills"]

def test_validate_guide_skills_catches_hallucination():
    sample_guide = {
        "job_name": "섀도어",
        "burst_rotation": {
            "pre_buff": ["킬링 커트", "어드레날린 러쉬"],
            "steps": ["1. 가상스킬폭격"],
        },
        "core_v_matrix": {
            "스킬_코어": ["인텐시브 인스팅트"],
            "6차_코어": ["마스터리 코어: 가상강화"]
        }
    }
    res = validate_guide_skills(sample_guide, "섀도어")
    assert res["valid"] is False
    assert "킬링 커트" in res["unknown_skills"] or "어드레날린 러쉬" in res["unknown_skills"]
