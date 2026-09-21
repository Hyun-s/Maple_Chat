"""Pinned embedding provider contract and idempotent PostgreSQL writes."""

from __future__ import annotations

import asyncio
import math
import os
from dataclasses import dataclass
from importlib import import_module
from typing import Any, ClassVar, Protocol, cast
from urllib.parse import urlparse

import httpx
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from maple_chat.db.models import Chunk, ChunkEmbedding, EmbeddingRevision
from maple_chat.indexing.chunking import ChunkDraft


@dataclass(frozen=True, slots=True)
class EmbeddingSpec:
    name: str
    model: str
    model_commit: str
    dimension: int = 1024
    normalized: bool = True
    distance: str = "cosine"

    def validate(self) -> None:
        if self.model != "BAAI/bge-m3" or not self.model_commit:
            raise ValueError("BGE-M3 model and immutable commit are required")
        if self.dimension != 1024 or not self.normalized or self.distance != "cosine":
            raise ValueError("embedding specification is incompatible")


class EmbeddingProvider(Protocol):
    spec: EmbeddingSpec

    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    async def aclose(self) -> None: ...


class SentenceTransformerEmbeddingProvider:
    """Optional local BGE-M3 adapter; model loading is explicit and never cloud-backed."""

    def __init__(self, spec: EmbeddingSpec, *, device: str = "auto") -> None:
        spec.validate()
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        if device not in {"auto", "cpu", "cuda"}:
            raise ValueError("embedding device must be auto, cpu, or cuda")
        if device == "cuda":
            torch = import_module("torch")
            if not torch.cuda.is_available():
                raise RuntimeError("CUDA is required for the configured embedding device")
        try:
            module = import_module("sentence_transformers")
        except ImportError as exc:  # pragma: no cover - optional production dependency
            raise RuntimeError("install the 'ml' optional dependency for local embeddings") from exc
        self.spec = spec
        transformer: Any = module.SentenceTransformer
        self._model: Any = transformer(
            spec.model,
            revision=spec.model_commit,
            local_files_only=True,
            device=None if device == "auto" else device,
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = await asyncio.to_thread(
            self._model.encode,
            texts,
            normalize_embeddings=True,
        )
        return cast(list[list[float]], vectors.tolist())

    async def aclose(self) -> None:
        """Match the provider lifecycle contract without owning network resources."""


class RemoteEmbeddingProvider:
    """Approved-local OpenAI-compatible BGE-M3 embedding client."""

    _ALLOWED_HOSTS: ClassVar[set[str]] = {
        "127.0.0.1",
        "::1",
        "localhost",
        "host.docker.internal",
        "dcm-embedding",
    }

    def __init__(
        self,
        spec: EmbeddingSpec,
        *,
        base_url: str,
        served_model: str,
        timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        spec.validate()
        if not served_model.strip():
            raise ValueError("embedding served model must not be empty")
        parsed = urlparse(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in self._ALLOWED_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("embedding base URL must target the approved local model boundary")
        self.spec = spec
        self._served_model = served_model
        self._info_url = parsed._replace(path="/info").geturl()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/",
            timeout=timeout_seconds,
        )

    async def _attest_model(self) -> None:
        response = await self._client.get(self._info_url)
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("embedding service /info returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("embedding service /info returned an invalid response")
        if payload.get("model_id") != self.spec.model:
            raise RuntimeError("embedding service /info model_id does not match")
        if payload.get("model_sha") != self.spec.model_commit:
            raise RuntimeError("embedding service /info model_sha does not match")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        await self._attest_model()
        response = await self._client.post(
            "embeddings",
            json={
                "model": self._served_model,
                "input": texts,
                "encoding_format": "float",
            },
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("embedding service returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("embedding service returned an invalid response")
        response_model = payload.get("model")
        if response_model is not None and response_model != self._served_model:
            raise RuntimeError("embedding service model does not match the configured revision")

        data = payload.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise RuntimeError("embedding service returned the wrong vector count")
        vectors: list[list[float] | None] = [None] * len(texts)
        for item in data:
            if not isinstance(item, dict):
                raise RuntimeError("embedding service returned an invalid vector entry")
            index = item.get("index")
            raw_vector = item.get("embedding")
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or index < 0
                or index >= len(texts)
                or vectors[index] is not None
                or not isinstance(raw_vector, list)
            ):
                raise RuntimeError("embedding service returned invalid vector ordering")
            if len(raw_vector) != self.spec.dimension:
                raise RuntimeError("embedding service returned the wrong vector dimension")
            vector: list[float] = []
            for value in raw_vector:
                if isinstance(value, bool) or not isinstance(value, int | float):
                    raise RuntimeError("embedding service returned non-finite vector values")
                try:
                    numeric_value = float(value)
                except OverflowError as exc:
                    raise RuntimeError(
                        "embedding service returned non-finite vector values"
                    ) from exc
                if not math.isfinite(numeric_value):
                    raise RuntimeError("embedding service returned non-finite vector values")
                vector.append(numeric_value)
            norm = math.sqrt(sum(value * value for value in vector))
            if not math.isclose(norm, 1.0, rel_tol=1e-3, abs_tol=1e-3):
                raise RuntimeError("embedding service returned a non-normalized vector")
            vectors[index] = vector
        if any(vector is None for vector in vectors):
            raise RuntimeError("embedding service returned incomplete vector ordering")
        return cast(list[list[float]], vectors)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


async def index_chunk_batch(
    session: AsyncSession,
    *,
    drafts: tuple[ChunkDraft, ...],
    revision: EmbeddingRevision,
    provider: EmbeddingProvider,
) -> int:
    provider.spec.validate()
    if (
        revision.name != provider.spec.name
        or revision.model != provider.spec.model
        or revision.model_commit != provider.spec.model_commit
        or revision.dimension != provider.spec.dimension
        or revision.normalized != provider.spec.normalized
        or revision.distance != provider.spec.distance
    ):
        raise ValueError("provider and database embedding revisions do not match")
    vectors = await provider.embed([draft.text for draft in drafts])
    if len(vectors) != len(drafts):
        raise ValueError("embedding provider returned the wrong vector count")

    written = 0
    for draft, vector in zip(drafts, vectors, strict=True):
        if len(vector) != revision.dimension:
            raise ValueError("embedding provider returned the wrong dimension")
        await session.execute(
            insert(Chunk)
            .values(
                chunk_id=draft.chunk_id,
                source_type=draft.source_type,
                source_key=draft.source_key,
                ordinal=draft.ordinal,
                text=draft.text,
                token_count=draft.token_count,
                metadata_json=draft.metadata,
                chunking_revision=draft.chunking_revision,
                active=True,
            )
            .on_conflict_do_update(
                index_elements=[Chunk.chunk_id],
                set_={Chunk.active: True, Chunk.metadata_json: draft.metadata},
            )
        )
        result = cast(
            CursorResult[Any],
            await session.execute(
                insert(ChunkEmbedding)
                .values(
                    chunk_id=draft.chunk_id,
                    revision_id=revision.id,
                    embedding=vector,
                    active=True,
                )
                .on_conflict_do_nothing(
                    index_elements=[ChunkEmbedding.chunk_id, ChunkEmbedding.revision_id]
                )
            ),
        )
        if result.rowcount:
            written += 1
    await session.flush()
    return written
