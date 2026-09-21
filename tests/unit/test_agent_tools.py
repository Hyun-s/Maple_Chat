from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from maple_chat.agent.tools import (
    ToolConfirmationRequired,
    ToolRegistry,
    ToolRisk,
    ToolSpec,
    build_nexon_tool_registry,
)
from maple_chat.nexon.client import NexonOpenAPIError


class _FakeNexonClient:
    async def resolve_character(self, character_name: str) -> dict[str, Any]:
        return {"ocid": f"ocid:{character_name}"}

    async def get_character_basic(self, ocid: str, *, at: date | None = None) -> dict[str, Any]:
        return {
            "date": "2026-08-30T00:00+09:00",
            "character_name": "테스트캐릭터",
            "character_level": 290,
            "character_image": "https://ignored.example/image",
        }

    async def get_character_stat(self, ocid: str, *, at: date | None = None) -> dict[str, Any]:
        return {"final_stat": [{"stat_name": "보스 몬스터 데미지", "stat_value": "400"}]}

    async def get_character_equipment(self, ocid: str, *, at: date | None = None) -> dict[str, Any]:
        return {
            "item_equipment": [
                {
                    "item_equipment_part": "무기",
                    "item_name": "테스트 무기",
                    "starforce": "22",
                    "item_icon": "https://ignored.example/item",
                }
            ]
        }

    async def get_union(self, ocid: str, *, at: date | None = None) -> dict[str, Any]:
        return {"union_level": 9000, "union_grade": "그랜드 마스터"}


class _EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


@pytest.mark.asyncio
async def test_registry_validates_inputs_and_returns_bounded_summaries() -> None:
    registry = build_nexon_tool_registry(_FakeNexonClient())  # type: ignore[arg-type]

    resolved = await registry.execute("resolve_character", {"character_name": "테스트캐릭터"})
    basic = await registry.execute("get_character_basic", {"ocid": resolved["ocid"]})
    equipment = await registry.execute("get_character_equipment", {"ocid": resolved["ocid"]})
    stats = await registry.execute("get_character_stat", {"ocid": resolved["ocid"]})
    union = await registry.execute("get_union", {"ocid": resolved["ocid"], "date": "2026-08-30"})

    assert resolved["ocid"] == "ocid:테스트캐릭터"
    assert basic["character_level"] == 290
    assert "character_image" not in basic
    assert equipment["item_equipment"][0]["item_name"] == "테스트 무기"
    assert "item_icon" not in equipment["item_equipment"][0]
    assert stats["final_stat"][0]["stat_value"] == "400"
    assert union["union_level"] == 9000

    with pytest.raises(ValidationError):
        await registry.execute("get_character_stat", {"ocid": "x", "unexpected": True})


@pytest.mark.asyncio
async def test_registry_rejects_unknown_tool_and_invalid_character_name() -> None:
    registry = build_nexon_tool_registry(_FakeNexonClient())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="unknown tool"):
        await registry.execute("shell", {})
    with pytest.raises(ValidationError):
        await registry.execute("resolve_character", {"character_name": "두 단어"})


@pytest.mark.asyncio
async def test_registry_fails_closed_when_identifier_is_missing() -> None:
    class MissingIdentifierClient(_FakeNexonClient):
        async def resolve_character(self, character_name: str) -> dict[str, Any]:
            return {"ocid": None}

    registry = build_nexon_tool_registry(MissingIdentifierClient())  # type: ignore[arg-type]

    with pytest.raises(NexonOpenAPIError, match="식별자"):
        await registry.execute("resolve_character", {"character_name": "테스트캐릭터"})


@pytest.mark.asyncio
async def test_registry_requires_confirmation_for_state_changing_tool() -> None:
    async def handler(_: BaseModel) -> dict[str, Any]:
        raise AssertionError("confirmation-gated tool must not execute")

    registry = ToolRegistry(
        (
            ToolSpec(
                name="mutate",
                description="상태 변경",
                input_model=_EmptyInput,
                handler=handler,
                risk=ToolRisk.CONFIRMATION_REQUIRED,
            ),
        )
    )

    with pytest.raises(ToolConfirmationRequired, match="confirmation"):
        await registry.execute("mutate", {})
