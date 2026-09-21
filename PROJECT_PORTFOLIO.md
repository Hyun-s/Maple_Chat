# Maple Chat — AI Research & Engineering Portfolio

> **포트폴리오 포지셔닝:** AI 연구 방법론을 중심으로, 연구 결과를 재현 가능한 RAG 서비스와
> 안전한 Tool-using Agent로 구현한 프로젝트
>
> 기준일: 2026-09-02  
> 구현 상태 기준: Alembic `g010_agent_runs`, 전체 237 tests 통과, branch-aware coverage 90.47%

## 1. Project Abstract

Maple Chat은 한국어 메이플스토리 도메인에서 **검색 누락, 약어 오해, 엔티티 혼동, 시간에 따른
정보 변화, 출처가 다른 주장 간 충돌**을 다루기 위해 만든 Knowledge-Graph-Augmented RAG 및
Tool-using Agent 프로젝트다.

핵심 연구 질문은 단순히 “LLM이 자연스러운 답을 만드는가?”가 아니다.

1. 비정형 커뮤니티 문서에서 질문과 관련된 근거를 얼마나 안정적으로 검색할 수 있는가?
2. dense retrieval의 의미 유사성과 lexical retrieval의 고유명사 정밀성을 어떻게 결합할 것인가?
3. 직업·보스·아이템의 canonical identity와 계층 관계를 생성 모델 밖에서 어떻게 보장할 것인가?
4. 근거가 없거나 충돌할 때 답변하지 않는 능력을 어떻게 시스템 계약으로 만들 것인가?
5. Agent가 외부 API와 내부 검색기를 사용할 때 모델의 자율성과 결정론적 안전 경계를 어떻게
   분리할 것인가?

이를 위해 **BGE-M3 bi-encoder, pgvector HNSW, pg_trgm lexical retrieval, Reciprocal Rank
Fusion, BGE cross-encoder reranking, exact knowledge-graph traversal, grounded generation,
canonical output validation, bounded ReAct-style Agent**를 하나의 평가 가능한 파이프라인으로
구성했다.

---

## 2. 기획 의도

### 2.1 문제 정의

게임 공략 도메인은 일반적인 문서 QA보다 다음 특성이 강하다.

- 같은 개념이 공식 명칭, 커뮤니티 약어, 오타, 직업별 표현으로 나타난다.
- 패치 전후의 사실이 함께 검색되며 “현재 정답”이 시간에 따라 달라진다.
- 평균 스펙, 최소컷, 개인 경험처럼 통계적 의미가 다른 주장이 섞인다.
- 직업군과 개별 직업, 보스 원형과 난이도 변형이 쉽게 혼동된다.
- 관련성이 높은 문서라도 질문한 엔티티가 아닌 다른 직업·보스의 수치를 포함할 수 있다.
- 검색된 커뮤니티 문서는 신뢰 가능한 명령이 아니라 비신뢰 데이터다.

따라서 단일 vector similarity나 프롬프트 지시만으로는 품질을 보장하기 어렵다. 본 프로젝트는
문제를 다음 세 층으로 분해했다.

| 층 | 연구 대상 | 시스템 구현 |
|---|---|---|
| Retrieval | 관련 근거를 찾는 문제 | dense + lexical + RRF + reranker |
| Knowledge | 명칭·계층·관계를 보존하는 문제 | provenance-bearing exact knowledge graph |
| Generation/Agent | 근거를 사용해 답하고 행동하는 문제 | grounded prompt, validator, bounded tools, checkpoint |

### 2.2 목표 사용자 경험

사용자는 Discord에서 자연어로 질문한다. 시스템은 질문 속 직업·보스·아이템을 식별하고,
커뮤니티 공략·공식 패치·검수된 지식 그래프를 서로 다른 신뢰 계층으로 조회한 뒤, 사용한 근거만
가지고 답한다. 캐릭터 분석이 필요하면 Agent가 공식 NEXON Open API를 선택적으로 호출하며,
실패한 실행은 저장된 계획과 툴 결과에서 재개할 수 있다.

### 2.3 성공 기준

- retrieval과 generation을 분리해 평가한다.
- 특정 예시를 맞추는 휴리스틱보다 실패 유형 전체를 줄이는 일반화된 수정안을 우선한다.
- 공식 관계, 커뮤니티 주장, 모델 생성 결과를 동일한 truth layer로 취급하지 않는다.
- 근거 부족 시 생성보다 abstention 또는 retrieval-only fallback을 선택한다.
- 모든 모델·임베딩 revision과 답변 근거를 재현 가능하게 보존한다.
- Agent의 툴 이름, 인자, 결과, 실행 한도와 재개 상태를 감사할 수 있어야 한다.

