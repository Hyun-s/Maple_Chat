from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from maple_chat import runtime
from maple_chat.indexing.embedding import EmbeddingSpec, RemoteEmbeddingProvider
from maple_chat.retrieval.hybrid import RerankerSpec


def test_embedding_contract_requires_pinned_compatible_bge_m3() -> None:
    EmbeddingSpec("v1", "BAAI/bge-m3", "commit-1").validate()
    with pytest.raises(ValueError, match="immutable"):
        EmbeddingSpec("v1", "BAAI/bge-m3", "").validate()
    with pytest.raises(ValueError, match="incompatible"):
        EmbeddingSpec("v1", "BAAI/bge-m3", "commit-1", dimension=768).validate()


@pytest.mark.asyncio
async def test_remote_embedding_provider_preserves_revision_order_and_normalization() -> None:
    requests: list[dict[str, object]] = []
    request_targets: list[tuple[str, str]] = []
    first = [1.0, *([0.0] * 1023)]
    second = [0.0, 1.0, *([0.0] * 1022)]

    async def handler(request: httpx.Request) -> httpx.Response:
        request_targets.append((request.method, str(request.url)))
        if request.url.path == "/info":
            return httpx.Response(
                200,
                json={"model_id": "BAAI/bge-m3", "model_sha": "commit-1"},
            )
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": "bge-m3",
                "data": [
                    {"object": "embedding", "index": 1, "embedding": second},
                    {"object": "embedding", "index": 0, "embedding": first},
                ],
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:8081/v1/",
    ) as client:
        provider = RemoteEmbeddingProvider(
            EmbeddingSpec("v1", "BAAI/bge-m3", "commit-1"),
            base_url="http://127.0.0.1:8081/v1",
            served_model="bge-m3",
            timeout_seconds=10,
            client=client,
        )
        assert await provider.embed(["첫째", "둘째"]) == [first, second]
        await provider.aclose()

    assert requests == [
        {
            "model": "bge-m3",
            "input": ["첫째", "둘째"],
            "encoding_format": "float",
        }
    ]
    assert request_targets == [
        ("GET", "http://127.0.0.1:8081/info"),
        ("POST", "http://127.0.0.1:8081/v1/embeddings"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("info_payload", "error"),
    [
        ({"model_id": "other", "model_sha": "commit-1"}, "model_id"),
        ({"model_id": "BAAI/bge-m3", "model_sha": "other"}, "model_sha"),
        ({"model_sha": "commit-1"}, "model_id"),
        ({"model_id": "BAAI/bge-m3"}, "model_sha"),
    ],
)
async def test_remote_embedding_provider_rejects_unattested_tei(
    info_payload: dict[str, object],
    error: str,
) -> None:
    requests: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        return httpx.Response(200, json=info_payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:8081/v1/",
    ) as client:
        provider = RemoteEmbeddingProvider(
            EmbeddingSpec("v1", "BAAI/bge-m3", "commit-1"),
            base_url="http://127.0.0.1:8081/v1",
            served_model="bge-m3",
            timeout_seconds=10,
            client=client,
        )
        with pytest.raises(RuntimeError, match=error):
            await provider.embed(["text"])

    assert requests == [("GET", "/info")]


@pytest.mark.asyncio
async def test_remote_embedding_provider_rechecks_tei_attestation_per_batch() -> None:
    requests: list[tuple[str, str]] = []
    info_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal info_calls
        requests.append((request.method, request.url.path))
        if request.url.path == "/info":
            info_calls += 1
            return httpx.Response(
                200,
                json={
                    "model_id": "BAAI/bge-m3",
                    "model_sha": "commit-1" if info_calls == 1 else "changed",
                },
            )
        return httpx.Response(
            200,
            json={
                "model": "bge-m3",
                "data": [{"object": "embedding", "index": 0, "embedding": [1.0, *([0.0] * 1023)]}],
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:8081/v1/",
    ) as client:
        provider = RemoteEmbeddingProvider(
            EmbeddingSpec("v1", "BAAI/bge-m3", "commit-1"),
            base_url="http://127.0.0.1:8081/v1",
            served_model="bge-m3",
            timeout_seconds=10,
            client=client,
        )
        await provider.embed(["first"])
        with pytest.raises(RuntimeError, match="model_sha"):
            await provider.embed(["second"])

    assert requests == [
        ("GET", "/info"),
        ("POST", "/v1/embeddings"),
        ("GET", "/info"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(200, content=b"text-embeddings-inference"), "invalid JSON"),
        (httpx.Response(200, json=["model_id", "model_sha"]), "invalid response"),
    ],
)
async def test_remote_embedding_provider_rejects_unparsable_info_attestation(
    response: httpx.Response,
    error: str,
) -> None:
    requests: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        return response

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:8081/v1/",
    ) as client:
        provider = RemoteEmbeddingProvider(
            EmbeddingSpec("v1", "BAAI/bge-m3", "commit-1"),
            base_url="http://127.0.0.1:8081/v1",
            served_model="bge-m3",
            timeout_seconds=10,
            client=client,
        )
        with pytest.raises(RuntimeError, match=error):
            await provider.embed(["text"])

    assert requests == [("GET", "/info")]


