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


def _no_tokenizer() -> None:
    """Default for existing tests: tokenizer unavailable must keep bodies verbatim."""

    raise RuntimeError("tokenizer disabled in this test")


class _PseudoTokenizer:
    """Deterministic stand-in treating one character as one pseudo-token."""

    def __init__(self) -> None:
        self.encode_calls = 0

    def __call__(
        self,
        text: str,
        *,
        truncation: bool = False,
        max_length: int | None = None,
        add_special_tokens: bool = True,
    ) -> dict[str, list[int]]:
        self.encode_calls += 1
        ids = list(range(len(text)))
        if truncation and max_length is not None:
            ids = ids[:max_length]
        return {"input_ids": ids}

    def decode(self, ids: list[int], **kwargs: object) -> str:
        return "T" + str(len(ids))


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
            tokenizer_loader=_no_tokenizer,
        )
        assert await reranker.score("질의", ["첫째", "둘째"]) == [0.1, 0.9]
        await reranker.aclose()

    assert requests == [{"model": "bge-reranker-v2-m3", "query": "질의", "texts": ["첫째", "둘째"]}]
    assert request_targets == [
        ("GET", "http://127.0.0.1:8082/info"),
        # TEI 404s /v1/rerank; the client must pin the endpoint to the origin root
        # even when base_url carries the /v1 service path.
        ("POST", "http://127.0.0.1:8082/rerank"),
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
            tokenizer_loader=_no_tokenizer,
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
            tokenizer_loader=_no_tokenizer,
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
            tokenizer_loader=_no_tokenizer,
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
            tokenizer_loader=_no_tokenizer,
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


def _mock_client(handler) -> httpx.AsyncClient:  # type: ignore[no-untyped-arg]
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:8082/v1/",
    )


_OK_INFO = {"model_id": "BAAI/bge-reranker-v2-m3", "model_sha": "commit-1"}


@pytest.mark.asyncio
async def test_remote_reranker_attests_once_across_repeated_score_calls() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/info":
            return httpx.Response(200, json=_OK_INFO)
        return httpx.Response(200, json=[{"index": 0, "score": 0.5}])

    async with _mock_client(handler) as client:
        reranker = RemoteReranker(
            _spec(),
            base_url="http://127.0.0.1:8082/v1",
            served_model="bge-reranker-v2-m3",
            timeout_seconds=10,
            client=client,
            tokenizer_loader=_no_tokenizer,
        )
        for _ in range(3):
            assert await reranker.score("q", ["a"]) == [0.5]
        await reranker.aclose()

    assert calls == ["/info", "/rerank", "/rerank", "/rerank"]


@pytest.mark.asyncio
async def test_failed_attestation_is_not_cached_and_retried_next_score() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"model_id": "evil", "model_sha": "commit-1"})

    async with _mock_client(handler) as client:
        reranker = RemoteReranker(
            _spec(),
            base_url="http://127.0.0.1:8082/v1",
            served_model="bge-reranker-v2-m3",
            timeout_seconds=10,
            client=client,
            tokenizer_loader=_no_tokenizer,
        )
        for _ in range(2):
            with pytest.raises(RuntimeError, match="model_id"):
                await reranker.score("q", ["a"])

    assert calls == ["/info", "/info"]


@pytest.mark.asyncio
async def test_remote_reranker_retries_429_then_succeeds_honouring_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    posts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.url.path == "/info":
            return httpx.Response(200, json=_OK_INFO)
        posts += 1
        if posts == 1:
            return httpx.Response(
                429,
                json={"error": "Model is overloaded"},
                headers={"retry-after": "1"},
            )
        return httpx.Response(
            200,
            json=[{"index": 1, "score": 0.8}, {"index": 0, "score": 0.2}],
        )

    async with _mock_client(handler) as client:
        reranker = RemoteReranker(
            _spec(),
            base_url="http://127.0.0.1:8082/v1",
            served_model="bge-reranker-v2-m3",
            timeout_seconds=10,
            client=client,
            tokenizer_loader=_no_tokenizer,
        )
        assert await reranker.score("q", ["a", "b"]) == [0.2, 0.8]
        await reranker.aclose()

    assert posts == 2
    assert sleeps == [1.0]


