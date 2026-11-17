"""Reject malformed catalog fixtures without touching packaged knowledge data."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

from maple_chat.knowledge import catalog


@pytest.fixture
def payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source": {
            "source_id": "fixture:source",
            "title": "Fixture catalog",
            "url": "https://example.invalid/catalog",
            "publisher": "Fixture publisher",
            "version": "fixture-v1",
            "checked_at": "2026-01-01T00:00:00+00:00",
        },
        "entities": [
            {"entity_id": "fixture:a", "entity_type": "fixture", "canonical_name": "Alpha"},
            {"entity_id": "fixture:b", "entity_type": "fixture", "canonical_name": "Beta"},
        ],
        "relations": [
            {
                "subject_entity_id": "fixture:a",
                "predicate": "fixture_link",
                "object_entity_id": "fixture:b",
            }
        ],
    }


@pytest.fixture
def resource_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(catalog, "files", lambda _package: tmp_path)
    return tmp_path


@pytest.mark.parametrize(
    ("location", "value", "message"),
    [
        ((), [], "root must be an object"),
        (("schema_version",), 2, "unsupported knowledge catalog schema_version"),
        (("source",), [], "source must be an object"),
        (("source", "title"), " ", "must be a non-empty string"),
        (("source", "publisher"), None, "must be a non-empty string"),
        (("source", "checked_at"), "2026-01-01", "must include a timezone"),
        (("allowed_ambiguous_aliases",), {}, "allowed_ambiguous_aliases"),
        (("allowed_ambiguous_aliases",), [""], "allowed_ambiguous_aliases"),
        (("external_entity_ids",), {}, "external_entity_ids"),
        (("external_entity_ids",), [None], "external_entity_ids"),
        (("entities",), [], "entities must be a non-empty list"),
        (("entities",), None, "entities must be a non-empty list"),
        (("entities", 0), "invalid", "each knowledge entity must be an object"),
        (("entities", 1, "entity_id"), "fixture:a", "duplicate knowledge entity_id"),
        (("entities", 0, "aliases"), {}, "must be non-empty strings"),
        (("entities", 0, "aliases"), [" "], "must be non-empty strings"),
        (
            ("entities", 1, "aliases"),
            [" \uff21\uff2c\uff30\uff28\uff21 "],
            "ambiguous normalized alias",
        ),
        (("entities", 0, "description"), 1, "must be a string or null"),
        (("relations",), [], "relations must be a non-empty list"),
        (("relations",), None, "relations must be a non-empty list"),
        (("relations", 0), "invalid", "each knowledge relation must be an object"),
        (("relations", 0, "subject_entity_id"), "absent", "unknown relation subject"),
        (("relations", 0, "object_entity_id"), "absent", "unknown relation object"),
    ],
)
def test_invalid_catalog_is_rejected(
    payload: dict[str, Any],
    resource_root: Path,
    location: tuple[str | int, ...],
    value: Any,
    message: str,
) -> None:
    document: Any = payload
    if location:
        parent = document
        for key in location[:-1]:
            parent = parent[key]
        parent[location[-1]] = value
    else:
        document = value
    (resource_root / "fixture.json").write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match=re.escape(message)):
        catalog._load_catalog("fixture.json")


def test_duplicate_relation_is_rejected(payload: dict[str, Any], resource_root: Path) -> None:
    payload["relations"].append(dict(payload["relations"][0]))
    (resource_root / "fixture.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate knowledge relation"):
        catalog._load_catalog("fixture.json")


def test_declared_ambiguity_and_external_reference_preserve_provenance(
    payload: dict[str, Any], resource_root: Path
) -> None:
    payload["allowed_ambiguous_aliases"] = [" alpha "]
    payload["external_entity_ids"] = [" fixture:external ", "fixture:external"]
    payload["entities"][1]["aliases"] = [" \uff21\uff2c\uff30\uff28\uff21 "]
    payload["source"]["title"] = "  Fixture catalog  "
    payload["relations"][0]["object_entity_id"] = "fixture:external"
    raw = json.dumps(payload, ensure_ascii=False).encode()
    (resource_root / "fixture.json").write_bytes(raw)
    loaded = catalog._load_catalog("fixture.json")
    assert loaded.source.title == "Fixture catalog"
    assert loaded.source.checked_at.utcoffset() is not None
    assert loaded.external_entity_ids == ("fixture:external",)
    assert loaded.entities[1].aliases == ("\uff21\uff2c\uff30\uff28\uff21",)
    assert loaded.relations[0].object_entity_id == "fixture:external"
    assert loaded.content_hash == hashlib.sha256(raw).hexdigest()
