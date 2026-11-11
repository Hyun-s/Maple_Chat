"""Tests for job guide catalog schema and loader integrity."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from maple_chat.skills.job_guide import _load_job_guides, get_job_guide, list_available_guides


def test_guides_directory_loading():
    guides = _load_job_guides(force_reload=True)
    assert isinstance(guides, dict)
    assert "팬텀" in guides
    assert "팬텀" in list_available_guides()


def test_all_guide_files_conform_to_schema():
    guides_dir = Path(__file__).parents[2] / "src" / "maple_chat" / "skills" / "guides"
    if not guides_dir.exists():
        pytest.skip("guides directory does not exist yet")

    required_keys = {"job_name", "job_family", "burst_rotation", "core_v_matrix"}
    json_files = list(guides_dir.glob("*.json"))
    assert len(json_files) > 0, "At least one guide JSON file should exist"

    for json_file in json_files:
        with open(json_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        missing = required_keys - set(data.keys())
        assert not missing, f"{json_file.name} missing required keys: {missing}"
        assert data["job_name"].strip(), f"{json_file.name} has empty job_name"
        assert isinstance(data["burst_rotation"], dict), f"{json_file.name} burst_rotation must be a dict"
        assert ("steps" in data["burst_rotation"] or "sequence" in data["burst_rotation"]), f"{json_file.name} burst_rotation must have steps or sequence"


def test_get_job_guide_returns_valid_structure_for_existing_jobs():
    guides = _load_job_guides()
    for job_name in list(guides.keys())[:5]:
        res = get_job_guide(job_name)
        assert res["found"] is True
        assert res["job_name"] == job_name
        assert "burst_rotation" in res
        assert "core_v_matrix" in res