@pytest.mark.asyncio
async def test_remote_embedding_provider_short_circuits_without_texts() -> None:
    """Indexing may ask for an empty page; that must not reach the model plane."""
    requests: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        raise AssertionError("no HTTP request expected for an empty batch")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:8081/v1/",
    ) as client:
        provider = RemoteEmbeddingProvider(
            EmbeddingSpec("v1", "BAAI/bge-m3", "commit-1"),
            base_url="http://127.0.0.1:8081/v1",
            served_model="bge-m3",
            timeout_seconds=10,
            client=client,
        )
        assert await provider.embed([]) == []

    assert requests == []


def _unit_vector(slot: int) -> list[float]:
    vector = [0.0] * 1024
    vector[slot] = 1.0
    return vector


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("advertised_cap", "expected_sizes"),
    [
        # TEI advertises more than the client may safely send: clamp to 32.
        (64, [32, 32, 6]),
        # TEI advertises a tighter cap than our ceiling: honour the service.
        (16, [16, 16, 16, 16, 6]),
    ],
)
async def test_remote_embedding_provider_shards_requests_within_service_batch_cap(
    advertised_cap: int,
    expected_sizes: list[int],
) -> None:
    """Indexing hands one ``embed`` call every chunk draft of a whole batch.

    TEI answers 422 once a request carries more inputs than its
    ``--max-client-batch-size``, so the client must shard while keeping the
    flattened vector order identical to the input order.
    """
    assert sum(expected_sizes) == 70

    batches: list[list[str]] = []
    targets: list[tuple[str, str]] = []
    emitted = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal emitted
        targets.append((request.method, request.url.path))
        if request.url.path == "/info":
            return httpx.Response(
                200,
                json={
                    "model_id": "BAAI/bge-m3",
                    "model_sha": "commit-1",
                    "max_client_batch_size": advertised_cap,
                },
            )
        payload = json.loads(request.content)
        assert payload["model"] == "bge-m3"
        assert payload["encoding_format"] == "float"
        inputs = list(payload["input"])
        batches.append(inputs)
        offset = emitted
        emitted += len(inputs)
        # Reply out of order inside the window to prove index reordering holds.
        data = [
            {
                "object": "embedding",
                "index": position,
                "embedding": _unit_vector(offset + position),
            }
            for position in reversed(range(len(inputs)))
        ]
        return httpx.Response(200, json={"object": "list", "model": "bge-m3", "data": data})

    texts = [f"chunk-{index}" for index in range(70)]

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:8081/v1/",
    ) as client:
        provider = RemoteEmbeddingProvider(
            EmbeddingSpec("v1", "BAAI/bge-m3", "commit-1"),
            base_url="http://127.0.0.1:8081/v1",
            served_model="bge-m3",
            timeout_seconds=10,
            client=client,
        )
        vectors = await provider.embed(texts)
        await provider.aclose()

    assert [len(batch) for batch in batches] == expected_sizes
    cursor = 0
    for batch, size in zip(batches, expected_sizes, strict=True):
        assert batch == texts[cursor : cursor + size]
        cursor += size
    assert cursor == len(texts)
    # One attestation per embed() call, never one per shard.
    assert targets.count(("GET", "/info")) == 1
    assert targets.count(("POST", "/v1/embeddings")) == len(expected_sizes)
    assert vectors == [_unit_vector(index) for index in range(70)]


