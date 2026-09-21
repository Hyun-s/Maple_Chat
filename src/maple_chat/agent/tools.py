"""Schema-validated, read-only tools exposed to the Maple Chat agent."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date as Date
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from maple_chat.nexon.client import NexonOpenAPIClient, NexonOpenAPIError

ToolHandler = Callable[[BaseModel], Awaitable[dict[str, Any]]]


class ToolRisk(StrEnum):
    READ_ONLY = "read_only"
    CONFIRMATION_REQUIRED = "confirmation_required"


class ToolConfirmationRequired(RuntimeError):
    """The selected tool cannot execute without a durable user confirmation."""


class ResolveCharacterInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    character_name: str = Field(min_length=2, max_length=32)

    @field_validator("character_name")
    @classmethod
    def validate_character_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(
            character.isspace() or ord(character) < 32 for character in normalized
        ):
            raise ValueError("character_name must be one token without control characters")
        return normalized


class CharacterQueryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ocid: str = Field(min_length=1, max_length=128)
    date: Date | None = None


class ToolSpec(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    description: str
    input_model: type[BaseModel]
    handler: ToolHandler = Field(exclude=True)
    risk: ToolRisk = ToolRisk.READ_ONLY

    def schema_for_prompt(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_model.model_json_schema(),
            "risk": self.risk.value,
        }


class ToolRegistry:
    def __init__(self, specs: tuple[ToolSpec, ...]) -> None:
        self._specs = {spec.name: spec for spec in specs}
        if len(self._specs) != len(specs):
            raise ValueError("tool names must be unique")

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def prompt_catalog(self) -> tuple[dict[str, Any], ...]:
        return tuple(spec.schema_for_prompt() for spec in self._specs.values())

    def merged_with(self, other: ToolRegistry) -> ToolRegistry:
        return ToolRegistry((*self._specs.values(), *other._specs.values()))

    async def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        spec = self._specs.get(name)
        if spec is None:
            raise ValueError("unknown tool")
        if spec.risk is ToolRisk.CONFIRMATION_REQUIRED:
            raise ToolConfirmationRequired("tool requires user confirmation")
        validated = spec.input_model.model_validate(arguments)
        return await spec.handler(validated)


def build_nexon_tool_registry(client: NexonOpenAPIClient) -> ToolRegistry:
    async def resolve(value: BaseModel) -> dict[str, Any]:
        args = ResolveCharacterInput.model_validate(value)
        payload = await client.resolve_character(args.character_name)
        ocid = payload.get("ocid")
        if not isinstance(ocid, str) or not ocid:
            raise NexonOpenAPIError("캐릭터 식별자를 확인할 수 없습니다.")
        return {"character_name": args.character_name, "ocid": ocid}

    async def basic(value: BaseModel) -> dict[str, Any]:
        args = CharacterQueryInput.model_validate(value)
        payload = await client.get_character_basic(args.ocid, at=args.date)
        keys = (
            "date",
            "character_name",
            "world_name",
            "character_class",
            "character_class_level",
            "character_level",
            "character_exp_rate",
            "character_guild_name",
            "character_date_create",
            "access_flag",
            "liberation_quest_clear",
        )
        return {key: payload.get(key) for key in keys}

    async def stat(value: BaseModel) -> dict[str, Any]:
        args = CharacterQueryInput.model_validate(value)
        payload = await client.get_character_stat(args.ocid, at=args.date)
        values = payload.get("final_stat")
        return {
            "date": payload.get("date"),
            "character_class": payload.get("character_class"),
            "final_stat": values[:80] if isinstance(values, list) else [],
            "remain_ap": payload.get("remain_ap"),
        }

    async def equipment(value: BaseModel) -> dict[str, Any]:
        args = CharacterQueryInput.model_validate(value)
        payload = await client.get_character_equipment(args.ocid, at=args.date)
        items = payload.get("item_equipment")
        summarized: list[dict[str, Any]] = []
        if isinstance(items, list):
            for item in items[:40]:
                if not isinstance(item, dict):
                    continue
                summarized.append(
                    {
                        key: item.get(key)
                        for key in (
                            "item_equipment_part",
                            "item_equipment_slot",
                            "item_name",
                            "starforce",
                            "potential_option_grade",
                            "potential_option_1",
                            "potential_option_2",
                            "potential_option_3",
                            "additional_potential_option_grade",
                            "additional_potential_option_1",
                            "additional_potential_option_2",
                            "additional_potential_option_3",
                            "scroll_upgrade",
                            "scroll_upgradeable_count",
                            "soul_name",
                        )
                    }
                )
        return {
            "date": payload.get("date"),
            "character_class": payload.get("character_class"),
            "preset_no": payload.get("preset_no"),
            "item_equipment": summarized,
        }

    async def union(value: BaseModel) -> dict[str, Any]:
        args = CharacterQueryInput.model_validate(value)
        payload = await client.get_union(args.ocid, at=args.date)
        return {
            key: payload.get(key)
            for key in (
                "date",
                "union_level",
                "union_grade",
                "union_artifact_level",
                "union_artifact_exp",
                "union_artifact_point",
            )
        }

    return ToolRegistry(
        (
            ToolSpec(
                name="resolve_character",
                description="캐릭터명으로 최신 캐릭터 식별자(ocid)를 조회한다.",
                input_model=ResolveCharacterInput,
                handler=resolve,
            ),
            ToolSpec(
                name="get_character_basic",
                description="ocid로 월드, 직업, 레벨, 길드 등 기본 정보를 조회한다.",
                input_model=CharacterQueryInput,
                handler=basic,
            ),
            ToolSpec(
                name="get_character_stat",
                description="ocid로 현재 종합 능력치를 조회한다.",
                input_model=CharacterQueryInput,
                handler=stat,
            ),
            ToolSpec(
                name="get_character_equipment",
                description="ocid로 현재 장착 중인 비캐시 장비와 잠재능력을 조회한다.",
                input_model=CharacterQueryInput,
                handler=equipment,
            ),
            ToolSpec(
                name="get_union",
                description="ocid가 속한 계정의 유니온 레벨과 등급을 조회한다.",
                input_model=CharacterQueryInput,
                handler=union,
            ),
        )
    )