### 2.4 AI 연구 포지셔닝

이 프로젝트는 새 foundation model을 pre-train하거나 fine-tune한 연구로 포장하지 않는다. 핵심
기여 범위는 **information retrieval, representation 활용, knowledge representation, grounded
generation, agentic decision policy와 evaluation**이다.

도메인 적응은 모델 weight 변경보다 corpus·ontology·retrieval·verifier를 통한 non-parametric
adaptation으로 수행했다. 그 결과 지식 갱신 시 전체 모델을 다시 학습하지 않고 source revision과
graph relation을 교체할 수 있고, 답변 근거를 추적할 수 있다. 향후 fine-tuning을 적용하더라도
retrieval과 generation error를 먼저 분리한 뒤, 충분한 hard negative와 human judgment가 확보된
경우에만 query encoder 또는 reranker 학습을 실험하는 것이 타당하다.

---

## 3. 구현 Item과 현재 상태

### 3.1 AI 연구·모델링 Item

| Item | 상태 | 핵심 내용 |
|---|---|---|
| 한국어 도메인 corpus | 구현·운영 데이터 존재 | 게시글 19,192건, 댓글 76,803건 |
| 구조 기반 chunking | 구현 | 목표 550, 최대 700, overlap 80 token |
| Dense embedding | 구현 | `BAAI/bge-m3`, 1024차원, normalized vector |
| ANN retrieval | 구현 | PostgreSQL pgvector cosine + HNSW |
| Lexical retrieval | 구현 | pg_trgm 기반 한국어 문자열 검색 |
| Hybrid fusion | 구현 | RRF + 제한된 품질·최신성·authority prior |
| Cross-encoder reranking | 구현 | `BAAI/bge-reranker-v2-m3` |
| Entity-aware filtering | 구현 | 명시된 job/boss scope와 다른 근거 제외 |
| Knowledge graph | 구현 | 217 entities, 755 aliases, 292 active relations |
| Temporal/authority RAG | 구현 | 공식 패치와 community 근거의 신뢰 계층 분리 |
| Grounded generation | 구현 | 근거 JSON과 canonical relation만 조건으로 생성 |
| Canonical validator | 구현 | 약어 확장 오류 재생성 후 graph fallback |
| Abstention/fallback | 구현 | low evidence refusal, retrieval/graph-only fallback |
| Korean evaluation suite | 구현 | 110개 retrieval·answer·security 고정 fixture |
| Claim extraction/contradiction graph | 연구 과제 | 문장 내부 주장을 구조화하고 상충을 판정하는 단계 |

> 위 corpus 수치는 2026-09-01 로컬 PostgreSQL snapshot이다. 평가 fixture의 수치와 실제 사용자
> 질의 성능을 혼동하지 않는다.

### 3.2 Agent·Tool Item

| Item | 상태 | 핵심 내용 |
|---|---|---|
| bounded plan generation | 구현 | 목표와 1~6개 단계 JSON 계획 |
| ReAct-style decision loop | 구현 | 한 번에 하나의 tool 또는 finish 선택 |
| schema-first Tool Registry | 구현 | Pydantic 입력 검증, extra field 금지 |
| NEXON character tools | 구현 | 식별자·기본정보·스탯·장비·유니온 5개 |
| Local evidence tools | 구현 | community·공식 패치·knowledge graph 3개 |
| tool budget | 구현 | 기본 4회, 설정 가능 범위 1~8회 |
| loop prevention | 구현 | 동일 tool + arguments 반복 차단 |
| durable run history | 구현 | `agent_runs`, `agent_tool_calls` |
| checkpoint/resume | 구현 | Discord `--agent-resume <run_id>` |
| requester isolation | 구현 | requester hash, guild, TTL 검증 |
| state-changing confirmation | 정책·테스트 구현 / UI 미구현 | confirmation-required tool은 실행 차단 |
| MCP transport | 미구현 | 현재 registry를 MCP로 노출하기 위한 방법론만 설계 |

### 3.3 연구를 지지하는 시스템 Item

- sanitize-before-store 개인정보 정제와 salted HMAC nickname 처리
- embedding/chunking/model revision 고정과 active revision 전환
- PostgreSQL 기반 idempotent indexing, lease, retry, checkpoint
- 답변 시점의 chunk·knowledge relation provenance 보존
- local-only OpenAI-compatible Qwen generation 경계
- 토큰 수, TTFT 기반 입력 TPS, 출력 TPS, 전체 latency 관측
- Alembic upgrade → downgrade → upgrade 회귀 검증

