from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from maple_chat.post_qa_evaluation import (
    evaluate,
    load_predictions,
    load_protocol,
    normalized_text,
)

PROTOCOL = Path("tests/evaluation/backfilled-post-zero-v1.json")
ORACLE = Path("tests/evaluation/backfilled-post-zero-oracle-v1.json")


def test_backfilled_post_oracle_passes_every_protocol_dimension() -> None:
    protocol = load_protocol(PROTOCOL)
    report = evaluate(protocol, load_predictions(ORACLE))

    assert protocol.protocol_id == "zero-community-qa-2026-09-03"
    assert len(protocol.cases) == 10
    assert report.cases == 10
    assert report.answerable_cases == 8
    assert report.answer_source_recall_at_k == 1
    assert report.mrr_at_k == 1
    assert report.source_containment_rate == 1
    assert report.claim_recall == 1
    assert report.complete_answer_rate == 1
    assert report.attribution_rate == 1
    assert report.abstention_accuracy == 1
    assert report.passed is True


def test_protocol_detects_retrieval_claim_attribution_and_abstention_regressions() -> None:
    protocol = load_protocol(PROTOCOL)
    predictions = load_predictions(ORACLE)
    answerable = next(case for case in protocol.cases if case.answerable)
    negative = next(case for case in protocol.cases if not case.answerable)
    predictions[answerable.id] = replace(
        predictions[answerable.id],
        sources=("article:2295:999999",),
        answer="아마 강화하면 됩니다.",
    )
    predictions[negative.id] = replace(
        predictions[negative.id],
        sources=("comment:article:2294:452426:1736688",),
        answer="25성 비용은 확실히 1메소입니다.",
    )

    report = evaluate(protocol, predictions)

    assert report.answer_source_recall_at_k < 1
    assert report.source_containment_rate < 1
    assert report.claim_recall < 1
    assert report.complete_answer_rate < 1
    assert report.attribution_rate < 1
    assert report.abstention_accuracy < 1
    assert report.passed is False


def test_protocol_requires_exact_prediction_coverage() -> None:
    protocol = load_protocol(PROTOCOL)
    predictions = load_predictions(ORACLE)
    predictions.pop(next(iter(predictions)))

    with pytest.raises(ValueError, match="exactly once"):
        evaluate(protocol, predictions)


def test_protocol_rejects_gold_sources_outside_the_target_post(tmp_path: Path) -> None:
    raw = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    raw["cases"][0]["expected_answer_sources"] = ["article:2295:999999"]
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="target post"):
        load_protocol(invalid)


def test_claim_normalization_handles_nested_entities_spacing_and_markup() -> None:
    value = "<b>17&amp;nbsp;~&amp;nbsp;18성</b>"
    assert normalized_text(value) == "1718성"
