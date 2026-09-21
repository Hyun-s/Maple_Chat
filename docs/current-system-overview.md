# Maple Chat 현재 시스템 설명서

기준일: 2026-09-02 KST  
대상 저장소: `/home/hyuns/project/Maple_Chat`

이 문서는 현재 코드와 실행 중인 PostgreSQL을 기준으로 작성한다. 계획만 존재하는 기능은 구현된
기능과 분리해 표시한다.

전체 이론과 전문가 설계 과정은
[RAG·데이터베이스·지식 그래프 시스템 교과서](rag-knowledge-graph-textbook.md)에서 먼저 학습할
수 있다.

## 1. 한 문장 설명

Maple Chat은 권리 승인을 거쳐 수집한 Inven 게시글·댓글과 최근 1년 넥슨 공식 패치노트의 hybrid
RAG, 넥슨 공식 문서 및 운영자 검수 catalog를 출처로 하는 도메인 지식 그래프를 결합해 선택된
근거만 로컬 Qwen 모델에 전달하며, 공식 캐릭터 API와 내부 근거 검색기를 선택하는 재개 가능한
Tool-using Agent를 제공하는 private Discord AI 시스템이다.

## 2. 구현 상태 요약

| 영역 | 상태 | 설명 |
|---|---|---|
| Inven 게시글·댓글 수집 | 구현·운영 중 | 승인된 7개 게시판 대상 |
| 개인정보 정제 | 구현 | 저장·임베딩 전에 적용 |
| PostgreSQL 원문 저장 | 구현·운영 중 | 글, 댓글, 상태, 추천 수, 조회 수 등 |
| 구조 기반 청크 생성 | 구현 | `structure-v1`, 550/700/80 토큰 정책 |
| BGE-M3 임베딩 | 구현·운영 중 | CUDA, 1024차원 |
| pgvector dense 검색 | 구현·운영 중 | cosine + HNSW |
| pg_trgm lexical 검색 | 구현·운영 중 | GIN trigram 인덱스 |
| RRF 결합 | 구현 | dense/lexical 순위 결합 + 제한된 품질 boost |
| BGE reranker | 구현·운영 중 | `BAAI/bge-reranker-v2-m3` |
| 근거 기반 로컬 생성 | 구현·E2E 검증 | 2026-09-02 local Qwen SSE와 Discord 생성 답변 확인 |
| 답변 처리 지표 | 구현 | 입력/출력/총 토큰, 입력·출력 TPS, 전체 처리 시간 표시·JSON 로그 |
| `--no-rag` | 구현 | 검색을 완전히 건너뛰는 direct 모드 |
| RAG-Anything식 query mode | 구현 | `local/global/hybrid/naive/mix/bypass`, 기본 `mix` |
| 답변별 출처 보존 | 구현 | `answers`와 `answer_sources` |
| 일일 증분 수집 | 구현·스케줄 실행 중 | 매일 05:00 KST, ID 중복 제거 + 최신/과거 백필 |
| 공식 패치노트 수집 | 구현·스케줄 실행 중 | 최근 365일, 매주 목요일 05:00 KST 증분 확인 |
| 과거 백필 | 구현 | 10페이지 단위 round-robin, 기본 목표 200페이지 |
| OCR 코드 | 구현 | 현재 운영에서는 비활성, 저장 데이터 0건 |
| 공식 직업 지식 RAG | 구현 | 넥슨 공식 직업소개 기반 48개 직업·5개 전투 계열 |
| 아이템·게임·콘텐츠 약어 RAG | 구현 | 검수 약어 13개와 canonical 명칭 `abbreviation_of` 관계 |
| 보스·난이도 identity RAG | 구현 | 공식 가이드 33개 보스·79개 난이도 변형과 시즌 보스 메이린 |
| 콘텐츠·장비 분류 RAG | 구현 | 유챔→유니온 챔피언, 데스티니→무기 또는 해방 퀘스트 보스 모드 |
| 지식 그래프 | 구현 | entity/alias/relation, exact traversal, 답변별 relation provenance |
| 명칭 drift 감사 | 구현·일일 실행 | 활성 corpus와 canonical 명칭 비교, JSONL 누적, 자동 승격 금지 |
| Q&A 근거 역할 분리 | 구현 | 질문 article 제외, comment의 댓글 주장만 생성 근거로 사용 |
| 약어 생성 검증 | 구현 | canonical relation 불일치 시 재생성, 재실패 시 graph fallback |
| 복합 Tool-using Agent | 구현 | NEXON API 5개 + community/공식 패치/graph tool 3개 |
| Agent 영속 실행·재개 | 구현 | plan/tool checkpoint, requester-scoped `--agent-resume` |
| MCP server transport | 미구현 | 내부 schema-first registry만 구현, adapter는 후속 과제 |
| 주장 추출·상충 판정 | 미구현 | 현재는 생성 프롬프트가 상충 공개를 지시할 뿐 |

