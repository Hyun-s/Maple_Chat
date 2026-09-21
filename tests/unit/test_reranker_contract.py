from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from maple_chat import runtime
from maple_chat.config import Settings
from maple_chat.retrieval.hybrid import RemoteReranker, RerankerSpec


def _spec() -> RerankerSpec:
    return RerankerSpec("BAAI/bge-reranker-v2-m3", "commit-1")


@pytest.mark.asyncio
async def test_remote_reranker_preserves_revision_order_tei_shape() -> None:
    requests: list[dict[str, object]] = []
    request_targets: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        request_targets.append((request.method, str(request.url)))
        if request.url.path == "/info":
            return httpx.Response(
                200,
                json={"model_id": "BAAI/bge-reranker-v2-m3", "model_sha": "commit-1"},
            )
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=[
                {"index": 1, "score": 0.9},
                {"index": 0, "score": 0.1},
            ],
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:8082/v1/",
    ) as client:
        reranker = RemoteReranker(
            _spec(),
            base_url="http://127.0.0.1:8082/v1",
            served_model="bge-reranker-v2-m3",
            timeout_seconds=10,
            client=client,
        )
        assert await reranker.score("질의", ["첫째", "둘째"]) == [0.1, 0.9]
        await reranker.aclose()

    assert requests == [{"model": "bge-reranker-v2-m3", "query": "질의", "texts": ["첫째", "둘째"]}]
    assert request_targets == [
        ("GET", "http://127.0.0.1:8082/info"),
        ("POST", "http://127.0.0.1:8082/v1/rerank"),
    ]


@pytest.mark.asyncio
async def test_remote_reranker_accepts_vllm_results_shape() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(
                200,
                json={"model_id": "BAAI/bge-reranker-v2-m3", "model_sha": "commit-1"},
            )
        return httpx.Response(
            200,
            json={
                "model": "bge-reranker-v2-m3",
                "results": [
                    {"index": 0, "relevance_score": 0.75},
                    {"index": 1, "relevance_score": 0.25},
                ],
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://dcm-reranker:80/v1/",
    ) as client:
        reranker = RemoteReranker(
            _spec(),
            base_url="http://dcm-reranker:80/v1",
            served_model="bge-reranker-v2-m3",
            timeout_seconds=10,
            client=client,
        )
        assert await reranker.score("q", ["a", "b"]) == [0.75, 0.25]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("info_payload", "error"),
    [
        ({"model_id": "other", "model_sha": "commit-1"}, "model_id"),
        ({"model_id": "BAAI/bge-reranker-v2-m3", "model_sha": "other"}, "model_sha"),
        ({"model_sha": "commit-1"}, "model_id"),
        ({"model_id": "BAAI/bge-reranker-v2-m3"}, "model_sha"),
    ],
)
async def test_remote_reranker_rejects_unattested_service(
    info_payload: dict[str, object],
    error: str,
) -> None:
    requests: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        return httpx.Response(200, json=info_payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:8082/v1/",
    ) as client:
        reranker = RemoteReranker(
            _spec(),
            base_url="http://127.0.0.1:8082/v1",
            served_model="bge-reranker-v2-m3",
            timeout_seconds=10,
            client=client,
        )
        with pytest.raises(RuntimeError, match=error):
            await reranker.score("q", ["a"])

    assert requests == [("GET", "/info")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ([{"index": 0, "score": 0.5}], "wrong score count"),
        ([{"index": 0, "score": 0.5}, {"index": 0, "score": 0.4}], "invalid score ordering"),
        ([{"index": 0, "score": 0.5}, {"index": 1, "score": "x"}], "non-finite scores"),
        (
            b'[{"index": 0, "score": 0.5}, {"index": 1, "score": 1e999}]',
            "non-finite scores",
        ),
        ([{"score": 0.5}, {"score": 0.4}], "invalid score ordering"),
    ],
)
async def test_remote_reranker_rejects_malformed_scores(
    payload: object,
    error: str,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(
                200,
                json={"model_id": "BAAI/bge-reranker-v2-m3", "model_sha": "commit-1"},
            )
        if isinstance(payload, bytes):
            return httpx.Response(
                200,
                content=payload,
                headers={"content-type": "application/json"},
            )
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:8082/v1/",
    ) as client:
        reranker = RemoteReranker(
            _spec(),
            base_url="http://127.0.0.1:8082/v1",
            served_model="bge-reranker-v2-m3",
            timeout_seconds=10,
            client=client,
        )
        with pytest.raises(RuntimeError, match=error):
            await reranker.score("q", ["a", "b"])


