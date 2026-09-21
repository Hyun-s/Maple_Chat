"""Evaluation contracts for question-answer evidence from one backfilled post."""

from __future__ import annotations

import argparse
import html
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class PostQACase:
    id: str
    query: str
    mutation: str
    expected_answer_sources: tuple[str, ...]
    required_claims: tuple[tuple[str, ...], ...]
    answerable: bool


@dataclass(frozen=True, slots=True)
class PostQAProtocol:
    protocol_id: str
    target_post_source: str
    top_k: int
    attribution_terms: tuple[str, ...]
    abstention_terms: tuple[str, ...]
    thresholds: Mapping[str, float]
    cases: tuple[PostQACase, ...]


@dataclass(frozen=True, slots=True)
class PostQAPrediction:
    sources: tuple[str, ...]
    answer: str


@dataclass(frozen=True, slots=True)
class PostQAReport:
    cases: int
    answerable_cases: int
    answer_source_recall_at_k: float
    mrr_at_k: float
    source_containment_rate: float
    claim_recall: float
    complete_answer_rate: float
    attribution_rate: float
    abstention_accuracy: float
    passed: bool

    def as_dict(self) -> dict[str, int | float | bool]:
        return {
            "cases": self.cases,
            "answerable_cases": self.answerable_cases,
            "answer_source_recall_at_k": self.answer_source_recall_at_k,
            "mrr_at_k": self.mrr_at_k,
            "source_containment_rate": self.source_containment_rate,
            "claim_recall": self.claim_recall,
            "complete_answer_rate": self.complete_answer_rate,
            "attribution_rate": self.attribution_rate,
            "abstention_accuracy": self.abstention_accuracy,
            "passed": self.passed,
        }


def _tuple_of_strings(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a list of strings")
    return tuple(value)


def _required_claims(value: object) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, list):
        raise ValueError("required_claims must be a list")
    claims = tuple(_tuple_of_strings(group, field="required_claims item") for group in value)
    if any(not group for group in claims):
        raise ValueError("required_claims items cannot be empty")
    return claims


def load_protocol(path: Path) -> PostQAProtocol:
    try:
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        raw_cases: list[dict[str, Any]] = raw["cases"]
        cases = tuple(
            PostQACase(
                id=str(case["id"]),
                query=str(case["query"]),
                mutation=str(case["mutation"]),
                expected_answer_sources=_tuple_of_strings(
                    case["expected_answer_sources"], field="expected_answer_sources"
                ),
                required_claims=_required_claims(case["required_claims"]),
                answerable=bool(case["answerable"]),
            )
            for case in raw_cases
        )
        protocol = PostQAProtocol(
            protocol_id=str(raw["protocol_id"]),
            target_post_source=str(raw["target_post_source"]),
            top_k=int(raw["top_k"]),
            attribution_terms=_tuple_of_strings(
                raw["attribution_terms"], field="attribution_terms"
            ),
            abstention_terms=_tuple_of_strings(raw["abstention_terms"], field="abstention_terms"),
            thresholds={str(key): float(value) for key, value in raw["thresholds"].items()},
            cases=cases,
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid single-post QA evaluation protocol") from exc
    _validate_protocol(protocol)
    return protocol


def _validate_protocol(protocol: PostQAProtocol) -> None:
    required_thresholds = {
        "answer_source_recall_at_k",
        "mrr_at_k",
        "source_containment_rate",
        "claim_recall",
        "complete_answer_rate",
        "attribution_rate",
        "abstention_accuracy",
    }
    if not protocol.protocol_id or not protocol.target_post_source:
        raise ValueError("protocol identifiers cannot be empty")
    if not 1 <= protocol.top_k <= 100:
        raise ValueError("top_k must be between 1 and 100")
    if set(protocol.thresholds) != required_thresholds or any(
        not 0 <= value <= 1 for value in protocol.thresholds.values()
    ):
        raise ValueError("protocol thresholds are incomplete or outside [0, 1]")
    if not protocol.attribution_terms or not protocol.abstention_terms:
        raise ValueError("attribution and abstention terms cannot be empty")
    if len({case.id for case in protocol.cases}) != len(protocol.cases):
        raise ValueError("single-post QA case ids must be unique")
    if not any(case.answerable for case in protocol.cases) or not any(
        not case.answerable for case in protocol.cases
    ):
        raise ValueError("protocol requires answerable and unanswerable controls")
    for case in protocol.cases:
        if not case.id or not case.query.strip() or not case.mutation:
            raise ValueError("case identity, query, and mutation cannot be empty")
        if case.answerable:
            if not case.expected_answer_sources or not case.required_claims:
                raise ValueError("answerable cases require answer sources and atomic claims")
            if any(
                protocol.target_post_source not in source for source in case.expected_answer_sources
            ):
                raise ValueError("answer sources must belong to the target post")
        elif case.expected_answer_sources or case.required_claims:
            raise ValueError("unanswerable controls cannot carry gold answers")


def load_predictions(path: Path) -> dict[str, PostQAPrediction]:
    try:
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return {
            str(case_id): PostQAPrediction(
                sources=_tuple_of_strings(value["sources"], field="prediction sources"),
                answer=str(value["answer"]),
            )
            for case_id, value in raw.items()
        }
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid single-post QA predictions") from exc


def normalized_text(value: str) -> str:
    """Normalize nested HTML entities and spacing for deterministic claim matching."""

    for _ in range(3):
        decoded = html.unescape(value)
        if decoded == value:
            break
        value = decoded
    without_markup = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"[\W_]+", "", without_markup.casefold(), flags=re.UNICODE)