### 2.1 현재 연구 corpus snapshot

2026-09-01 로컬 PostgreSQL 기준 게시글 19,192건, 댓글 76,803건, 활성 chunk/embedding
98,801건이며 활성 embedding revision은 `bge-m3-5617a9f61b02` 1024차원이다. 이 수치는 운영
데이터 규모이며 고정 evaluation fixture의 성능과는 별개다.

## 3. 전체 데이터 흐름

```text
Inven 목록 페이지
  → 게시글 URL 발견
  → 게시글 HTML + 공개 댓글 JSON 수집
  → 본문/댓글 파싱
  → 개인정보 정제 및 닉네임 HMAC 해시
  → PostgreSQL articles/comments 저장
  → 구조 기반 chunk 생성
  → BGE-M3 embedding 생성
  → chunks/chunk_embeddings 저장

Nexon 공식 업데이트 목록(최근 365일)
  → 목록의 ID·제목·게시/수정 시각 비교
  → 신규·수정 상세 페이지만 GET
  → synthetic board -1001의 articles에 멱등 저장
  → source_authority=official metadata로 청크·임베딩 생성
  → 365일 경계를 벗어난 공식 청크 비활성화

Discord 질문 (`mix` 기본)
  → 접근 정책·rate limit 검사
  ├─ 질문 BGE-M3 embedding → dense/lexical/RRF/reranker → 커뮤니티 근거
  ├─ 질문 alias entity linking → mode별 1-hop/2-hop graph traversal
  └─ `mix` 검색 근거 본문 alias entity linking → graph enrichment
  → 공식 관계 JSON + 비신뢰 커뮤니티 근거 JSON + 질문을 로컬 Qwen에 전달
  → 등록 약어의 생성 명칭을 canonical relation과 검증
  → stream usage와 TTFT/완료 시각으로 토큰·TPS 집계
  → 답변 하단에 처리 지표 표시 + 개인정보 없는 JSON 완료 로그
  → Discord 답변
  → answers/answer_sources/answer_knowledge_sources에 실제 사용 근거 저장

Discord `--agent` 질문
  → 목표와 1~6개 단계 계획
  → agent_runs에 plan/checkpoint 저장
  → NEXON API / community / official patch / graph tool 중 하나 선택
  → Pydantic schema·risk·반복 호출·budget 검사
  → bounded result를 agent_tool_calls에 저장
  → 다음 tool 또는 finish 결정
  → 장애 시 requester-scoped `--agent-resume`으로 저장 observation부터 재개
```

## 4. 수집 계층

### 4.1 수집 대상

`src/maple_chat/scheduler/planner.py`의 승인된 게시판은 다음 7개다.

| board_id | 게시판 |
|---:|---|
| 2294 | 전사 |
| 2295 | 마법사 |
| 2296 | 궁수 |
| 2297 | 도적 |
| 2298 | 해적 |
| 2300 | 질문과 답변 |
| 2304 | 팁과 노하우 |

### 4.2 저장하는 게시글 정보