---

## 4. AI 연구 방법론

### 4.1 연구 프로세스: failure-driven iteration

본 프로젝트의 핵심 방법론은 기능 목록을 순서대로 구현하는 것이 아니라 **실패를 연구 단위로
고정하고 일반화 가능한 원인을 찾는 것**이다.

```text
실패 질의 고정
  → 검색 결과·점수·엔티티·생성 답변 관찰
  → 코드로 확인된 사실과 추론 분리
  → retrieval / knowledge / generation 중 원인 층 식별
  → 최소 수정안 적용
  → 실패 질의를 regression test로 승격
  → 전체 family 성능과 부작용 재검증
```

대표적으로 다음 실패를 연구 artifact로 관리했다.

| 실패 유형 | 관찰 | 일반화된 해결 |
|---|---|---|
| 약어 hallucination | `놀긍`을 잘못 확장 | canonical `abbreviation_of` relation + output validator |
| taxonomy 오류 | 직업군과 직업을 혼동 | `is_a`, `part_of` 의미를 분리한 ontology |
| entity leakage | 질문하지 않은 보스 수치 사용 | query scope group filter + evidence graph merge 제한 |
| retrieval miss | 저장된 제논 성공 글을 누락 | entity anchor, title coverage, authored-article calibration |
| temporal conflict | 패치 전·후 주장이 동시 노출 | official authority와 published time을 metadata로 모델링 |
| heuristic overfit | 한 질의 수정이 다른 질의 훼손 | 사례 문자열 규칙 대신 ontology·scoring contract로 이동 |

### 4.2 데이터 구성과 전처리

#### Corpus unit

- article: 제목·본문·게시판·카테고리·게시 시각·추천/조회 수
- comment: 질문 문맥을 보조하는 parent article 일부 + 댓글 주장
- official update: 최근 패치의 제목·본문·게시/수정 시각과 `source_authority=official`
- curated graph: source version과 URL을 가진 entity/alias/relation

#### 안전한 연구 데이터 경계

원문 수집 직후 이메일·전화번호 등 정해진 PII를 마스킹하고, nickname은 원문 대신 salted
HMAC-SHA256만 저장한다. 이 단계는 privacy뿐 아니라 embedding과 evaluation corpus에 원치 않는
식별 신호가 남는 것을 줄이는 데이터 품질 정책이다.

### 4.3 Structure-aware Chunking

고정 길이 sliding window만 사용하지 않고 제목, 게시판, 분류, 작성일 header와 문단·Markdown
경계를 보존한다.

```text
header + semantic section groups
target_tokens = 550
max_tokens = 700
overlap_tokens = 80
```

이 설계의 가설은 다음과 같다.

- 너무 작은 chunk는 절차와 조건을 분리해 recall을 떨어뜨린다.
- 너무 큰 chunk는 관련 없는 문장을 함께 넣어 cross-encoder와 generator의 precision을 낮춘다.
- 매 chunk에 동일한 source header를 넣으면 제목·게시판·시점 신호를 retrieval 단계에서 사용할 수
  있다.
- overlap은 경계 근처 claim의 단절을 줄이되 embedding/storage 비용을 제한해야 한다.

### 4.4 Dense Retrieval

질의와 문서를 동일한 BGE-M3 bi-encoder 공간에 투영한다.

\[
q = f_{\theta}(x_q), \qquad d_i = f_{\theta}(x_i)
\]

normalized embedding을 사용하므로 cosine similarity는 내적과 동일하게 해석할 수 있다.

\[
s_{dense}(q,d_i)=\frac{q^\top d_i}{\|q\|\|d_i\|}
\]

문서 수 증가에 대응하기 위해 pgvector HNSW를 사용한다. model name만 저장하지 않고 immutable
commit, dimension, normalization, distance metric을 `embedding_revisions` 계약으로 묶어 실험의
재현성을 보존한다.

### 4.5 Lexical Retrieval

Dense retrieval은 의미가 비슷한 문장에 강하지만 짧은 한국어 고유명사, 보스명, 아이템 약어,
숫자 표현을 놓칠 수 있다. 이를 보완하기 위해 pg_trgm similarity를 독립 후보 생성기로 사용한다.

이 방식은 BM25와 동일하지 않다. 형태소 분석 기반 term frequency 모델 대신 character trigram
overlap을 사용하므로, 본 프로젝트에서는 “sparse semantic ranker”가 아니라 **문자열 정밀도를
보완하는 lexical channel**로 해석한다.

