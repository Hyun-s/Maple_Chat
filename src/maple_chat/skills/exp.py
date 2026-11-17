"""Deterministic MapleStory experience, potion, and hunting calculation engine."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from maple_chat.agent.tools import ToolRegistry, ToolSpec

_TABLE_PATH = Path(__file__).parent / "exp_table.json"
_MOB_TABLE_PATH = Path(__file__).parent / "mob_exp_table.json"


def _load_exp_table() -> dict[int, int]:
    if not _TABLE_PATH.is_file():
        raise FileNotFoundError(f"EXP table not found at {_TABLE_PATH}")
    with _TABLE_PATH.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    return {int(k): int(v) for k, v in raw.items()}


def _load_mob_exp_table() -> dict[int, int]:
    if not _MOB_TABLE_PATH.is_file():
        raise FileNotFoundError(f"Monster base EXP table not found at {_MOB_TABLE_PATH}")
    with _MOB_TABLE_PATH.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    return {int(k): int(v) for k, v in raw.items()}


LEVEL_EXP_TABLE: dict[int, int] = _load_exp_table()
MONSTER_BASE_EXP_TABLE: dict[int, int] = _load_mob_exp_table()


def get_monster_base_exp(mob_level: int) -> int:
    """메이플스토리 일반 필드 몬스터의 레벨별 기준 순경험치 반환 (200~300)"""
    if mob_level in MONSTER_BASE_EXP_TABLE:
        return MONSTER_BASE_EXP_TABLE[mob_level]
    if mob_level < 200:
        return 100000
    if mob_level > 300:
        return MONSTER_BASE_EXP_TABLE[300]
    return MONSTER_BASE_EXP_TABLE.get(mob_level, 100000)


@dataclass(frozen=True, slots=True)
class PotionRule:
    name: str
    aliases: tuple[str, ...]
    max_guaranteed_level: int  # 이 레벨 이하에서는 무조건 1업 (또는 100% 경험치)
    cap_level: int  # 초과 레벨 사용 시 기준이 되는 레벨 (해당 레벨의 100% 필요 경험치를 지급)


_POTION_RULES: tuple[PotionRule, ...] = (
    PotionRule(
        name="성장의 비약 (200~209)",
        aliases=("성비1", "성장의비약1", "성비 1", "성장의 비약 1"),
        max_guaranteed_level=209,
        cap_level=209,
    ),
    PotionRule(
        name="성장의 비약 (200~219)",
        aliases=("성비2", "성장의비약2", "성비 2", "성장의 비약 2"),
        max_guaranteed_level=219,
        cap_level=219,
    ),
    PotionRule(
        name="성장의 비약 (200~229)",
        aliases=("성비3", "성장의비약3", "성비 3", "성장의 비약 3"),
        max_guaranteed_level=229,
        cap_level=229,
    ),
    PotionRule(
        name="태풍 성장의 비약",
        aliases=("태성비", "태풍성장의비약", "태풍 성비"),
        max_guaranteed_level=239,
        cap_level=239,
    ),
    PotionRule(
        name="극한 성장의 비약",
        aliases=("극성비", "극한성장의비약", "극한 성비"),
        max_guaranteed_level=249,
        cap_level=249,
    ),
    PotionRule(
        name="초월 성장의 비약",
        aliases=("초월비", "초월성장의비약", "초월 성비"),
        max_guaranteed_level=269,
        cap_level=269,
    ),
    PotionRule(
        name="익스트림 성장의 비약",
        aliases=("익성비", "익스트림성장의비약", "익스성비"),
        max_guaranteed_level=199,  # 200 이상에선 199렙 필요 경험치(고정 571,115,568) 지급
        cap_level=199,
    ),
)

_LV199_EXP = 571_115_568


def resolve_potion_rule(name: str) -> PotionRule:
    normalized = name.strip().lower().replace(" ", "")
    for rule in _POTION_RULES:
        if normalized == rule.name.lower().replace(" ", ""):
            return rule
        if any(normalized == alias.lower().replace(" ", "") for alias in rule.aliases):
            return rule
    valid = [r.name for r in _POTION_RULES]
    raise ValueError(f"알 수 없는 비약 종류: '{name}'. 지원 목록: {valid}")


class PotionCalculationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: int = Field(ge=200, le=299, description="캐릭터 현재 레벨 (200~299)")
    current_exp_percent: float = Field(
        default=0.0, ge=0.0, lt=100.0, description="현재 경험치 퍼센트 (0.0 ~ 99.99)"
    )
    potion_name: str = Field(description="비약 명칭 (예: 익성비, 극성비, 태성비, 성비1, 초월비 등)")
    count: int = Field(default=1, ge=1, le=100, description="사용할 비약 개수 (기본 1개)")


class HuntingCalculationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: int = Field(ge=200, le=299, description="캐릭터 현재 레벨 (200~299)")
    current_exp_percent: float = Field(
        default=0.0, ge=0.0, lt=100.0, description="현재 경험치 퍼센트"
    )
    mob_level: int = Field(ge=190, le=310, description="사냥터 몬스터 레벨")
    mob_base_exp: int | None = Field(
        default=None,
        ge=1,
        description=(
            "몬스터 1마리당 기본 경험치 (생략 시 mob_level 기준 메이플 "
            "일반 몬스터 기준 순경험치 자동 적용)"
        ),
    )
    kill_rate_per_hour: int = Field(
        default=15000, ge=100, le=50000, description="시간당 처치 마릿수 (기본 15,000)"
    )
    total_exp_rate_percent: float = Field(
        default=250.0,
        ge=100.0,
        le=1500.0,
        description="총 적용 도핑 배율 합 (예: 1배=100, 2배쿠폰+룬+MVP 등 합산이 350%면 350.0)",
    )


def get_level_penalty_rate(character_level: int, mob_level: int) -> float:
    """메이플스토리 공식 레벨 격차 경험치 증감율 반환"""
    diff = mob_level - character_level
    if diff in (-1, 0, 1):
        return 1.10
    if diff in (-2, 2):
        return 1.05
    if diff in (-4, -3, 3, 4):
        return 1.00
    if diff in (-9, -8, -7, -6, -5, 5, 6, 7, 8, 9):
        return 0.95
    if diff in (10, -10):
        return 0.90
    if diff in (11, -11):
        return 0.85
    if diff in (12, -12):
        return 0.80
    if diff in (13, -13):
        return 0.75
    if diff in (14, -14):
        return 0.70
    if diff in (15, -15):
        return 0.65
    if diff in (16, -16):
        return 0.60
    if diff in (17, -17):
        return 0.55
    if diff in (18, -18):
        return 0.50
    if diff in (19, -19):
        return 0.45
    if diff in (20, -20):
        return 0.40
    if diff > 20 or diff < -20:
        return max(0.10, 0.40 - (abs(diff) - 20) * 0.05)
    return 1.00


def calculate_potion_exp(input_data: PotionCalculationInput) -> dict[str, Any]:
    rule = resolve_potion_rule(input_data.potion_name)
    cur_lvl = input_data.level
    cur_pct = input_data.current_exp_percent
    count = input_data.count

    initial_req = LEVEL_EXP_TABLE[cur_lvl]
    cur_exp = int(initial_req * (cur_pct / 100.0))
    total_gained_exp = 0
    step_log: list[str] = []

    for i in range(1, count + 1):
        if cur_lvl >= 300:
            step_log.append(f"#{i}: 300레벨 도달")
            break
        req_for_cur = LEVEL_EXP_TABLE[cur_lvl]

        if cur_lvl <= rule.max_guaranteed_level:
            # 100% 1업 보장 구간: 현재 레벨의 100% 필요 경험치를 그대로 부여
            gained = req_for_cur
            total_gained_exp += gained
            old_lvl = cur_lvl
            cur_lvl += 1
            # 경험치 % 비율은 보존됨
            cur_exp = int(LEVEL_EXP_TABLE.get(cur_lvl, req_for_cur) * (cur_pct / 100.0))
            step_log.append(
                f"#{i} ({rule.name}): 1레벨 확정 상승 (Lv.{old_lvl} -> "
                f"Lv.{cur_lvl}, 획득 {gained:,})"
            )
        else:
            # 초과 구간: 상한 레벨의 100% 경험치 고정 지급
            gained = _LV199_EXP if rule.cap_level == 199 else LEVEL_EXP_TABLE[rule.cap_level]
            total_gained_exp += gained
            cur_exp += gained
            req = LEVEL_EXP_TABLE[cur_lvl]
            gain_pct = (gained / req) * 100.0

            leveled_up = False
            while cur_lvl < 300 and cur_exp >= LEVEL_EXP_TABLE[cur_lvl]:
                cur_exp -= LEVEL_EXP_TABLE[cur_lvl]
                cur_lvl += 1
                leveled_up = True

            new_pct = (cur_exp / LEVEL_EXP_TABLE[cur_lvl]) * 100.0 if cur_lvl < 300 else 100.0
            if leveled_up:
                step_log.append(
                    f"#{i} ({rule.name}): 고정 경험치 {gained:,} "
                    f"(+{gain_pct:.2f}%) 획득 -> 레벨업 (Lv.{cur_lvl} "
                    f"{new_pct:.2f}%)"
                )
            else:
                step_log.append(
                    f"#{i} ({rule.name}): 고정 경험치 {gained:,} "
                    f"(+{gain_pct:.2f}%) 획득 -> Lv.{cur_lvl} {new_pct:.2f}%"
                )
            cur_pct = new_pct

    final_req = LEVEL_EXP_TABLE.get(cur_lvl, 1)
    final_pct = (cur_exp / final_req) * 100.0 if cur_lvl < 300 else 100.0
    total_gain_pct_relative = (total_gained_exp / initial_req) * 100.0

    return {
        "potion_name": rule.name,
        "count": count,
        "start_level": input_data.level,
        "start_percent": input_data.current_exp_percent,
        "final_level": cur_lvl,
        "final_percent": round(final_pct, 4),
        "level_ups": cur_lvl - input_data.level,
        "total_exp_gained": total_gained_exp,
        "relative_gain_percent_of_start_level": round(total_gain_pct_relative, 2),
        "step_log": step_log,
    }


def calculate_hunting_efficiency(input_data: HuntingCalculationInput) -> dict[str, Any]:
    penalty_rate = get_level_penalty_rate(input_data.level, input_data.mob_level)
    base_exp = (
        input_data.mob_base_exp
        if input_data.mob_base_exp is not None
        else get_monster_base_exp(input_data.mob_level)
    )
    effective_base_exp = base_exp * penalty_rate
    multiplier = input_data.total_exp_rate_percent / 100.0
    exp_per_kill = effective_base_exp * multiplier

    exp_1hour = int(exp_per_kill * input_data.kill_rate_per_hour)
    exp_2hour_jaehoek = exp_1hour * 2

    req_exp = LEVEL_EXP_TABLE[input_data.level]
    pct_1hour = (exp_1hour / req_exp) * 100.0
    pct_2hour = (exp_2hour_jaehoek / req_exp) * 100.0

    remaining_pct = max(0.0, 100.0 - input_data.current_exp_percent)
    remaining_exp = int(req_exp * (remaining_pct / 100.0))
    hours_to_level_up = remaining_exp / exp_1hour if exp_1hour > 0 else 0.0

    return {
        "character_level": input_data.level,
        "mob_level": input_data.mob_level,
        "mob_base_exp": base_exp,
        "auto_base_exp_applied": input_data.mob_base_exp is None,
        "level_penalty_bonus_rate": round(penalty_rate, 2),
        "exp_per_kill": round(exp_per_kill, 2),
        "kill_rate_per_hour": input_data.kill_rate_per_hour,
        "total_exp_rate_percent": input_data.total_exp_rate_percent,
        "exp_per_1hour": exp_1hour,
        "pct_per_1hour": round(pct_1hour, 2),
        "exp_per_2hour_jaehoek": exp_2hour_jaehoek,
        "pct_per_2hour_jaehoek": round(pct_2hour, 2),
        "hours_to_next_level": round(hours_to_level_up, 2),
    }


class MonsterExpLookupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mob_level: int = Field(ge=200, le=300, description="조회할 몬스터 레벨 (200~300)")


async def _handle_calculate_potion_exp(value: BaseModel) -> dict[str, Any]:
    args = PotionCalculationInput.model_validate(value)
    return calculate_potion_exp(args)


async def _handle_calculate_hunting_efficiency(value: BaseModel) -> dict[str, Any]:
    args = HuntingCalculationInput.model_validate(value)
    return calculate_hunting_efficiency(args)


async def _handle_get_monster_base_exp(value: BaseModel) -> dict[str, Any]:
    args = MonsterExpLookupInput.model_validate(value)
    base_exp = get_monster_base_exp(args.mob_level)
    return {
        "mob_level": args.mob_level,
        "base_exp": base_exp,
        "description": (
            f"Lv.{args.mob_level} 메이플 일반 필드 몬스터 마리당 기준 순경험치: {base_exp:,}"
        ),
    }


def build_exp_skills_registry() -> ToolRegistry:
    return ToolRegistry(
        (
            ToolSpec(
                name="calculate_potion_exp",
                description=(
                    "캐릭터 레벨, 현재 경험치(%), 비약 "
                    "종류(익성비/태성비/극성비/성비1~3/초월비), 개수로 정확한 "
                    "획득 경험치, 최종 레벨 및 % 상승량을 계산한다."
                ),
                input_model=PotionCalculationInput,
                handler=_handle_calculate_potion_exp,
            ),
            ToolSpec(
                name="calculate_hunting_efficiency",
                description=(
                    "캐릭터 레벨, 몬스터 레벨(기본 경험치 미입력 시 레벨 기준 "
                    "자동 계산), 도핑 배율, 마릿수로 1시간/재획당 경험치(%), "
                    "레벨업 소요 시간을 계산한다."
                ),
                input_model=HuntingCalculationInput,
                handler=_handle_calculate_hunting_efficiency,
            ),
            ToolSpec(
                name="get_monster_base_exp",
                description=(
                    "몬스터 레벨(200~300)에 해당하는 메이플 일반 필드 몬스터의 "
                    "기준 기본 순경험치를 조회한다."
                ),
                input_model=MonsterExpLookupInput,
                handler=_handle_get_monster_base_exp,
            ),
        )
    )