- 게시판 ID, 원격 게시글 ID, 카테고리
- URL, 제목, 정제 본문
- 작성자 원문이 아닌 salted HMAC 해시
- 게시 시각, 최초/최근 관측 시각
- 조회 수, 추천 수, 댓글 수
- 콘텐츠 SHA-256 hash
- 처리 상태: discovered부터 indexed/deleted/denied/failed까지

추천 수는 `articles.recommendation_count`에 저장되고 검색 품질 boost에도 제한적으로 사용된다.

### 4.3 댓글

- Inven의 공개 read-only 댓글 JSON endpoint를 사용한다.
- 댓글 ID, 부모 댓글 ID, 정제 본문, 점수, 시간, 작성자 해시를 저장한다.
- 댓글 트리의 orphan/cycle을 검사하며 잘못된 구조는 parser drift로 중단한다.
- 색인 시 글 본문 앞 800자와 댓글을 합쳐 질문 문맥이 포함된 댓글 문서를 만든다.

### 4.4 정제와 안전 경계

- 저장과 임베딩 전에 이메일·전화번호 등 정해진 PII 패턴을 마스킹한다.
- 닉네임은 원문을 저장하지 않고 salt가 포함된 HMAC-SHA256으로 저장한다.
- CAPTCHA, 반복 403/429, parser drift 발생 시 우회하지 않고 circuit을 연다.
- 허용 호스트와 경로, 승인 파일의 scope를 매 요청 전에 검증한다.
- 이미지 재귀 fetch와 위험한 미디어는 차단한다.

### 4.5 수집 스케줄

#### 일일 ID 기반 증분 수집

- `scripts/daily_sync_loop.sh`
- 기본 시각: 매일 05:00 KST
- 모든 게시판의 신규 글을 먼저 처리한 뒤 과거 백필 단계로 이동
- 최신 페이지부터 전체가 기존 ID인 페이지가 나올 때까지 미저장 게시글 ID를 모두 수집
- `(board_id, remote_article_id)` 유니크 키로 이미 저장된 글의 상세·댓글 요청을 생략
- 게시판별 과거 1페이지를 `next_backfill_page`부터 이어서 진행
- 수집 후 vLLM GPU handoff를 거쳐 변경된 source만 색인하고 read-only `audit-knowledge` 실행

#### 과거 백필

- `scripts/run_full_backfill.sh`
- 게시판별 10페이지씩 round-robin
- 기본 목표 200페이지
- DB checkpoint부터 재개
- 일일 수집과 동일한 `flock` 파일을 사용해 동시 크롤링을 막음

#### 공식 패치노트

- `scripts/weekly_patch_notes_loop.sh`, `scripts/sync_patch_notes.sh`
- 범위: 실행일 기준 최근 365일
- 기본 시각: 매주 목요일 05:00 KST
- 목록은 경계 날짜를 만날 때까지만 읽고, 상세 본문은 신규 ID 또는 수정 시각/제목 변경 시에만 수집
- 공식 Nexon HTTPS host와 `/News/Update` 경로만 허용하는 별도 GET-only client 사용
- Inven 승인 scope와 lock을 넓히지 않으며 `/tmp/maple-chat-patch-notes.lock`으로 실행 중복 방지
- 오래된 row는 보존하되 검색 청크와 임베딩을 inactive로 전환

현재 운영 프로세스에는 Discord 봇, 일일 Inven scheduler, 주간 공식 패치노트 scheduler가 함께
실행되며, 별도 과거 백필 프로세스는 실행 중이지 않다.

## 5. PostgreSQL 데이터베이스

### 5.1 엔진과 확장

- PostgreSQL 16.10
- pgvector 0.8.0
- pg_trgm 1.6
- 현재 Alembic revision: `g010_agent_runs`
- 로컬 개발 배치는 `127.0.0.1:55432`에만 bind한다.

### 5.2 수집 원본 영역

| 테이블 | 역할 |
|---|---|
| `boards` | 게시판 설정과 수집 checkpoint |
| `categories` | 게시판별 원격 카테고리 |
| `articles` | 정제된 게시글과 조회/추천/댓글 수 |
| `comments` | 정제된 댓글과 부모 관계 |
| `media_assets` | 이미지 메타데이터 및 OCR 결과 |