### 4.6 Reciprocal Rank Fusion

Dense와 lexical score는 calibration scale이 다르므로 raw score를 직접 합하지 않고 순위를
결합한다.

\[
s_{RRF}(d)=\sum_{m\in\{dense,lexical\}} \frac{1}{k+r_m(d)}, \qquad k=60
\]

여기에 추천·조회·카테고리·최신성·공식 authority prior를 작은 상한 안에서 더한다. prior가 의미
관련성을 덮지 않도록 community boost는 제한하며, 명시된 entity가 제목 또는 작성자 본문에
나타날 때만 bounded anchor를 적용한다.

연구적으로 중요한 점은 “좋아 보이는 휴리스틱을 많이 더하는 것”이 아니라 각 prior의 영향 범위를
작게 고정하고 회귀 질의에서 부작용을 측정하는 것이다.

### 4.7 Cross-encoder Reranking

후보 생성 단계의 bi-encoder는 corpus 전체를 빠르게 줄이는 데 사용하고, 상위 후보에는
BGE reranker cross-encoder를 적용한다.

\[
s_{rerank}(q,d)=g_{\phi}([q;d])
\]

Cross-encoder는 query-document token interaction을 직접 보므로 정밀도가 높지만 계산량이 크다.
따라서 전체 문서가 아니라 fusion 이후 제한된 후보에만 적용한다. source group당 후보 수와 최종
evidence 중복을 제한해 비슷한 댓글이 context를 독점하지 않도록 했다.

### 4.8 Entity-aware Retrieval

단순 relevance score가 높더라도 질문의 명시적 ontology facet과 다른 문서는 제외한다. 질문이
특정 job과 boss를 동시에 포함하면 후보 문서가 각 facet에서 하나 이상의 canonical entity를
포함해야 한다.

```text
query facets = {job: [제논], boss: [하드 세렌]}
accept(d) = contains_any(d, job facet) AND contains_any(d, boss facet)
```

이는 모델이 “비슷한 보스의 수치”를 질문한 보스에 대입하는 cross-entity contamination을 검색
단계에서 줄이는 장치다.

### 4.9 Knowledge-Graph-Augmented RAG

#### 표현

검수된 지식은 다음 triple과 provenance로 표현한다.

\[
(subject, predicate, object, source, version, valid\_time)
\]

예:

- `(제논, is_a, 도적)`
- `(제논, is_a, 해적)`
- `(놀긍, abbreviation_of, 놀라운 긍정의 혼돈 주문서)`
- `(하드 세렌, variant_of, 세렌)`

#### 왜 vector store와 분리했는가

Vector similarity는 관련 문서를 찾는 데 적합하지만, multiple inheritance, 정확한 약어 확장,
보스 variant identity와 provenance를 보장하지 않는다. 반대로 graph만으로는 비정형 공략과 경험
주장을 검색하기 어렵다. 따라서 다음처럼 역할을 분리한다.

| 저장소 | 강점 | 답변에서의 역할 |
|---|---|---|
| Vector/Lexical corpus | 비정형 경험·공략 검색 | 주장 근거 후보 |
| Official patch corpus | 시간에 따른 공식 변경 | 최신 authority evidence |
| Curated knowledge graph | identity·taxonomy·canonical relation | 명칭·계층·관계의 기준 |

질문과 검색된 evidence 양쪽에서 entity linking을 수행하되, evidence에서 유도한 graph fact가 질문의
명시적 job/boss scope를 교체하지 못하게 merge rule을 둔다.

### 4.10 Grounded Generation과 Deterministic Validation

생성 모델에는 자유 형식 원문 대신 두 종류의 구조화 context를 전달한다.

```text
<canonical_knowledge_json>  # 검수·버전 관리된 관계
<untrusted_evidence_json>   # 검색된 외부 문서와 metadata
```

생성 후 canonical 약어 relation과 다른 확장이 발견되면 correction JSON으로 한 번 재생성한다.
두 번째 결과도 위반하면 LLM 답변을 폐기하고 graph/retrieval-only fallback을 반환한다.

이는 prompt가 모든 오류를 예방할 것이라는 가정 대신 다음 verifier 구조를 사용한 것이다.

```text
generate → deterministic check → repair once → check → fallback
```

### 4.11 Abstention과 Graceful Degradation

RAG 품질은 정답률만이 아니라 근거가 없을 때 잘 멈추는 능력으로 평가해야 한다.

