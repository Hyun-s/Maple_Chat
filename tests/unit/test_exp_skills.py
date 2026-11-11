from __future__ import annotations

import pytest
from maple_chat.skills.exp import (
    HuntingCalculationInput,
    PotionCalculationInput,
    calculate_hunting_efficiency,
    calculate_potion_exp,
    get_level_penalty_rate,
    resolve_potion_rule,
)


def test_resolve_potion_aliases() -> None:
    rule1 = resolve_potion_rule("익성비")
    assert rule1.name == "익스트림 성장의 비약"
    assert rule1.cap_level == 199

    rule2 = resolve_potion_rule("극성비")
    assert rule2.name == "극한 성장의 비약"
    assert rule2.max_guaranteed_level == 249

    rule3 = resolve_potion_rule("태성비")
    assert rule3.name == "태풍 성장의 비약"

    rule4 = resolve_potion_rule("초월비")
    assert rule4.name == "초월 성장의 비약"

    with pytest.raises(ValueError, match="알 수 없는 비약"):
        resolve_potion_rule("존재하지않는비약")


def test_guaranteed_level_up_potions() -> None:
    # Lv 248 30%에서 극성비 1개 사용 시 -> Lv 249 30% 확정 1업
    res = calculate_potion_exp(
        PotionCalculationInput(
            level=248,
            current_exp_percent=30.0,
            potion_name="극성비",
            count=1,
        )
    )
    assert res["start_level"] == 248
    assert res["final_level"] == 249
    assert res["final_percent"] == 30.0
    assert res["level_ups"] == 1


def test_over_cap_potion_gives_fixed_exp() -> None:
    # Lv 260 0%에서 극성비 1개 사용 시 (극성비 cap_level=249 필요경험치: 155,483,188,174)
    # 260 필요경험치는 1,731,919,984,062 -> 정확히 8.9775% 상승
    res = calculate_potion_exp(
        PotionCalculationInput(
            level=260,
            current_exp_percent=0.0,
            potion_name="극성비",
            count=1,
        )
    )
    assert res["start_level"] == 260
    assert res["final_level"] == 260
    assert 8.97 < res["final_percent"] < 8.98
    assert res["level_ups"] == 0
    assert res["total_exp_gained"] == 155_483_188_174


def test_multiple_potions_carry_over_level_up() -> None:
    # Lv 260 95%에서 극성비 1개 사용 -> 10.67% 경험치가 들어가므로 105.67% -> Lv 261 레벨업 및 잔여 % 이월
    res = calculate_potion_exp(
        PotionCalculationInput(
            level=260,
            current_exp_percent=95.0,
            potion_name="극성비",
            count=1,
        )
    )
    assert res["start_level"] == 260
    assert res["final_level"] == 261
    assert res["level_ups"] == 1
    assert res["final_percent"] > 0.0


def test_level_penalty_rate_table() -> None:
    # 동렙: 110%
    assert get_level_penalty_rate(260, 260) == 1.10
    assert get_level_penalty_rate(260, 261) == 1.10
    assert get_level_penalty_rate(260, 259) == 1.10
    # ±2렙: 105%
    assert get_level_penalty_rate(260, 262) == 1.05
    # ±10렙: 90%
    assert get_level_penalty_rate(260, 270) == 0.90
    assert get_level_penalty_rate(260, 250) == 0.90


def test_hunting_calculation() -> None:
    # Lv 260, 몹 260레벨, 기본경험치 1,000,000, 시간당 15,000마리, 도핑 합 300% (3배)
    # 마리당 경험치 = 1,000,000 * 1.1 (동렙 보너스) * 3.0 = 3,300,000
    # 1시간 경험치 = 3,300,000 * 15,000 = 49,500,000,000 (495억)
    # 260렙 필요 경험치: 1,731,919,984,062
    # 1시간 % = 49,500,000,000 / 1,731,919,984,062 * 100 ≈ 2.86%
    res = calculate_hunting_efficiency(
        HuntingCalculationInput(
            level=260,
            current_exp_percent=0.0,
            mob_level=260,
            mob_base_exp=1_000_000,
            kill_rate_per_hour=15000,
            total_exp_rate_percent=300.0,
        )
    )
    assert res["exp_per_kill"] == 3_300_000.0
    assert res["exp_per_1hour"] == 49_500_000_000
    assert 2.85 <= res["pct_per_1hour"] <= 2.87
    assert res["pct_per_2hour_jaehoek"] == round(res["pct_per_1hour"] * 2, 2)
    assert res["hours_to_next_level"] > 30.0


@pytest.mark.asyncio
async def test_exp_skills_tool_registry() -> None:
    from maple_chat.skills.exp import build_exp_skills_registry

    registry = build_exp_skills_registry()
    assert set(registry.names) == {
        "calculate_potion_exp",
        "calculate_hunting_efficiency",
        "get_monster_base_exp",
    }

    # Potion execution via registry
    potion_res = await registry.execute(
        "calculate_potion_exp",
        {"level": 260, "current_exp_percent": 0.0, "potion_name": "극성비", "count": 1},
    )
    assert potion_res["final_level"] == 260
    assert 8.97 < potion_res["final_percent"] < 8.98

    # Hunting execution via registry with explicit mob_base_exp
    hunt_res = await registry.execute(
        "calculate_hunting_efficiency",
        {
            "level": 260,
            "current_exp_percent": 0.0,
            "mob_level": 260,
            "mob_base_exp": 1_000_000,
            "kill_rate_per_hour": 15000,
            "total_exp_rate_percent": 300.0,
        },
    )
    assert hunt_res["exp_per_kill"] == 3_300_000.0
    assert hunt_res["auto_base_exp_applied"] is False

    # Hunting execution via registry with omitted mob_base_exp (auto lookup by mob_level)
    hunt_auto_res = await registry.execute(
        "calculate_hunting_efficiency",
        {
            "level": 260,
            "current_exp_percent": 0.0,
            "mob_level": 260,
            "kill_rate_per_hour": 15000,
            "total_exp_rate_percent": 300.0,
        },
    )
    assert hunt_auto_res["auto_base_exp_applied"] is True
    assert hunt_auto_res["mob_base_exp"] > 1_000_000
    assert hunt_auto_res["exp_per_1hour"] > 0

    # Monster base exp lookup via registry
    lookup_res = await registry.execute(
        "get_monster_base_exp",
        {"mob_level": 260},
    )
    assert lookup_res["mob_level"] == 260
    assert lookup_res["base_exp"] == hunt_auto_res["mob_base_exp"]