### 5.3 RAG 색인 영역

| 테이블 | 역할 |
|---|---|
| `chunks` | 검색 단위 텍스트, metadata, revision, active 상태 |
| `embedding_revisions` | embedding 모델과 immutable commit, 차원, 활성 revision |
| `chunk_embeddings` | 청크별 1024차원 pgvector |

중요한 제약:

- 활성 embedding revision은 하나만 존재할 수 있다.
- 동일 source/ordinal/revision에서 활성 청크는 하나만 존재할 수 있다.
- 글이 수정되면 이전 청크는 삭제하지 않고 inactive로 남긴다.
- 검색은 active 청크와 active embedding만 사용한다.
- 과거 답변이 참조한 청크는 inactive가 되어도 감사용 관계가 유지된다.

### 5.4 수집 실행·큐 영역

| 테이블 | 역할 |
|---|---|
| `crawl_runs` | 일일/백필 실행 단위와 counters/state |
| `crawl_jobs` | lease, retry, dead-letter를 지원하는 PostgreSQL job queue |
| `outbox_events` | 글·댓글 변경 이벤트의 transactional outbox |

큐는 `FOR UPDATE SKIP LOCKED` lease를 지원하고, 실패 시 exponential backoff 후 최대 시도 횟수를
넘으면 dead letter로 전환한다. 다만 현재 로컬 실운영의 일일 수집은 이 durable queue worker가
아니라 shell loop가 `sync-live` CLI를 직접 호출하는 경로다.

### 5.5 공식 지식 그래프 영역

| 테이블 | 역할 |
|---|---|
| `knowledge_sources` | 공식 URL, publisher, version, 확인 시각, catalog hash |
| `knowledge_entities` | taxonomy/job_family/job/item/game_term/abbreviation/effect canonical node |
| `knowledge_aliases` | canonical name과 약칭의 entity 연결 |
| `knowledge_relations` | 계층·약어·효과·보스 variant·장비 성장 edge와 provenance |
| `answer_knowledge_sources` | 답변에 실제 사용된 graph relation |

현재 내장 그래프는 직업·아이템 명칭·게임 용어·보스·콘텐츠·장비 성장 영역을 합쳐 active source 8개,
entity 217개, 검색 alias 755개, relation 292개로 구성된다. `데스티니`는 의도적으로 무기와 보스 콘텐츠 모드 두 entity에 연결되는 다의어이며 질문 문맥으로 구분한다. 직업 영역에서 제논은 도적과 해적
두 계열에 속한다. 명칭
영역에는 검수 약어 13개가 canonical target과 `abbreviation_of`로 연결되며, 여기에는
`아크이노→아크 이노센트 주문서`, `추옵→추가 옵션`이 포함된다. 상세 이론과 traversal 계약은
[`knowledge-graph.md`](knowledge-graph.md)를 참고한다.

### 5.6 답변·운영·삭제 영역

| 테이블 | 역할 |
|---|---|
| `answers` | 질문 hash, Discord 위치, 생성 모드, 만료 시각 |
| `answer_sources` | 실제 답변에 사용된 community chunk와 검색/rerank 점수, 순위 |
| `answer_knowledge_sources` | 실제 답변에 사용된 공식 graph relation과 순위 |
| `guild_settings` | 허용 Discord 채널과 owner |
| `feedback` | 1~5점 평가와 사유 |
| `denylist` | 다시 수집하면 안 되는 source |
| `tombstones` | 삭제 관측 기록 |
| `audit_events` | 관리자 작업 감사 로그 |
| `agent_runs` | Agent goal plan, 상태, checkpoint, 호출 budget과 TTL |
| `agent_tool_calls` | 검증된 툴 인자와 bounded result의 순서별 실행 기록 |

Discord 출처 버튼은 기본 7일 TTL 동안 요청자 본인에게만 원 출처를 보여준다. 삭제된 source는
기존 감사 관계는 남기되 URL을 다시 노출하지 않는다.

## 6. 청크와 임베딩

### 6.1 청크 구성