def _contains_any(answer: str, alternatives: tuple[str, ...]) -> bool:
    normalized_answer = normalized_text(answer)
    return any(normalized_text(term) in normalized_answer for term in alternatives)


def evaluate(
    protocol: PostQAProtocol,
    predictions: Mapping[str, PostQAPrediction],
) -> PostQAReport:
    if set(predictions) != {case.id for case in protocol.cases}:
        raise ValueError("predictions must cover each single-post QA case exactly once")

    answerable = tuple(case for case in protocol.cases if case.answerable)
    unanswerable = tuple(case for case in protocol.cases if not case.answerable)
    retrieval_hits = 0
    reciprocal_ranks = 0.0
    contained = 0
    claim_hits = 0
    claim_count = 0
    complete_answers = 0
    attributed = 0

    for case in answerable:
        prediction = predictions[case.id]
        top_sources = prediction.sources[: protocol.top_k]
        ranks = [
            rank
            for rank, source in enumerate(top_sources, start=1)
            if source in case.expected_answer_sources
        ]
        if ranks:
            retrieval_hits += 1
            reciprocal_ranks += 1 / min(ranks)
        if top_sources and all(protocol.target_post_source in source for source in top_sources):
            contained += 1
        current_hits = sum(
            _contains_any(prediction.answer, alternatives) for alternatives in case.required_claims
        )
        claim_hits += current_hits
        claim_count += len(case.required_claims)
        complete_answers += int(current_hits == len(case.required_claims))
        attributed += int(_contains_any(prediction.answer, protocol.attribution_terms))

    abstained = sum(
        not predictions[case.id].sources
        and _contains_any(predictions[case.id].answer, protocol.abstention_terms)
        for case in unanswerable
    )
    answerable_count = len(answerable)
    metrics = {
        "answer_source_recall_at_k": retrieval_hits / answerable_count,
        "mrr_at_k": reciprocal_ranks / answerable_count,
        "source_containment_rate": contained / answerable_count,
        "claim_recall": claim_hits / claim_count,
        "complete_answer_rate": complete_answers / answerable_count,
        "attribution_rate": attributed / answerable_count,
        "abstention_accuracy": abstained / len(unanswerable),
    }
    return PostQAReport(
        cases=len(protocol.cases),
        answerable_cases=answerable_count,
        **metrics,
        passed=all(metrics[name] >= threshold for name, threshold in protocol.thresholds.items()),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="maple-post-evaluate")
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = evaluate(load_protocol(args.protocol), load_predictions(args.predictions))
    print(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))
    return 0 if report.passed else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