- retrieval과 graph가 모두 비어 있음 → 질문 구체화를 요청
- LLM unavailable + evidence 존재 → retrieval-only 결과
- LLM unavailable + graph 존재 → canonical relation-only 결과
- canonical validator 반복 실패 → 생성 결과 폐기
- Agent tool 실패 → 오류를 observation으로 환원해 다음 행동 선택

---

## 5. Agent 연구 및 개발 방법론

### 5.1 Constrained sequential decision problem

Agent 실행을 무제한 자율 루프가 아니라 budget이 있는 순차 의사결정 문제로 정의했다.

\[
s_t=(goal, plan, observations_{1:t}, budget_t)
\]

\[
a_t \in Tools \cup \{Finish\}
\]

모델은 후보 행동을 제안하지만 실제 전이 가능 여부는 코드가 결정한다.

```text
LLM decision JSON
  → action shape validation
  → registered tool lookup
  → Pydantic argument validation
  → risk policy
  → duplicate-call check
  → bounded execution
  → observation persistence
  → next decision or finish
```

### 5.2 Planner와 Actor 분리

- Planner: 사용자 목표를 1~6개의 검증 가능한 단계로 구조화
- Actor: 현재 observation에서 tool 하나 또는 finish를 선택
- Validator: JSON shape, tool name, arguments, budget을 결정론적으로 검증
- Store: 계획과 tool transition을 transaction으로 저장

계획은 사고 과정 전체를 노출하기 위한 것이 아니라 실행 범위와 재개 지점을 구조화하는 contract다.

### 5.3 Tool selection

현재 Agent가 선택할 수 있는 tool은 다음과 같다.

| Tool | 데이터 계층 | 입력 계약 |
|---|---|---|
| `resolve_character` | NEXON API | `character_name` |
| `get_character_basic` | NEXON API | `ocid`, optional `date` |
| `get_character_stat` | NEXON API | `ocid`, optional `date` |
| `get_character_equipment` | NEXON API | `ocid`, optional `date` |
| `get_union` | NEXON API | `ocid`, optional `date` |
| `search_community` | hybrid RAG | `query`, `top_k` |
| `search_official_updates` | official RAG | `query`, `top_k` |
| `query_knowledge_graph` | exact graph | `query`, `top_k` |

장비 API는 원 응답 전체를 context에 넣지 않고 장비명, 부위, 스타포스, 잠재능력 등 분석 필드만
요약한다. local retrieval tool도 excerpt와 점수를 제한해 context inflation을 방지한다.

### 5.4 Durable checkpoint와 resume

`agent_runs`에는 goal plan, 상태, checkpoint, 호출 수, 최대 budget, 만료 시각을 저장하고,
`agent_tool_calls`에는 검증된 인자와 bounded result를 순서대로 저장한다.

```text
queued → running → partial → succeeded
                   └──────→ failed → resume → running
```

재개 시 저장된 tool result를 observation으로 복원하므로 동일 외부 API를 다시 호출하지 않는다.
requester hash, guild, TTL을 확인해 다른 사용자가 실행을 가져갈 수 없게 한다.

### 5.5 Agent evaluation

Agent는 최종 답변만 평가하면 실패 원인을 찾기 어렵다. 다음 단위로 나누어 평가한다.

1. plan validity: goal과 1~6 steps 계약을 지키는가?
2. tool selection accuracy: 필요한 tool을 선택하고 불필요한 호출을 줄이는가?
3. argument validity: schema와 domain constraint를 통과하는가?
4. transition validity: tool result 이후 다음 행동이 타당한가?
5. loop safety: 동일 호출 반복과 budget 초과가 차단되는가?
6. recovery: LLM 장애 후 저장된 observation에서 재개하는가?
7. grounded answer: tool result 밖의 사실을 추가하지 않는가?

현재 자동 테스트는 invalid JSON correction, unknown/invalid tool, 반복 호출, budget, tool failure,
영속 plan, duplicate request, requester-scoped resume, checkpoint 이후 무중복 재개를 포함한다.

---

## 6. Tool / MCP 개발 방법론

### 6.1 현재 구현과 MCP의 구분

현재 코드는 프로젝트 내부 `ToolRegistry`를 사용하며 **MCP server transport 자체는 구현하지
않았다**. 다만 `name + description + JSON input schema + structured result + risk` 계약을 이미
사용하므로 MCP adapter로 확장하기 좋은 형태다.

MCP는 host-client-server 경계에서 tools, resources, prompts를 표준화한다. 2026-07-28 최신
specification은 stateless protocol core, cacheable list results, header-based routing과 extension
framework를 도입했다. 따라서 새 구현은 오래된 session 전제를 복사하지 않고 target SDK와 protocol
revision을 먼저 고정해야 한다.