`src/maple_chat/indexing/chunking.py`의 기본값:

- revision: `structure-v1`
- 목표: 550 tokens
- 최대: 700 tokens
- overlap: 80 tokens

모든 청크 앞에는 다음 header가 붙는다.

```text
제목: ...
게시판: ...
분류: ...
작성일: ...
```

`chunk_id`는 다음 값의 SHA-256 기반 deterministic ID다.

```text
source_key + ordinal + chunking_revision + chunk_text
```

같은 내용은 같은 ID를 만들고, 내용이 바뀌면 새 ID를 만든다.

### 6.2 임베딩

- 모델: `BAAI/bge-m3`
- 차원: 1024
- normalize: true
- 거리: cosine
- 장치: CUDA
- 현재 활성 revision: `bge-m3-5617a9f61b02`

모델 이름뿐 아니라 immutable commit이 DB revision과 맞지 않으면 색인을 거부한다.

## 7. 검색 방식

### 7.1 Dense 검색

1. 질문을 BGE-M3 1024차원 vector로 변환한다.
2. pgvector cosine distance로 가까운 활성 청크 50개를 조회한다.
3. active embedding revision만 검색한다.

### 7.2 Lexical 검색

1. `pg_trgm`의 `similarity(chunk.text, query)`를 사용한다.
2. similarity가 0보다 큰 활성 청크 상위 50개를 조회한다.
3. `ix_chunks_text_trgm` GIN 인덱스를 사용한다.

### 7.3 RRF 결합

Dense와 lexical 순위를 `1 / (60 + rank)`로 합친다. 그 후 다음 metadata를 이용해 총 0.12를
넘지 않는 제한된 boost를 추가한다.

- 추천 수: 최대 0.04
- 조회 수: 최대 0.025
- 팁 카테고리: +0.025
- 댓글 Q&A branch: +0.015
- 최신성: 최대 0.03, 365일 decay

이 boost는 관련성을 대체하지 않고 동점에 가까운 후보의 품질을 보조하는 용도다.

### 7.4 Reranking과 최종 근거

- RRF 상위 30개를 `BAAI/bge-reranker-v2-m3`에 전달한다.
- rerank 점수 0.15 미만은 제거한다.
- 같은 `source_key`에서는 하나만 선택한다.
- 최종 근거는 최대 8개다.
- 하나도 남지 않으면 `insufficient_evidence`로 답변 생성을 거절한다.

## 8. 답변 생성

### 8.1 RAG 모드

RAG-Anything/LightRAG의 공개 query mode를 현재 저장소에 맞게 적용한다. `local`은 질문 entity의
검수 관계만, `global`은 최대 2-hop graph만, `hybrid`는 global graph와 community evidence를,
`naive`는 graph anchor 없는 community evidence만 사용한다. 기본 `mix`는 기존 1-hop graph와
community evidence에 검색 근거 본문의 entity linking을 더한다. `bypass`와 기존 `--no-rag`는
검색을 모두 건너뛴다. 상세 매핑은
[`rag-anything-adaptation.md`](rag-anything-adaptation.md)에 있다.

선택된 청크와 직업 관계는 서로 다른 JSON 배열로 직렬화한다.

```json
{
  "evidence_id": "chunk id",
  "source_key": "article/comment source key",
  "text": "청크 본문",
  "metadata": {
    "title": "...",
    "board": "...",
    "category": "...",
    "published_at": "...",
    "url": "..."
  }
}
```

`canonical_knowledge_json`은 공식 직업 계층에 우선 사용하고, `untrusted_evidence_json`의
커뮤니티 내용은 데이터로만 취급한다. 커뮤니티 근거가 공식 직업 관계와 충돌하면 직업명·계층은
그래프를 따른다. 그래프에 없는 공략·수치는 기존 근거 밖에서 단정하지 않는다.

현재 답변 스타일:

- 최대 12문장
- 핵심 결론 우선
- 내용이 둘 이상이면 굵은 소제목과 bullet
- 절차는 번호 목록
- 문단당 최대 2문장

### 8.2 로컬 모델