@pytest.mark.asyncio
async def test_remote_embedding_provider_shards_without_advertised_capability() -> None:
    """Older TEI /info payloads omit the cap; the safe ceiling still applies."""
    batch_sizes: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(
                200,
                json={"model_id": "BAAI/bge-m3", "model_sha": "commit-1"},
            )
        inputs = list(json.loads(request.content)["input"])
        batch_sizes.append(len(inputs))
        return httpx.Response(
            200,
            json={
                "model": "bge-m3",
                "data": [
                    {"object": "embedding", "index": index, "embedding": _unit_vector(index)}
                    for index in range(len(inputs))
                ],
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:8081/v1/",
    ) as client:
        provider = RemoteEmbeddingProvider(
            EmbeddingSpec("v1", "BAAI/bge-m3", "commit-1"),
            base_url="http://127.0.0.1:8081/v1",
            served_model="bge-m3",
            timeout_seconds=10,
            client=client,
        )
        vectors = await provider.embed([f"chunk-{index}" for index in range(33)])
        await provider.aclose()

    assert batch_sizes == [32, 1]
    assert len(vectors) == 33


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bad_payload", "error"),
    [
        (b"gateway-upstream", "invalid JSON"),
        (["data"], "invalid response"),
        ({"model": "bge-m3", "data": []}, "wrong vector count"),
        (
            {
                "model": "bge-m3",
                "data": [
                    "not-an-entry",
                    {"object": "embedding", "index": 0, "embedding": [1.0] * 1024},
                ],
            },
            "invalid vector entry",
        ),
        (
            {
                "model": "bge-m3",
                "data": [
                    {"object": "embedding", "index": 7, "embedding": [1.0] * 1024},
                    {"object": "embedding", "index": 0, "embedding": [1.0] * 1024},
                ],
            },
            "invalid vector ordering",
        ),
        (
            {
                "model": "bge-m3",
                "data": [
                    {"object": "embedding", "index": 0, "embedding": _unit_vector(0)},
                    {"object": "embedding", "index": 0, "embedding": _unit_vector(0)},
                ],
            },
            "invalid vector ordering",
        ),
    ],
)
async def test_remote_embedding_provider_rejects_malformed_window_responses(
    bad_payload: object,
    error: str,
) -> None:
    """A shard that answers anything but a full, in-range vector set must fail closed."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(
                200,
                json={"model_id": "BAAI/bge-m3", "model_sha": "commit-1"},
            )
        if isinstance(bad_payload, bytes):
            return httpx.Response(200, content=bad_payload)
        return httpx.Response(200, json=bad_payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:8081/v1/",
    ) as client:
        provider = RemoteEmbeddingProvider(
            EmbeddingSpec("v1", "BAAI/bge-m3", "commit-1"),
            base_url="http://127.0.0.1:8081/v1",
            served_model="bge-m3",
            timeout_seconds=10,
            client=client,
        )
        # Two texts: the "incomplete" shape returns only one vector for two inputs.
        with pytest.raises(RuntimeError, match=error):
            await provider.embed(["첫째", "둘째"])


@pytest.mark.asyncio
async def test_remote_embedding_provider_closes_a_client_it_owns() -> None:
    """Without an injected client the provider must release its own connection pool."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(
                200,
                json={"model_id": "BAAI/bge-m3", "model_sha": "commit-1"},
            )
        return httpx.Response(
            200,
            json={
                "model": "bge-m3",
                "data": [
                    {
                        "object": "embedding",
                        "index": 0,
                        "embedding": _unit_vector(0),
                    }
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    provider = RemoteEmbeddingProvider(
        EmbeddingSpec("v1", "BAAI/bge-m3", "commit-1"),
        base_url="http://127.0.0.1:8081/v1",
        served_model="bge-m3",
        timeout_seconds=10,
    )
    provider._client = httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8081/v1/")
    provider._owns_client = True
    assert await provider.embed(["첫째"]) == [_unit_vector(0)]
    await provider.aclose()
    assert provider._client.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_patch", "error"),
    [
        ({"model": "different"}, "model"),
        (
            {"data": [{"object": "embedding", "index": 0, "embedding": [0.0] * 1024}]},
            "non-normalized",
        ),
        (
            {"data": [{"object": "embedding", "index": 0, "embedding": [1.0] * 768}]},
            "dimension",
        ),
        (
            {"data": [{"object": "embedding", "index": 0, "embedding": [1.0] * 1025}]},
            "dimension",
        ),
        (
            {
                "data": [
                    {
                        "object": "embedding",
                        "index": 0,
                        "embedding": [float("nan"), *([0.0] * 1023)],
                    }
                ]
            },
            "non-finite",
        ),
        (
            {
                "data": [
                    {
                        "object": "embedding",
                        "index": 0,
                        "embedding": [True, *([0.0] * 1023)],
                    }
                ]
            },
            "non-finite",
        ),
        (
            {
                "data": [
                    {
                        "object": "embedding",
                        "index": 0,
                        "embedding": [10**400, *([0.0] * 1023)],
                    }
                ]
            },
            "non-finite",
        ),
    ],
)
async def test_remote_embedding_provider_rejects_contract_drift(
    response_patch: dict[str, object],
    error: str,
) -> None:
    payload: dict[str, object] = {
        "object": "list",
        "model": "bge-m3",
        "data": [{"object": "embedding", "index": 0, "embedding": [1.0, *([0.0] * 1023)]}],
    }
    payload.update(response_patch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(
                200,
                json={"model_id": "BAAI/bge-m3", "model_sha": "commit-1"},
            )
        return httpx.Response(
            200,
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:8081/v1/",
    ) as client:
        provider = RemoteEmbeddingProvider(
            EmbeddingSpec("v1", "BAAI/bge-m3", "commit-1"),
            base_url="http://127.0.0.1:8081/v1",
            served_model="bge-m3",
            timeout_seconds=10,
            client=client,
        )
        with pytest.raises(RuntimeError, match=error):
            await provider.embed(["text"])


@pytest.mark.asyncio
async def test_remote_embedding_provider_accepts_exact_container_endpoint_only() -> None:
    async with httpx.AsyncClient(base_url="http://dcm-embedding:80/v1/") as client:
        RemoteEmbeddingProvider(
            EmbeddingSpec("v1", "BAAI/bge-m3", "commit-1"),
            base_url="http://dcm-embedding:80/v1",
            served_model="bge-m3",
            timeout_seconds=10,
            client=client,
        )

    with pytest.raises(ValueError, match="local model boundary"):
        RemoteEmbeddingProvider(
            EmbeddingSpec("v1", "BAAI/bge-m3", "commit-1"),
            base_url="http://dcm-embedding.example:80/v1",
            served_model="bge-m3",
            timeout_seconds=10,
        )


def test_remote_embedding_provider_rejects_nonlocal_endpoint() -> None:
    with pytest.raises(ValueError, match="local model boundary"):
        RemoteEmbeddingProvider(
            EmbeddingSpec("v1", "BAAI/bge-m3", "commit-1"),
            base_url="https://example.com/v1",
            served_model="bge-m3",
            timeout_seconds=10,
        )


def test_embedding_provider_selection_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    remote_provider = object()
    calls: list[tuple[str, object, dict[str, object]]] = []

    def build_remote(spec: EmbeddingSpec, **kwargs: object) -> object:
        calls.append(("remote", spec, kwargs))
        return remote_provider

    monkeypatch.setattr(runtime, "RemoteEmbeddingProvider", build_remote)
    settings = SimpleNamespace(
        embedding_provider="remote",
        embedding_model="BAAI/bge-m3",
        embedding_model_commit="commit-1",
        embedding_base_url="http://127.0.0.1:8081/v1",
        embedding_remote_model="bge-m3",
        embedding_timeout_seconds=10.0,
        embedding_device="cpu",
    )

    assert runtime.build_embedding_provider(settings) is remote_provider
    assert [call[0] for call in calls] == ["remote"]
    assert calls[0][2] == {
        "base_url": "http://127.0.0.1:8081/v1",
        "served_model": "bge-m3",
        "timeout_seconds": 10.0,
    }

    settings.embedding_provider = "local"
    with pytest.raises(ValueError, match="EMBEDDING_PROVIDER"):
        runtime.build_embedding_provider(settings)
    assert [call[0] for call in calls] == ["remote"]


def test_remote_provider_construction_failure_never_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_remote(spec: EmbeddingSpec, **kwargs: object) -> object:
        raise RuntimeError("remote unavailable")

    monkeypatch.setattr(runtime, "RemoteEmbeddingProvider", fail_remote)
    settings = SimpleNamespace(
        embedding_provider="remote",
        embedding_model="BAAI/bge-m3",
        embedding_model_commit="commit-1",
        embedding_base_url="http://127.0.0.1:8081/v1",
        embedding_remote_model="bge-m3",
        embedding_timeout_seconds=10.0,
        embedding_device="cpu",
    )

    with pytest.raises(RuntimeError, match="remote unavailable"):
        runtime.build_embedding_provider(settings)


def test_reranker_contract_rejects_unpinned_or_wrong_model() -> None:
    with pytest.raises(ValueError, match="immutable"):
        RerankerSpec("BAAI/bge-reranker-v2-m3", "").validate()
    with pytest.raises(ValueError, match="BGE"):
        RerankerSpec("other", "commit").validate()
