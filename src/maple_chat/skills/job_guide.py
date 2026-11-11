"""Job combat guides and skill rotation skill modules for Maple Chat."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from maple_chat.agent.tools import ToolRegistry, ToolSpec

_JOB_GUIDES_CACHE: dict[str, Any] | None = None
_JOB_GUIDES_MTIME: float = 0.0


def _get_guides_mtime() -> float:
    base_dir = Path(__file__).parent
    guides_dir = base_dir / "guides"
    if not guides_dir.exists():
        return 0.0
    mtimes = [p.stat().st_mtime for p in guides_dir.glob("*.json")]
    return max(mtimes) if mtimes else 0.0


def _load_job_guides(force_reload: bool = False) -> dict[str, Any]:
    global _JOB_GUIDES_CACHE, _JOB_GUIDES_MTIME
    latest_mtime = _get_guides_mtime()
    if _JOB_GUIDES_CACHE is None or force_reload or latest_mtime > _JOB_GUIDES_MTIME:
        guides: dict[str, Any] = {}
        base_dir = Path(__file__).parent

        # 1. Load legacy/bundle file if present
        bundle_path = base_dir / "job_guides.json"
        if bundle_path.exists():
            try:
                with open(bundle_path, "r", encoding="utf-8") as f:
                    guides.update(json.load(f))
            except Exception:
                pass

        # 2. Load individual modular guide files from guides/ dir
        guides_dir = base_dir / "guides"
        if guides_dir.exists():
            for json_path in sorted(guides_dir.glob("*.json")):
                try:
                    with open(json_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        job_name = data.get("job_name")
                        if job_name:
                            guides[job_name] = data
                except Exception:
                    continue

        _JOB_GUIDES_CACHE = guides
        _JOB_GUIDES_MTIME = latest_mtime
    return _JOB_GUIDES_CACHE


def list_available_guides() -> list[str]:
    return sorted(list(_load_job_guides().keys()))


class JobGuideInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_name: str = Field(description="조회할 메이플스토리 직업 이름 (예: 팬텀, 나이트워커, 히어로)")
    topic: str | None = Field(
        default=None,
        description="조회할 공략 주제 ('딜사이클', '스킬', '코강', '스틸스킬' 등). 미입력 시 전체 공략 조회",
    )


def get_job_guide(job_name: str, topic: str | None = None) -> dict[str, Any]:
    guides = _load_job_guides()
    norm_name = job_name.strip()
    target_guide: dict[str, Any] | None = None

    # Exact match first
    if norm_name in guides:
        target_guide = guides[norm_name]
    else:
        for key, val in guides.items():
            if norm_name in key or key in norm_name:
                target_guide = val
                break

    if target_guide is None:
        return {
            "found": False,
            "job_name": norm_name,
            "message": f"'{norm_name}' 직업의 정제된 실전 공략 가이드 데이터가 아직 등록되어 있지 않습니다.",
            "available_jobs": list_available_guides(),
        }

    result: dict[str, Any] = {
        "found": True,
        "job_name": target_guide["job_name"],
        "job_family": target_guide.get("job_family"),
        "summary": target_guide.get("summary"),
        "data_source": target_guide.get("data_source"),
        "metadata": target_guide.get("metadata", {}),
    }

    topic_lower = (topic or "").strip().lower()
    if not topic_lower or "전체" in topic_lower:
        result["buff_sequence"] = target_guide.get("buff_sequence")
        result["deal_cycle"] = target_guide.get("deal_cycle")
        result["burst_rotation"] = target_guide.get("burst_rotation")
        result["sub_burst_and_normal"] = target_guide.get("sub_burst_and_normal")
        result["stolen_skills"] = target_guide.get("stolen_skills")
        result["core_v_matrix"] = target_guide.get("core_v_matrix")
    elif any(k in topic_lower for k in ["딜", "사이클", "극딜", "준극", "평딜", "오리진", "순서"]):
        result["buff_sequence"] = target_guide.get("buff_sequence")
        result["deal_cycle"] = target_guide.get("deal_cycle")
        result["burst_rotation"] = target_guide.get("burst_rotation")
        result["sub_burst_and_normal"] = target_guide.get("sub_burst_and_normal")
    elif "버프" in topic_lower or "시퀀스" in topic_lower:
        result["buff_sequence"] = target_guide.get("buff_sequence")
    elif "스틸" in topic_lower or "훔치" in topic_lower:
        result["stolen_skills"] = target_guide.get("stolen_skills")
    elif "코강" in topic_lower or "코어" in topic_lower or "v매트릭스" in topic_lower:
        result["core_v_matrix"] = target_guide.get("core_v_matrix")
    else:
        result["buff_sequence"] = target_guide.get("buff_sequence")
        result["deal_cycle"] = target_guide.get("deal_cycle")
        result["burst_rotation"] = target_guide.get("burst_rotation")
        result["sub_burst_and_normal"] = target_guide.get("sub_burst_and_normal")
        result["stolen_skills"] = target_guide.get("stolen_skills")
        result["core_v_matrix"] = target_guide.get("core_v_matrix")

    return result


async def _handle_get_job_combat_guide(value: BaseModel) -> dict[str, Any]:
    args = JobGuideInput.model_validate(value)
    return get_job_guide(args.job_name, args.topic)


def build_job_guide_skills_registry() -> ToolRegistry:
    return ToolRegistry(
        (
            ToolSpec(
                name="get_job_combat_guide",
                description=(
                    "메이플스토리 직업(예: 팬텀 등)의 검증된 실전 딜사이클(극딜/준극딜 순서), "
                    "추천 스틸 스킬, 코어 강화(코강) 우선순위 및 스킬 운용 팁 공략을 조회한다."
                ),
                input_model=JobGuideInput,
                handler=_handle_get_job_combat_guide,
            ),
        )
    )