- endpoint: `http://127.0.0.1:8001/v1`
- served model: `local-coder`
- 실제 모델 배치: Qwen3.8-27B 로컬 vLLM
- timeout: 180초
- 생성 max tokens: 1024
- temperature: 0.1
- thinking: disabled
- 클라우드 host나 URL credential은 코드에서 거부한다.

### 8.3 실패 모드

| 상황 | answer mode | 동작 |
|---|---|---|
| 충분한 근거 + 생성 성공 | `generated` | 근거 기반 답변 |
| 사용자가 `--no-rag` | `direct` | 검색 없이 로컬 모델 답변 |
| community와 graph 근거 모두 부족 | `insufficient_evidence` | 질문을 구체화하라는 거절 |
| 근거는 있으나 LLM 실패 | `retrieval_only` | 검색 결과 또는 정확 graph relation 목록 |

## 9. Discord 계층

- 단일 private guild와 허용 채널만 처리한다.
- 봇에 대한 직접 mention만 허용한다.
- webhook, bot 메시지, 위험 URL scheme, 과도한 mention을 거부한다.
- 질문 길이 최대 1800자다.
- `--rag-mode <mode>`는 질문 맨 앞에서 여섯 query mode 중 하나를 선택한다.
- `--no-rag`는 하위 호환 옵션이며 `bypass`로 정규화된다.
- 사용자별 분당 5회, 채널별 분당 20회 제한이다.
- 생성은 semaphore 1로 직렬화한다.
- 대기열 최대 20개다.
- Discord 2,000자 제한에 맞춰 기본 1,900자 단위로 답변을 나눈다.
- `@everyone`, `@here`, 사용자 mention은 neutralize한다.

## 10. 현재 실제 데이터 스냅샷

2026-08-25 확인값:

| 항목 | 수량 |
|---|---:|
| 게시판/공식 source board | 8 (Inven 7 + Nexon 공식 1) |
| 저장된 게시글 | 17,777 (공식 패치노트 30 포함) |
| indexed 게시글 | 17,776 |
| 미색인 게시글 | 1 |
| 저장된 댓글 | 70,386 |
| indexed 댓글 | 70,373 |
| 미색인 댓글 | 13 |
| 전체 청크 | 90,850 |
| 활성 청크 | 90,848 |
| 활성 embedding | 90,848 |
| 이미지/OCR asset | 0 |
| 누적 답변 | 38 |

답변 모드 분포:

- generated: 27
- retrieval_only: 7
- direct: 3
- insufficient_evidence: 1

중요: **DB에 저장된 모든 글과 댓글이 검색 가능한 것은 아니다.** 검색 가능한 범위는 active chunk와
active embedding이 생성된 indexed 데이터다. 현재 미색인 상태는 게시글 1건과 댓글 13건이다.
공식 패치노트 초기 백필은 30건·555개 벡터로 완료됐다.

### 게시판 checkpoint

| 게시판 | last successful page | next page |
|---|---:|---:|
| 전사 | 211 | 212 |
| 마법사 | 40 | 41 |
| 궁수 | 40 | 41 |
| 도적 | 40 | 41 |
| 해적 | 40 | 41 |
| 질문과 답변 | 40 | 41 |
| 팁과 노하우 | 35 | 36 |

현재 백필 script의 기본 목표는 200페이지다. 전사 checkpoint는 과거 실행으로 이미 211까지
진행됐으므로 추가 수집 대상에서 제외되고, 나머지 게시판은 각 checkpoint부터 200페이지까지
10페이지 round-robin으로 진행한다.

## 11. 지식 그래프의 현재 상태

### 11.1 구현된 범위