@pytest.mark.asyncio
async def test_remote_reranker_empty_documents_short_circuits() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:8082/v1/",
    ) as client:
        reranker = RemoteReranker(
            _spec(),
            base_url="http://127.0.0.1:8082/v1",
            served_model="bge-reranker-v2-m3",
            timeout_seconds=10,
            client=client,
        )
        assert await reranker.score("q", []) == []
    assert calls == []


def test_remote_reranker_rejects_non_boundary_base_urls() -> None:
    for base_url in (
        "https://cloud.example.com/v1",
        "http://127.0.0.1:8082/v1?token=x",
        "http://user@127.0.0.1:8082/v1",
        "ftp://127.0.0.1:8082/v1",
    ):
        with pytest.raises(ValueError, match="approved local model boundary"):
            RemoteReranker(
                _spec(),
                base_url=base_url,
                served_model="bge-reranker-v2-m3",
                timeout_seconds=10,
            )


def test_remote_reranker_requires_pinned_spec_and_served_model() -> None:
    with pytest.raises(ValueError, match="immutable"):
        RemoteReranker(
            RerankerSpec("BAAI/bge-reranker-v2-m3", ""),
            base_url="http://127.0.0.1:8082/v1",
            served_model="bge-reranker-v2-m3",
            timeout_seconds=10,
        )
    with pytest.raises(ValueError, match="served model"):
        RemoteReranker(
            _spec(),
            base_url="http://127.0.0.1:8082/v1",
            served_model="  ",
            timeout_seconds=10,
        )


def test_build_reranker_selects_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def build_local(spec: RerankerSpec) -> SimpleNamespace:
        calls.append("local")
        return SimpleNamespace(spec=spec)

    def build_remote(
        spec: RerankerSpec,
        *,
        base_url: str,
        served_model: str,
        timeout_seconds: float,
    ) -> SimpleNamespace:
        calls.append("remote")
        return SimpleNamespace(
            spec=spec,
            base_url=base_url,
            served_model=served_model,
            timeout_seconds=timeout_seconds,
        )

    monkeypatch.setattr(runtime, "SentenceTransformerReranker", build_local)
    monkeypatch.setattr(runtime, "RemoteReranker", build_remote)
    settings = SimpleNamespace(
        reranker_provider="remote",
        reranker_model="BAAI/bge-reranker-v2-m3",
        reranker_model_commit="commit-1",
        reranker_base_url="http://dcm-reranker:80/v1",
        reranker_remote_model="bge-reranker-v2-m3",
        reranker_timeout_seconds=15.0,
    )
    reranker = runtime.build_reranker(settings)  # type: ignore[arg-type]
    assert calls == ["remote"]
    assert reranker.base_url == "http://dcm-reranker:80/v1"  # type: ignore[attr-defined]
    assert reranker.served_model == "bge-reranker-v2-m3"  # type: ignore[attr-defined]
    assert reranker.timeout_seconds == 15.0  # type: ignore[attr-defined]

    settings2 = SimpleNamespace(
        reranker_provider="local",
        reranker_model="BAAI/bge-reranker-v2-m3",
        reranker_model_commit="commit-1",
    )
    runtime.build_reranker(settings2)  # type: ignore[arg-type]
    assert calls == ["remote", "local"]


def test_settings_validate_remote_reranker_boundary() -> None:
    base = {
        "DISCORD_TOKEN": "t",
        "DISCORD_GUILD_ID": "1",
        "DISCORD_OWNER_ID": "2",
        "DATABASE_URL": "postgresql+asyncpg://db/app",
        "PII_HASH_SALT": "salt",
        "RERANKER_PROVIDER": "remote",
        "RERANKER_BASE_URL": "http://dcm-reranker:80/v1",
        "RERANKER_REMOTE_MODEL": "bge-reranker-v2-m3",
        "RERANKER_TIMEOUT_SECONDS": "30",
    }
    with patch.dict(os.environ, base, clear=True):
        settings = Settings()
    assert settings.reranker_provider == "remote"
    assert settings.reranker_base_url == "http://dcm-reranker:80/v1"
    assert settings.safe_summary()["reranker_provider"] == "remote"

    cloud = base | {"RERANKER_BASE_URL": "https://api.cohere.ai/v1"}
    with patch.dict(os.environ, cloud, clear=True), pytest.raises(Exception, match="RERANKER"):
        Settings()
