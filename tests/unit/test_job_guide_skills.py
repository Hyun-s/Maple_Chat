import pytest
from maple_chat.skills.job_guide import (
    JobGuideInput,
    build_job_guide_skills_registry,
    get_job_guide,
)

def test_get_job_guide_phantom():
    guide = get_job_guide("팬텀")
    assert guide["found"] is True
    assert guide["job_name"] == "팬텀"
    assert "burst_rotation" in guide
    assert "stolen_skills" in guide
    assert "core_v_matrix" in guide
    assert "라 모르 카르트" in guide["burst_rotation"]["critical_tip"]

def test_get_job_guide_filter_topic():
    guide = get_job_guide("팬텀", topic="딜사이클")
    assert guide["found"] is True
    assert "burst_rotation" in guide
    assert "stolen_skills" not in guide

def test_get_job_guide_stolen_skills():
    guide = get_job_guide("팬텀", topic="스틸스킬")
    assert guide["found"] is True
    assert "stolen_skills" in guide
    assert "파이널 컷" in guide["stolen_skills"]["4차"]

def test_get_job_guide_not_found():
    guide = get_job_guide("미존재직업")
    assert guide["found"] is False
    assert "팬텀" in guide["available_jobs"]

@pytest.mark.asyncio
async def test_build_job_guide_skills_registry():
    registry = build_job_guide_skills_registry()
    assert set(registry.names) == {"get_job_combat_guide"}
    result = await registry.execute("get_job_combat_guide", {"job_name": "팬텀", "topic": "딜사이클"})
    assert result["found"] is True
    assert result["job_name"] == "팬텀"
    assert "burst_rotation" in result