- `knowledge_sources`: 공식 URL, publisher, version, checked_at, content hash
- `knowledge_entities`: 직업·아이템·게임 용어·보스·콘텐츠·장비 성장 entity 총 217개
- `knowledge_aliases`: canonical surface와 검색 별칭 총 755개
- `knowledge_relations`: 계층·약어·효과·보스 variant·난이도·장비 성장 relation 총 292개
- `answer_knowledge_sources`: 답변에 사용한 relation provenance
- DB alias 기반 entity linking
- job/job_family/taxonomy 타입별 exact relation traversal
- 질문과 claim-bearing evidence 양쪽을 조회하는 2단계 graph enrichment
- Q&A article/question과 comment/answer의 구조적 claim role 분리
- 생성된 약어 확장과 `abbreviation_of` target의 일치 검증
- `아크 이노센트 주문서`의 공식 초기화 효과 `has_effect` traversal
- 공식 graph JSON과 비신뢰 community evidence JSON의 분리
- 데스티니 무기와 데스티니 보스 모드의 다의어 linking, 세렌·칼로스·카링 variant traversal
- 생성 stream usage, TTFT, 완료 시각을 이용한 답변별 token/TPS/전체 시간 집계

현재 `메카닉 → 해적`, `렌 → 전사`, `소울마스터 → 전사`, `제논 → 도적/해적`을 PostgreSQL
relation으로 직접 조회할 수 있다. 공식 지식은 별도 chunk/embedding을 만들지 않는다. 관계가
명시된 작은 정형 그래프이므로 vector 근사 검색보다 alias linking과 정확 traversal을 사용한다.

### 11.2 이전 휴리스틱과 현재 방식의 차이

이전에 Python 상수·정규식·출력 치환으로 알려진 오답만 고치는 실험은 전부 제거했다. 현재 방식은
질문/답변 문자열을 사례별로 치환하지 않는다. 버전된 JSON catalog를 검증해 DB
entity/relation으로 동기화하고, 어떤 질문이든 같은 entity linking, traversal, relation 기반
생성 검증 알고리즘을 적용한다.

### 11.3 아직 구현되지 않은 범위

- catalog에 아직 등록되지 않은 게임 용어
- 모험가·레지스탕스 등 출신 분류 축
- 장비 제작 절차와 보스 최소컷 주장의 구조화·상충 판정
- 공식 페이지 자동 변경 감지 및 승인 workflow
- 생성 문장별 relation/chunk citation validator

상세 이론, schema, traversal, 갱신 절차는 [`knowledge-graph.md`](knowledge-graph.md)에 있다.

## 12. 현재 시스템이 잘하는 것과 못하는 것

### 잘하는 것

- 수집된 커뮤니티 글에서 질문과 의미·문자열이 가까운 근거 탐색
- 추천/조회/최신성 등을 제한적으로 반영한 후보 정렬
- 로컬 모델에 선택된 근거만 전달
- 답변이 실제 어떤 청크를 사용했는지 DB에 보존
- 직업 계층을 공식 graph relation으로 보장하고 답변별 relation을 보존
- 약칭을 버전된 alias에서 canonical 직업 entity로 연결
- 검수된 아이템 약어를 공식 item entity로 연결
- 검수된 `추옵`을 공식 `추가 옵션` game_term entity로 연결
- 질문에 없고 검색 댓글에만 있는 등록 약어도 canonical relation으로 보강
- Q&A 질문자가 제시한 순서를 댓글 답변 주장으로 오인하지 않도록 분리
- 근거가 없거나 LLM이 실패할 때 fail-closed fallback
- 글 수정 후 최신 청크만 검색하면서 과거 답변 감사 관계 보존
- 생성 답변에서 입력·출력·총 token과 유효 TPS·전체 처리 시간을 사용자와 운영 로그에 제공

### 아직 못하는 것

- catalog에 아직 없는 공식 용어를 canonical entity로 연결
- 서로 다른 글의 주장을 구조적으로 비교해 상충 판정
- 여러 경험담을 조건별로 정규화해 평균/최소컷 계산
- 의미가 같은 과거 질문의 답변과 현재 답변 일관성 비교
- 수집된 미색인 데이터 전체를 즉시 검색

## 13. 다른 사람에게 설명할 때 사용할 답변

### “어떤 RAG인가?”

