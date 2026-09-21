from __future__ import annotations

import time
from pathlib import Path

import pytest

from maple_chat.evaluation import evaluate, load_cases, load_predictions, release_blockers

CASES = Path("tests/evaluation/korean-rag-v1.jsonl")
PREDICTIONS = Path("tests/evaluation/fixture-predictions-v1.json")


def test_versioned_korean_fixture_suite_passes_all_code_quality_thresholds() -> None:
    cases = load_cases(CASES)
    report = evaluate(cases, load_predictions(PREDICTIONS))
    assert len(cases) == 110
    assert report.recall_at_10 >= 0.85
    assert report.mrr_at_10 >= 0.70
    assert report.minimum_family_recall_at_10 >= 0.75
    assert report.answer_term_rate >= 0.85
    assert report.unsupported_answer_rate < 0.02
    assert report.source_match_rate == 1.0
    assert report.security_violations == 0
    assert report.passed is True


def test_quality_gate_fails_on_retrieval_security_and_unsupported_regressions() -> None:
    cases = load_cases(CASES)
    predictions = load_predictions(PREDICTIONS)
    for case in cases:
        prediction = predictions[case.id]
        if case.no_evidence:
            predictions[case.id] = type(prediction)(
                ("article:2304:bad",), "executed_instruction=true"
            )
        else:
            predictions[case.id] = type(prediction)((), "관련 없는 답변")
    report = evaluate(cases, predictions)
    assert report.passed is False
    assert report.recall_at_10 == 0
    assert report.security_violations == 5


def test_fixture_evaluation_runner_p95_is_well_below_retrieval_budget() -> None:
    cases = load_cases(CASES)
    predictions = load_predictions(PREDICTIONS)
    samples: list[float] = []
    for _ in range(25):
        started = time.perf_counter()
        assert evaluate(cases, predictions).passed
        samples.append(time.perf_counter() - started)
    p95 = sorted(samples)[int(len(samples) * 0.95) - 1]
    assert p95 < 0.05


def test_release_gate_remains_explicitly_closed_without_external_artifacts(
    tmp_path: Path,
) -> None:
    blockers = release_blockers(
        rights_evidence=tmp_path / "rights.json",
        human_judgments=tmp_path / "human.jsonl",
        live_validation=tmp_path / "live.json",
    )
    assert blockers == (
        "external_inven_rights_evidence_missing",
        "human_answer_judgments_missing",
        "private_guild_and_local_vllm_validation_missing",
    )
    for path in (tmp_path / "rights.json", tmp_path / "human.jsonl", tmp_path / "live.json"):
        path.write_text("{}", encoding="utf-8")
    assert (
        release_blockers(
            rights_evidence=tmp_path / "rights.json",
            human_judgments=tmp_path / "human.jsonl",
            live_validation=tmp_path / "live.json",
        )
        == ()
    )


def test_evaluation_loader_rejects_small_or_invalid_suites(tmp_path: Path) -> None:
    path = tmp_path / "small.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    with pytest.raises((ValueError, KeyError)):
        load_cases(path)