- [MCP 2026-07-28 specification release](https://blog.modelcontextprotocol.io/posts/2026-07-28/)
- [MCP architecture](https://modelcontextprotocol.io/specification/2025-06-18/architecture)
- [MCP server primitives](https://modelcontextprotocol.io/specification/2025-06-18/server/index)
- [MCP Python SDK](https://py.sdk.modelcontextprotocol.io/get-started/)

### 6.2 Maple Chat capability mapping

| MCP primitive | Maple Chat mapping | 제어 주체 |
|---|---|---|
| Tool | community/patch/graph/character query | model-controlled, policy-enforced |
| Resource | 답변 provenance, graph source, Agent run summary | application-controlled |
| Prompt | 보스 준비 분석, 패치 영향 분석 template | user-controlled |

모든 기능을 tool로 만들지 않는다. 읽기만 필요한 provenance 문서는 resource가 적합하고, 사용자가
명시적으로 선택해야 하는 분석 양식은 prompt가 적합하다.

### 6.3 Schema-first tool contract

각 tool은 다음 항목을 가져야 한다.

```yaml
name: query_knowledge_graph
description: 검수된 직업·보스·아이템 관계 조회
inputSchema:
  type: object
  required: [query]
  additionalProperties: false
outputSchema:
  type: object
  required: [query, relations]
risk: read_only
timeout_seconds: 5
version: v1
```

개발 순서는 handler 구현보다 contract test가 먼저다.

1. capability와 사용자 가치를 한 문장으로 정의한다.
2. 입력 범위, output schema, timeout, error taxonomy를 고정한다.
3. read-only / confirmation-required / destructive risk를 분류한다.
4. 정상·경계·악성 입력 contract test를 작성한다.
5. domain service를 구현하고 protocol adapter는 얇게 유지한다.
6. in-memory MCP client와 실제 transport smoke test를 분리한다.

### 6.4 Security boundary

- MCP server에는 전체 대화나 DB credential을 전달하지 않는다.
- tool에는 필요한 최소 인자만 전달하고 arbitrary URL·SQL·shell을 허용하지 않는다.
- API credential은 server boundary 안에 두고 result/log에 포함하지 않는다.
- model-controlled tool call과 user authorization을 분리한다.
- 상태 변경 tool은 dry-run 결과와 영향 범위를 먼저 제시하고 durable confirmation을 요구한다.
- tool result의 문자열은 data이며 host prompt나 다른 server에 대한 명령으로 실행하지 않는다.
- remote MCP는 protocol revision에 맞는 authorization, issuer, scope, redirect 검증을 적용한다.

### 6.5 MCP adapter 목표 구조

```text
MCP host
  → capability discovery
  → Maple Chat MCP adapter
      ├─ tools/list  → ToolRegistry prompt catalog
      ├─ tools/call  → schema + risk policy → domain handler
      ├─ resources/read → provenance/run summary repository
      └─ prompts/get → versioned analysis template
  → existing retrieval / knowledge / NEXON services
```

Adapter가 business logic을 다시 구현하지 않도록 현재 registry와 domain service를 single source of
truth로 유지한다. MCP 도입 여부가 retrieval 품질이나 ontology 정확성을 바꾸지 않게 하는 것이
핵심이다.

### 6.6 MCP 검증 계획

- tools/list schema snapshot과 deterministic ordering
- valid/invalid `tools/call` structured result
- unknown tool과 extra argument fail-closed
- timeout, cancellation, retry-safe error classification
- secret/PII canary가 result와 log에 없는지 검사
- resource authorization과 requester scope 검사
- protocol revision mismatch와 unsupported capability 검사
- in-memory client contract test 후 stdio/HTTP smoke test
- state-changing tool의 confirmation denial/expiry/replay 검사

---

## 7. 평가 방법론과 실험 설계

### 7.1 Retrieval metrics

\[
Recall@k = \frac{|Relevant \cap Retrieved@k|}{|Relevant|}
\]

\[
MRR@k = \frac{1}{N}\sum_{i=1}^{N}\frac{1}{rank_i}
\]

전체 평균만 보면 특정 직업군이 무너져도 알기 어렵기 때문에 minimum family Recall@10을 별도
gate로 둔다.

### 7.2 Generation metrics

- expected answer-term rate
- unsupported answer rate
- exact source match rate
- prompt-injection/security violation count
- canonical expansion violation count
- evidence 없는 질문의 abstention accuracy

자동 term match는 human factuality 평가를 대체하지 않는다. 특히 자연스러운 표현, 부분적으로
맞는 주장, 출처가 문장을 실제로 entail하는지는 claim 단위 human judgment가 필요하다.

### 7.3 권장 ablation matrix

| Experiment | Dense | Lexical | RRF | Reranker | KG | Validator |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| A | ✓ |  |  |  |  |  |
| B |  | ✓ |  |  |  |  |
| C | ✓ | ✓ | ✓ |  |  |  |
| D | ✓ | ✓ | ✓ | ✓ |  |  |
| E | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| F | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

각 단계에서 Recall@10, MRR@10, latency, entity leakage, unsupported rate를 함께 측정해야 한다.
정확도만 높이고 latency나 abstention을 악화시키는 변경은 운영 개선으로 간주하지 않는다.

### 7.4 현재 자동 검증 결과

```text
Unit + PostgreSQL integration tests: 237 passed
Branch-aware coverage: 90.47%
Alembic upgrade/downgrade/upgrade: passed
Ruff + format: passed
Mypy strict: 62 source files passed
Agent/tool/routing focused unit tests: 23 passed
Secret baseline scan: passed
Dependency vulnerability audit: no known vulnerabilities found
Compose configuration smoke test: passed
Temporary-database restore drill: passed
```

기존 110개 고정 Korean fixture에서는 Recall@10 1.00, MRR@10 1.00, minimum family Recall@10
1.00, unsupported answer rate 0.00, security violation 0을 기록했다. 이 결과는 deterministic
fixture regression gate이며 실제 사용자 분포에 대한 일반화 성능 주장이 아니다.

실제 백필 데이터와 고정 fixture 사이의 간극을 측정하기 위해, 댓글 344개가 달린 Q&A 게시글
하나에서 부모 질문–자식 답변 gold pair 4개와 query mutation 10개를 구성한 단일 게시글 평가
프로토콜도 추가했다. 이 실험은 answer-source Recall/MRR, atomic claim recall, source containment,
community attribution, future/authority 질문에 대한 abstention을 분리해 측정한다. 초기 open-corpus
retrieval-only 진단에서 무기 강화 query 4개의 gold answer Recall@8과 MRR@8은 각각 0.50이었다.
direct/constraint 질의는 정답 댓글을 1위로 찾았지만 semantic paraphrase와 Korean typo에서 sibling
댓글로 이탈했다. 이는 개선 전 baseline이며 일반화 성능 주장이 아니다. 프로토콜과 failure
가설은 [`docs/single-post-qa-evaluation-protocol.md`](docs/single-post-qa-evaluation-protocol.md)에
기록했다.

2026-09-02 실제 local Qwen으로 SSE usage, hybrid RAG, knowledge graph, Agent local tool 선택과
checkpoint를 검증했다. Discord Gateway에 연결한 뒤 허용 채널의 실제 사용자 멘션이 generated
답변으로 완료되고 community chunk 5개와 graph relation 7개를 보존하는 것도 확인했다. 상세
측정은 [`docs/e2e-verification-2026-09-02.md`](docs/e2e-verification-2026-09-02.md)에 기록했다.
NEXON API key가 없어 실제 character API tool만 E2E 범위에서 제외했다.

통합 `verify_all.sh`의 개별 gate는 위와 같이 검증했으나, 전체 wrapper는 aarch64 Conda 환경의
PyTorch 의존 패키지 `nvidia-cusparselt-cu13 0.8.1`을 `pip check`가 “현재 플랫폼에서 지원되지
않음”으로 판정해 exit 1을 반환한다. `pip-audit`은 알려진 취약점이 없다고 보고했고 ML/Agent
코드·테스트 실패는 아니지만, 환경 lock을 정리하기 전까지 full wrapper 통과로 표기하지 않는다.

---

## 8. 기술적 기여를 AI 연구 관점에서 설명하는 방법

### 8.1 핵심 기여

1. **Hybrid retrieval 연구 구현**  
   BGE-M3 dense retrieval과 pg_trgm lexical retrieval을 RRF로 결합하고, cross-encoder로
   재정렬하는 다단계 검색기를 구현했다.

2. **Entity contamination 완화**  
   질문의 job/boss ontology facet을 검색 filter와 graph merge에 함께 사용해 다른 엔티티의 수치가
   답변에 유입되는 실패를 줄였다.

3. **Canonical knowledge와 비정형 evidence 분리**  
   명칭·계층·identity는 provenance-bearing graph로 보장하고, 커뮤니티 경험은 검색 근거로만
   사용하는 authority-aware context architecture를 설계했다.

4. **생성 후 deterministic verification**  
   약어 확장과 canonical relation 위반을 코드로 검사해 repair 또는 fallback하는 verifier
   architecture를 구현했다.

5. **평가 가능한 Agent state machine**  
   planner, actor, tool registry, schema validator, budget, persistence를 분리해 툴 호출 과정 자체를
   회귀 테스트하고 재개할 수 있게 했다.

### 8.2 한 줄 포트폴리오 요약

> 한국어 게임 도메인의 entity ambiguity와 temporal conflict를 해결하기 위해 BGE-M3 hybrid
> retrieval, cross-encoder reranking, provenance-bearing knowledge graph, deterministic output
> verifier와 resumable tool-using Agent를 설계·구현했다.

### 8.3 이력서용 bullet 예시

- 19K articles / 76K comments 규모의 한국어 domain corpus에 BGE-M3·pgvector HNSW·pg_trgm·RRF·
  BGE reranker를 결합한 hybrid retrieval pipeline 구현
- 217 entities / 755 aliases / 292 relations의 provenance-bearing knowledge graph를 RAG와 결합해
  직업·보스·아이템 identity 및 약어 hallucination을 deterministic하게 검증
- 110개 한국어 retrieval/answer/security fixture와 Recall@10·MRR@10·unsupported rate 기반 release
  gate 설계, 237 tests와 90.47% branch-aware coverage 확보
- NEXON API·community RAG·official patch·knowledge graph를 schema-first tool로 통합하고 PostgreSQL
  checkpoint에서 재개 가능한 bounded Agent 구현

---

## 9. 한계와 후속 연구

### 9.1 AI 연구 과제

- claim extraction과 claim-level entailment/contradiction 판정
- temporal validity interval과 patch supersession graph
- hard negative가 포함된 실제 relevance judgment dataset
- dense-only, hybrid, reranker, KG 단계별 정식 ablation
- calibration curve와 query family별 threshold 최적화
- human factuality/faithfulness 평가와 inter-annotator agreement
- Agent tool selection 정확도 및 호출 비용 benchmark

### 9.2 제품화 과제

- state-changing tool의 Discord confirmation/expiry/replay UI
- Agent run cancellation과 동시 resume lease
- NEXON profile cache의 30일 갱신·삭제 정책
- 실제 NEXON character API tool과 Discord `--agent` E2E 검증
- internal ToolRegistry를 최신 MCP SDK adapter로 노출하는 별도 구현

### 9.3 해석상 주의

- 고정 fixture 1.00은 실제 서비스 정확도 100%를 뜻하지 않는다.
- knowledge graph는 검수된 관계에 강하지만 corpus 전체 claim을 구조화한 것은 아니다.
- community evidence는 경험적 주장이고 공식 사실과 같은 authority를 갖지 않는다.
- MCP는 현재 설계 방법론이며 구현 완료 항목으로 표시하지 않는다.

---

## 10. 주요 코드와 연구 문서

| 목적 | 경로 |
|---|---|
| Hybrid retrieval | `src/maple_chat/retrieval/hybrid.py` |
| Evidence role separation | `src/maple_chat/retrieval/evidence.py` |
| Chunking | `src/maple_chat/indexing/chunking.py` |
| Embedding revision | `src/maple_chat/indexing/embedding.py` |
| Knowledge graph retrieval | `src/maple_chat/knowledge/service.py` |
| Grounded generation/validator | `src/maple_chat/qa/service.py` |
| Agent planner/actor | `src/maple_chat/agent/service.py` |
| Tool contracts | `src/maple_chat/agent/tools.py` |
| Local RAG/graph tools | `src/maple_chat/agent/local_tools.py` |
| Agent persistence | `src/maple_chat/agent/store.py` |
| Evaluation | `src/maple_chat/evaluation.py` |
| 품질 실패 연구 로그 | `docs/service-quality-log.md` |
| RAG·KG 이론 교과서 | `docs/rag-knowledge-graph-textbook.md` |
| Agent 상세 설명 | `docs/nexon-open-api-agent.md` |
| 실제 E2E 검증 | `docs/e2e-verification-2026-09-02.md` |

---

## 11. 재현 명령

실제 LLM 없이 코드·DB·Agent checkpoint까지 검증:

```bash
conda run --no-capture-output -n maple-chat scripts/verify_g002.sh
```

전체 보안·평가·복구 gate:

```bash
conda run --no-capture-output -n maple-chat scripts/verify_all.sh
```

실제 local Qwen과 Discord E2E 결과는 `docs/e2e-verification-2026-09-02.md`에 별도로 보존한다.