> Inven 글과 댓글을 정제·청크화해 BGE-M3로 임베딩하고, pgvector dense 검색과 pg_trgm
> 문자열 검색을 RRF로 합친 다음 BGE reranker로 최종 근거를 고르는 하이브리드 RAG다. 최종
> 근거 최대 8개를 고른다. 동시에 공식 직업 alias를 entity에 연결해 relation을 정확 조회하고,
> 공식 graph와 community evidence를 구분해 로컬 Qwen에 전달하는 Knowledge-Graph-Augmented RAG다.

### “ChatGPT 같은 클라우드 모델을 쓰나?”

> 아니다. 생성 모델은 localhost의 OpenAI 호환 vLLM endpoint에서 실행되는 Qwen이고, 코드가
> 외부 host나 URL credential을 거부한다.

### “모든 수집 데이터가 바로 답변에 반영되나?”

> 아니다. DB 저장 후 청크와 embedding까지 생성되어 indexed 상태가 된 데이터만 검색된다. 현재
> 저장 데이터 중 일부는 아직 미색인 상태다.

### “추천 수도 저장하고 활용하나?”

> 저장한다. 게시글의 추천 수와 조회 수를 metadata로 청크에 전달하고, 검색 후보의 작은 품질
> boost에 사용한다. 다만 관련성 점수를 압도하지 않도록 전체 boost를 제한한다.

### “출처를 추적할 수 있나?”

> 가능하다. 커뮤니티 chunk는 `answer_sources`에, 공식 relation은
> `answer_knowledge_sources`에 저장한다. Discord 출처 버튼은 요청자 본인에게만 일정 기간
> 제공된다.

### “지식 그래프가 구현됐나?”

> 직업 전투 계열, 검수 아이템·게임 용어, 공식 보스와 난이도, 일부 콘텐츠·장비 성장 영역에
> 구현됐다. 48개 직업·5개 상위 계열, 상시 보스 33개·난이도 변형 79개와 데스티니 결전 보스
> variant 등을 PostgreSQL relation으로 저장하고 질문 및 검색 댓글에서 exact traversal한다.
> 등록되지 않은 모든 게임 용어와 공략 주장 전체를 포괄하는 범용 지식 그래프는 아니다.

### “직업 계층 오류는 지금 어떻게 막나?”

> 질문에 등장한 직업 alias를 공식 entity에 연결하고 `is_a` relation을 조회한다. 생성 모델에는
> 이 graph fact를 community 글보다 우선하도록 분리 제공한다. 사례별 정규식이나 출력 치환은 없다.

### “답변 속도와 토큰 사용량은 어떻게 보나?”

> 모델이 생성한 답변 하단에 입력·출력·총 token, 입력 TPS(TTFT 기반), 출력 TPS와 QA 전체 시간이
> 표시된다. 같은 숫자는 `discord_answer_completed` JSON 로그에도 남는다. 입력 TPS는 queue와
> HTTP 시간을 포함한 유효 처리율이므로 GPU kernel만의 순수 성능 수치는 아니다.

## 14. 주요 코드 위치

- DB 모델: `src/maple_chat/db/models.py`
- migrations: `migrations/versions/`
- 수집: `src/maple_chat/crawler/`
- 스케줄: `src/maple_chat/scheduler/planner.py`, `scripts/daily_sync_loop.sh`
- 청크: `src/maple_chat/indexing/chunking.py`
- embedding 저장: `src/maple_chat/indexing/embedding.py`
- 색인 서비스: `src/maple_chat/indexing/service.py`
- hybrid 검색: `src/maple_chat/retrieval/hybrid.py`
- 지식 catalog/조회: `src/maple_chat/knowledge/`
- 지식 그래프 상세 문서: `docs/knowledge-graph.md`
- RAG·지식 그래프 시스템 교과서: `docs/rag-knowledge-graph-textbook.md`
- QA transaction/prompt: `src/maple_chat/qa/service.py`
- 로컬 LLM client: `src/maple_chat/llm/client.py`
- Discord routing/runtime: `src/maple_chat/discord/`
- 실제 runtime wiring: `src/maple_chat/runtime.py`
- 품질 실패 로그: `docs/service-quality-log.md`