@pytest.mark.asyncio
async def test_remote_reranker_retries_429_during_attestation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    infos = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal infos
        if request.url.path == "/info":
            infos += 1
            if infos == 1:
                return httpx.Response(429, json={"error": "Model is overloaded"})
            return httpx.Response(200, json=_OK_INFO)
        return httpx.Response(200, json=[{"index": 0, "score": 0.6}])

    async with _mock_client(handler) as client:
        reranker = RemoteReranker(
            _spec(),
            base_url="http://127.0.0.1:8082/v1",
            served_model="bge-reranker-v2-m3",
            timeout_seconds=10,
            client=client,
            tokenizer_loader=_no_tokenizer,
        )
        assert await reranker.score("q", ["a"]) == [0.6]
        await reranker.aclose()

    assert infos == 2
    assert len(sleeps) == 1
    assert 0.125 <= sleeps[0] <= 0.25


@pytest.mark.asyncio
async def test_remote_reranker_exhausts_bounded_retries_and_keeps_fail_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    posts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.url.path == "/info":
            return httpx.Response(200, json=_OK_INFO)
        posts += 1
        return httpx.Response(429, json={"error": "Model is overloaded"})

    async with _mock_client(handler) as client:
        reranker = RemoteReranker(
            _spec(),
            base_url="http://127.0.0.1:8082/v1",
            served_model="bge-reranker-v2-m3",
            timeout_seconds=10,
            client=client,
            tokenizer_loader=_no_tokenizer,
        )
        # Exhaustion keeps the historical httpx failure semantics that the
        # no-rerank fail-open path in qa/service.py depends on.
        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            await reranker.score("q", ["a"])
    assert excinfo.value.response.status_code == 429
    assert posts == 3
    assert len(sleeps) == 2
    assert 0.125 <= sleeps[0] <= 0.25
    assert 0.25 <= sleeps[1] <= 0.5


@pytest.mark.asyncio
async def test_score_truncates_documents_to_the_pinned_token_budget() -> None:
    bodies: list[dict[str, object]] = []
    tokenizer = _PseudoTokenizer()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(200, json=_OK_INFO)
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=[{"index": 1, "score": 0.9}, {"index": 0, "score": 0.1}],
        )

    async with _mock_client(handler) as client:
        reranker = RemoteReranker(
            _spec(),
            base_url="http://127.0.0.1:8082/v1",
            served_model="bge-reranker-v2-m3",
            timeout_seconds=10,
            client=client,
            tokenizer_loader=lambda: tokenizer,
        )
        assert await reranker.score("q", ["x" * 600, "가나다"]) == [0.1, 0.9]
        await reranker.aclose()

    assert bodies[0]["texts"] == ["T480", "T3"]
    assert tokenizer.encode_calls == 2


@pytest.mark.asyncio
async def test_score_keeps_documents_verbatim_when_tokenizer_unavailable() -> None:
    bodies: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(200, json=_OK_INFO)
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=[{"index": 0, "score": 0.4}, {"index": 1, "score": 0.3}],
        )

    async with _mock_client(handler) as client:
        reranker = RemoteReranker(
            _spec(),
            base_url="http://127.0.0.1:8082/v1",
            served_model="bge-reranker-v2-m3",
            timeout_seconds=10,
            client=client,
            tokenizer_loader=_no_tokenizer,
        )
        docs = ["x" * 600, "short"]
        assert await reranker.score("q", docs) == [0.4, 0.3]
        await reranker.aclose()

    assert bodies[0]["texts"] == ["x" * 600, "short"]


@pytest.mark.asyncio
async def test_tokenizer_loader_runs_once_across_repeated_score_calls() -> None:
    loader_calls = 0
    tokenizer = _PseudoTokenizer()

    def loader() -> _PseudoTokenizer:
        nonlocal loader_calls
        loader_calls += 1
        return tokenizer

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(200, json=_OK_INFO)
        return httpx.Response(
            200,
            json=[{"index": 0, "score": 0.5}, {"index": 1, "score": 0.6}],
        )

    async with _mock_client(handler) as client:
        reranker = RemoteReranker(
            _spec(),
            base_url="http://127.0.0.1:8082/v1",
            served_model="bge-reranker-v2-m3",
            timeout_seconds=10,
            client=client,
            tokenizer_loader=loader,
        )
        for _ in range(2):
            assert await reranker.score("q", ["a", "bb"]) == [0.5, 0.6]
        await reranker.aclose()

    assert loader_calls == 1
    assert tokenizer.encode_calls == 4
