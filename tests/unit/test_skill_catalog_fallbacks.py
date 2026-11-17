"""Exercise existing catalog fallback contracts with explicitly synthetic skills."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from maple_chat.knowledge import skill_catalog as skills


@pytest.fixture(autouse=True)
def isolated_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "fixture-skills.json"
    monkeypatch.setattr(skills, "CATALOG_PATH", path)
    monkeypatch.setattr(skills, "_CATALOG_CACHE", None)
    return path


def test_missing_file_cache_is_only_replaced_on_explicit_reload(isolated_catalog: Path) -> None:
    empty = {"jobs": {}, "common_skills": [], "total_jobs": 0}
    assert skills.load_skill_catalog() == empty
    updated = {"jobs": {"fixture-job": {"official_skills": ["fixture-own"]}}}
    isolated_catalog.write_text(json.dumps(updated), encoding="utf-8")
    assert skills.load_skill_catalog() == empty
    assert skills.load_skill_catalog(force_reload=True) == updated
    assert skills.get_job_skill_whitelist("fixture-job") == ["fixture-own"]


def test_malformed_json_falls_back_and_reports_unverified(isolated_catalog: Path) -> None:
    isolated_catalog.write_text("{", encoding="utf-8")
    assert skills.load_skill_catalog() == {"jobs": {}, "common_skills": [], "total_jobs": 0}
    assert skills.get_job_skill_details("fixture-absent") == {}
    assert skills.get_grounded_v_matrix("fixture-absent") is None
    result = skills.validate_guide_skills({}, "fixture-absent")
    assert result["unverified"] is True
    assert result["matched_skills"] == []


@pytest.mark.parametrize("document", [{}, {"fixture": 1}, {"jobs": {}}])
def test_unregistered_job_has_no_details(document: dict[str, Any], isolated_catalog: Path) -> None:
    isolated_catalog.write_text(json.dumps(document), encoding="utf-8")
    assert skills.get_job_skill_details("fixture-absent") == {}
    assert skills.get_job_taxonomy("fixture-absent") == {
        stage: [] for stage in ("1차", "2차", "3차", "4차", "하이퍼", "5차", "6차")
    }


@pytest.mark.parametrize("nested", [True, False])
def test_taxonomy_accepts_existing_nested_and_flat_formats(
    nested: bool, isolated_catalog: Path
) -> None:
    taxonomy = {"1차": ["fixture-first"], "6차": ["fixture-sixth"]}
    details = {"taxonomy": taxonomy} if nested else taxonomy
    isolated_catalog.write_text(json.dumps({"jobs": {"fixture-job": details}}), encoding="utf-8")
    assert skills.get_job_taxonomy("fixture-job") == taxonomy


@pytest.mark.parametrize("common_key", ["common_skills", "universal_common_skills"])
@pytest.mark.parametrize(
    ("job", "family"),
    [
        ("섀도어", "도적"),
        ("비숍", "마법사"),
        ("히어로", "전사"),
        ("신궁", "궁수"),
        ("캡틴", "해적"),
    ],
)
def test_legacy_whitelist_keeps_family_skills_isolated(
    common_key: str, job: str, family: str, isolated_catalog: Path
) -> None:
    families = {
        name: [f"fixture-family-{name}"] for name in ("도적", "마법사", "전사", "궁수", "해적")
    }
    document = {
        common_key: ["fixture-common"],
        "family_common_skills": families,
        "jobs": {
            job: {
                "1차": ["fixture-direct"],
                "taxonomy": {"4차": ["fixture-nested"], "ignored": None},
                "ignored": 0,
            }
        },
    }
    isolated_catalog.write_text(json.dumps(document), encoding="utf-8")
    assert skills.get_job_skill_whitelist(job) == sorted(
        ["fixture-common", "fixture-direct", "fixture-nested", f"fixture-family-{family}"]
    )
    assert skills.get_job_skill_whitelist("fixture-absent") == ["fixture-common"]


def test_explicit_whitelist_is_deduplicated_without_cross_job_expansion(
    isolated_catalog: Path,
) -> None:
    document = {
        "common_skills": ["fixture-other"],
        "jobs": {"fixture-job": {"official_skills": ["fixture-b", "fixture-a", "fixture-b"]}},
    }
    isolated_catalog.write_text(json.dumps(document), encoding="utf-8")
    assert skills.get_job_skill_whitelist("fixture-job") == ["fixture-a", "fixture-b"]


def test_existing_validator_collects_nested_buffs_cycles_and_core_names(
    isolated_catalog: Path,
) -> None:
    isolated_catalog.write_text(
        json.dumps({"jobs": {"fixture-job": {"official_skills": ["fixture-a", "fixture-b"]}}}),
        encoding="utf-8",
    )
    fixture = {
        "burst_rotation": {"pre_buff": ["fixture-a"], "steps": ["use fixture-b", "no match"]},
        "buff_sequence": {"before": ["fixture-b", "fixture-unknown"], "ignored": None},
        "deal_cycle": {"cycle": ["use fixture-a", "no match"], "ignored": None},
        "core_v_matrix": {
            "cores": ["스킬 코어: fixture-a", "오리진: fixture-unregistered"],
            "ignored": None,
        },
    }
    result = skills.validate_guide_skills(fixture, "fixture-job")
    assert result["valid"] is False
    assert result["matched_skills"] == ["fixture-a", "fixture-b"]
    assert result["unknown_skills"] == ["fixture-unknown", "fixture-unregistered"]
    assert result["match_ratio"] == round(2 / 4, 2)


@pytest.mark.parametrize("rotation", [["use fixture-a", "no match"], None])
def test_existing_rotation_shapes_only_extract_known_matches(
    rotation: list[str] | None, isolated_catalog: Path
) -> None:
    isolated_catalog.write_text(
        json.dumps({"jobs": {"fixture-job": {"official_skills": ["fixture-a"]}}}), encoding="utf-8"
    )
    fixture = {
        "burst_rotation": rotation,
        "buff_sequence": None,
        "deal_cycle": None,
        "core_v_matrix": None,
    }
    result = skills.validate_guide_skills(fixture, "fixture-job")
    assert result["matched_skills"] == (["fixture-a"] if rotation is not None else [])
    assert result["unknown_skills"] == []
