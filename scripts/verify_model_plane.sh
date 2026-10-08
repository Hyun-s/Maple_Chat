#!/usr/bin/env bash

# Pre-flight the embedding and reranker sidecars before an indexing run.
#
# TEI keeps /health returning 200 while its CUDA context is degraded: the
# 2026-09-21..09-28 indexing freeze ran for a week with a green health endpoint
# and opaque 424 Failed Dependency responses from /v1/embeddings. The cheapest
# probe that actually catches that state is a chunk-length embed through the
# production client, attestation included, rather than /health.

set +x
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/_local_env.sh"
cd "$ROOT_DIR"

conda_run python - <<'PY'
import asyncio
import sys
import time

import httpx

from maple_chat.config import ProcessRole, Settings, load_settings
from maple_chat.runtime import build_embedding_provider, build_reranker

# ~680 Korean characters, the length of a structure-v1 article chunk. A
# degraded encoder still answers short inputs, so anything much shorter
# proves nothing.
_SENTENCE = "보스 공략 패턴이 변경되면서 유지력과 유틸기 순환이 중요해졌습니다. "
CHUNK = _SENTENCE * 20
SHORT = "힐러"
LONG_DOC = CHUNK * 2
# Mirrors the pgvector column width in maple_chat.db.models.Chunk.embedding.
EXPECTED_DIMENSIONS = 1024

REMEDIATION = """\
Likely cause: the BGE-M3 TEI sidecar is serving a degraded CUDA context while
/health stays green. Restart only the encoder container:
    docker restart dcm-embedding-bge-m3
Then re-run this check. Indexing resumes from the pending rows on its own.
"""


def _report(status: str, detail: str) -> None:
    print(f"model-plane {status}: {detail}", flush=True)


async def _check_embedding(settings: Settings) -> bool:
    provider = build_embedding_provider(settings)
    started = time.perf_counter()
    try:
        vectors = await provider.embed([CHUNK, SHORT])
    except httpx.HTTPStatusError as exc:
        _report("FAIL", f"embed HTTP {exc.response.status_code} {exc.response.reason_phrase} "
                        f"on a {len(CHUNK)}-character input")
        return False
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        _report("FAIL", f"embed {type(exc).__name__}: {exc}")
        return False
    finally:
        await provider.aclose()
    elapsed = time.perf_counter() - started
    if len(vectors) != 2:
        _report("FAIL", f"embed returned {len(vectors)} vectors for 2 inputs")
        return False
    if any(len(vector) != EXPECTED_DIMENSIONS for vector in vectors):
        _report("FAIL", f"embed returned dimensions {[len(v) for v in vectors]}, "
                        f"expected {EXPECTED_DIMENSIONS}")
        return False
    _report("ok", f"embed 2 inputs dim={EXPECTED_DIMENSIONS} in {elapsed:.2f}s")
    return True


async def _check_reranker(settings: Settings) -> bool:
    reranker = build_reranker(settings)
    started = time.perf_counter()
    try:
        scores = await reranker.score("보스 공략", [LONG_DOC, SHORT])
    except httpx.HTTPStatusError as exc:
        _report("FAIL", f"rerank HTTP {exc.response.status_code} {exc.response.reason_phrase} "
                        f"on a {len(LONG_DOC)}-character document")
        return False
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        _report("FAIL", f"rerank {type(exc).__name__}: {exc}")
        return False
    finally:
        await reranker.aclose()
    elapsed = time.perf_counter() - started
    if len(scores) != 2 or any(score != score for score in scores):
        _report("FAIL", f"rerank returned invalid scores {scores}")
        return False
    _report("ok", f"rerank 2 documents in {elapsed:.2f}s")
    return True


async def main() -> int:
    settings = load_settings(ProcessRole.INDEX_WORKER)
    embedding_healthy = await _check_embedding(settings)
    reranker_healthy = await _check_reranker(settings)
    if not embedding_healthy:
        print(REMEDIATION, file=sys.stderr, end="")
    return 0 if embedding_healthy and reranker_healthy else 1


raise SystemExit(asyncio.run(main()))
PY
