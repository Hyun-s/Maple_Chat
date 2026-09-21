# Maple Chat Local LLM·RAG·Agent·Discord E2E 검증

기준일: 2026-09-02 KST

## 1. 검증 범위

실제 로컬 Qwen과 운영 PostgreSQL을 사용해 다음 vertical slice를 검증했다.

```text
Discord mention
  → 접근/routing 정책
  → BGE-M3 query embedding
  → pgvector dense + pg_trgm lexical retrieval
  → RRF + BGE cross-encoder reranking
  → exact knowledge-graph traversal
  → local Qwen SSE generation
  → answer/source persistence
  → Discord reply
```

별도로 실제 Qwen이 Agent plan을 생성하고 local tool을 선택한 뒤 observation을 평가해 종료하며,
`agent_runs`와 `agent_tool_calls`에 checkpoint를 남기는 경로를 검증했다.

## 2. 환경과 제약

- Generator: `Qwen/Qwen3.8-27B`, served model `local-coder`
- Serving: local vLLM `http://127.0.0.1:8001/v1`
- Embedding: `BAAI/bge-m3`, 1024차원
- Reranker: `BAAI/bge-reranker-v2-m3`
- Database: PostgreSQL 16 + pgvector + pg_trgm, Alembic `g010_agent_runs`
- Discord: private guild, 허용 채널 direct mention
- `NEXON_API_KEY`: 미설정

NEXON API key가 없으므로 실제 캐릭터 Open API tool E2E는 범위 밖이다. 공식 API를 제외한
community retrieval, official update retrieval, knowledge graph와 Agent orchestration은 검증했다.

## 3. 결과

### 3.1 Readiness와 직접 생성

```text
database=ready
discord_configuration=ready
local_llm=ready
embedding_revision=bge-m3-5617a9f61b02
```

SSE smoke prompt는 정확히 `E2E_OK`를 반환했다.

| 지표 | 값 |
|---|---:|
| prompt tokens | 34 |
| completion tokens | 5 |
| total tokens | 39 |
| request time | 2.504초 |

### 3.2 실제 RAG 생성

검증 질문은 제논·하드 메이린이라는 명시적 job/boss scope와 최소컷 사례를 함께 포함했다.

| 검증 항목 | 결과 |
|---|---:|
| answer mode | `generated` |
| community evidence | 6개 |
| knowledge facts | 5개 |
| requester-scoped source view | 9개 |
| input/output/total tokens | 5,522 / 253 / 5,775 |
| model calls | 1회 |
| total time | 69.217초 |

답변은 제논의 도적·해적 multiple inheritance와 검색된 하드 메이린 93% 성공 사례를 구분했고,
단일 community 성공 기록을 보편적·공식 최소컷으로 확정하지 않았다. 검증용 answer/source row는
확인 직후 삭제했다.

### 3.3 실제 Tool-using Agent

Agent에는 local `query_knowledge_graph`, `search_community`, `search_official_updates` registry를
제공하고 제논의 전투 계열을 질문했다.

| 검증 항목 | 결과 |
|---|---:|
| run state | `succeeded` |
| selected tool | `query_knowledge_graph` |
| tool calls | 1회 |
| checkpoint | `complete` |
| model calls | 3회: plan, act, finish |
| input/output/total tokens | 1,722 / 201 / 1,923 |
| total time | 47.595초 |

실제 tool 이력에 따라 `Data based on Maple Chat knowledge graph`가 표시되었으며, Agent 검증용
run/tool/answer row는 확인 직후 삭제했다.

### 3.4 실제 Discord mention

사용자가 허용 채널에서 `@ClaudeBot 제논은 어떤 직업 계열이야?`를 전송했다.

| 검증 항목 | 결과 |
|---|---:|
| Discord authentication | 통과 |
| Gateway connection | 통과 |
| answer completion event | 확인 |
| answer mode | `generated` |
| persisted community chunks | 5개 |
| persisted graph relations | 7개 |
| input/output/total tokens | 4,829 / 75 / 4,904 |
| model calls | 1회 |
| model time | 21.340초 |
| end-to-end handler time | 24.546초 |

Discord 답변과 source provenance는 실제 사용자 요청 기록이므로 보존했다. 검증용 bot process는
Gateway 확인 후 종료했으며 사용자가 시작한 vLLM container는 중지하지 않았다.

## 4. E2E에서 발견하고 수정한 문제

### 4.1 잘못된 Agent attribution

**증상:** knowledge graph만 호출한 Agent 답변에도 고정된
`Data based on NEXON Open API` 문구가 붙었다.

**수정:** 최종 provenance를 모델 텍스트가 아니라 실제 tool call 이력에서 계산한다.

- NEXON character tools → `Data based on NEXON Open API`
- `search_official_updates` → `Data based on NEXON official updates`
- `query_knowledge_graph` → `Data based on Maple Chat knowledge graph`
- `search_community` → `Data based on Maple Chat indexed community evidence`

모델이 스스로 출력한 attribution line은 제거하고 결정론적으로 다시 생성한다.

### 4.2 local model metadata network access

**증상:** `local_files_only=True`인데도 Sentence Transformers 초기화 중 Hugging Face metadata
HTTP 요청이 발생했다.

**수정:** embedding과 reranker module import 전에 `HF_HUB_OFFLINE=1`과
`TRANSFORMERS_OFFLINE=1`을 강제한다.

새 Discord runtime에서 Hugging Face HTTP request 0건과 정상 Gateway 연결을 함께 확인했다.

## 5. 자동 회귀 검증

E2E 수정 후 임시 PostgreSQL container에서 재검증했다.

```text
232 passed
branch-aware coverage 90.84%
Alembic upgrade → downgrade → upgrade passed
Ruff passed
format passed
Mypy strict passed (61 source files)
```

## 6. 남은 검증 공백

- 실제 NEXON character API tool: `NEXON_API_KEY` 미설정으로 제외
- Discord `--agent` production route: 같은 이유로 현재는 미설정 안내를 반환
- latency 수치는 단일 실행 관찰값이며 benchmark나 percentile SLO가 아님
- aarch64 Conda의 `nvidia-cusparselt-cu13` metadata 문제로 통합 `verify_all.sh` wrapper의
  `pip check`는 별도 환경 정리가 필요함

핵심 local LLM, hybrid RAG, knowledge graph, Agent local tool, persistence, Discord Gateway와 실제
사용자 mention-to-reply 경로는 모두 통과했다.
