# Maple Chat Agent 확장 로드맵

> 기준일: 2026-09-01. 읽기 전용 NEXON Open API, community/공식 패치 RAG, 지식 그래프를
> 선택하는 복합 Agent와 영속 plan/tool checkpoint, Discord 재개가 구현되었다. 사용자 확인이
> 필요한 상태 변경 툴과 MCP transport는 아직 다음 단계다. 현재 동작과 설정은
> [`nexon-open-api-agent.md`](nexon-open-api-agent.md), AI/Tool 연구 방법론은
> [`../PROJECT_PORTFOLIO.md`](../PROJECT_PORTFOLIO.md)를 참고한다.

| Capability | 상태 |
|---|---|
| 제한된 plan과 ReAct-style tool loop | 구현 |
| NEXON 공식 캐릭터 tool 5개 | 구현 |
| community·공식 패치·지식 그래프 tool 3개 | 구현 |
| `agent_runs` / `agent_tool_calls` checkpoint | 구현 |
| `--agent-resume` requester-scoped 재개 | 구현 |
| 상태 변경 tool confirmation UI | 미구현 |
| 장기 cancellation·동시 실행 lease | 미구현 |
| MCP server adapter | 미구현 |

## 1. 목적

Maple Chat은 hybrid RAG와 지식 그래프를 사용하는 고정형 QA 경로를 유지하면서, 별도의
`--agent` 경로에서 다음 능력을 갖춘 행동형 Agent로 확장되었다. 이 문서는 구현된 읽기 전용
경계와 이후 상태 변경 capability의 로드맵을 함께 정리한다.

- 목표를 여러 단계의 작업으로 분해
- 상황에 맞는 툴 선택
- 검증된 툴 호출 인자 생성
- 툴 실행 결과에 따른 다음 행동 결정
- 여러 단계 작업의 상태 저장과 재개
- 위험하거나 상태를 변경하는 작업의 사용자 확인
- 툴 실패 시 제한된 재시도와 대안 선택

## 2. 대표 사용자 시나리오

가장 자연스러운 첫 시나리오는 **메이플 성장·보스 준비 Agent**다.

예시 질문:

> 제논으로 이번 주 하드 세렌을 준비하려면 무엇을 해야 해?

예상 실행 과정:

1. 목표를 `하드 세렌 주간 준비 계획`으로 해석한다.
2. 캐릭터 정보가 부족하면 필요한 정보만 사용자에게 질문한다.
3. 보스 조건과 직업 관계를 지식 그래프에서 조회한다.
4. 최근 공식 패치노트에서 관련 변경 사항을 확인한다.
5. 커뮤니티 공략을 hybrid RAG로 검색한다.
6. 근거가 충돌하면 공식 정보와 검수된 지식 그래프를 우선한다.
7. 부족한 장비·스펙·연습 항목을 체크리스트로 작성한다.
8. 실행 상태를 저장하고 후속 요청에서 이어서 처리한다.

## 3. Agent 툴 후보

| 툴 | 역할 | 위험도 | 기본 실행 정책 |
|---|---|---:|---|
| `search_community` | 기존 hybrid RAG 검색 | 낮음 | 자동 실행 |
| `query_knowledge_graph` | 직업·보스·아이템 관계 조회 | 낮음 | 자동 실행 |
| `search_patch_notes` | 저장된 공식 패치노트 검색 | 낮음 | 자동 실행 |
| `get_character_profile` | 사용자가 제공·저장한 캐릭터 정보 조회 | 낮음 | 자동 실행 |
| `get_backfill_status` | 게시판별 수집·색인 상태 조회 | 낮음 | 자동 실행 |
| `get_answer_sources` | 과거 답변의 근거 재확인 | 낮음 | 요청자 범위에서 자동 실행 |
| `request_reindex` | 특정 출처 재색인 요청 | 중간 | owner 확인 필요 |
| `start_sync` | 승인된 최신 데이터 수집 시작 | 중간 | 범위 표시 후 확인 필요 |
| `deny_article` | 출처 차단 및 검색 제외 | 높음 | owner 인증과 대상 재확인 필요 |

외부 사이트나 API를 호출하는 툴은 해당 서비스의 약관, robots 정책, API 라이선스 및 별도
승인 범위를 만족할 때만 registry에 등록한다. 기술적으로 접근 가능하다는 사실은 사용 권한을
의미하지 않는다.

## 4. 필요한 Agent 능력

### 4.1 작업 계획 수립

LLM이 즉시 최종 답변을 생성하지 않고 제한된 구조의 계획을 먼저 만든다.

```json
{
  "goal": "하드 세렌 준비",
  "steps": [
    "캐릭터 정보 확인",
    "보스 조건 조회",
    "최근 변경 사항 확인",
    "공략 검색",
    "준비 체크리스트 작성"
  ]
}
```

계획은 최대 단계 수, 전체 실행 시간, 툴 호출 수를 제한해야 한다.

### 4.2 툴 선택

질문의 의도와 현재 상태에 따라 필요한 툴만 선택한다.

- 직업 분류 질문 → `query_knowledge_graph`
- 최근 보스 변경 질문 → `search_patch_notes`
- 실전 공략 질문 → `search_community`
- 운영 진행 상태 질문 → `get_backfill_status`

### 4.3 툴 호출 인자 생성과 검증

모든 툴은 Pydantic 입력 스키마를 제공한다. LLM이 만든 문자열이나 임의 명령을 직접 실행하지
않고, allowlist와 스키마 검증을 통과한 값만 실행한다.

```python
class CommunitySearchInput(BaseModel):
    query: str
    board_ids: tuple[int, ...] = ()
    published_after: datetime | None = None
    top_k: int = Field(default=8, ge=1, le=20)
```

### 4.4 결과 기반 상태 전이

```text
search_community
  ├─ 충분한 근거 → 답변 생성
  ├─ 공식 정보만 존재 → 공식 정보 중심 답변
  ├─ 사용자 정보 부족 → 추가 정보 요청
  └─ 모든 근거 부족 → 범위를 좁혀 달라고 요청
```

LLM이 임의로 모든 전이를 결정하게 두지 않고, 권한·위험도·실행 한도는 결정론적 policy가
강제한다.

### 4.5 상태 저장과 재개

현재 PostgreSQL 구현:

- `agent_runs`: 목표 plan, 요청자 hash, 상태, checkpoint, 호출 budget, 생성·완료·만료 시각
- `agent_tool_calls`: 순서, 툴, 검증된 인자, bounded 결과, 오류
- Discord `--agent-resume <run_id>`: 동일 requester/guild와 TTL 확인 후 저장 observation에서 재개

계획 단계는 현재 `agent_runs.plan` JSON에, 다음 행동은 `agent_runs.checkpoint` JSON에 저장한다.
별도 `agent_steps`, `agent_confirmations` 테이블은 상태 변경 툴을 도입할 때 추가한다. 기존
`crawl_jobs`의 lease, retry, checkpoint, idempotency 설계를 참고하되 사용자 Agent 실행과 수집
작업 큐의 책임은 분리한다.

### 4.6 사용자 확인

- 조회 전용 툴: 자동 실행
- 재색인·동기화: 실행 범위와 부작용을 표시한 뒤 확인
- denylist·삭제성 작업: owner 인증, 대상 재입력, 감사 로그 필수

Discord에서는 확인·취소·재개 버튼을 제공하고, 확인이 만료되거나 요청자가 일치하지 않으면
실행하지 않는다.

### 4.7 실패 시 대안

- 캐릭터 조회 실패 → 저장된 캐시 또는 사용자 입력 요청
- 커뮤니티 근거 부족 → 공식 지식 그래프와 패치노트만 사용
- LLM 장애 → 기존 `retrieval_only` fallback
- 수집 실패 → 허용된 범위 안에서 DB checkpoint부터 재시도
- 툴 인자 검증 실패 → 제한된 수정 시도 후 중단
- 최대 단계 초과 → 현재까지 확인한 사실과 미완료 작업을 요약

## 5. 현재 코드 구조

```text
src/maple_chat/agent/
├── service.py        # planner, bounded decision loop, durable orchestration
├── store.py          # Agent run/tool checkpoint transaction
├── tools.py          # ToolSpec, risk, registry, NEXON tools
└── local_tools.py    # community/official retrieval와 knowledge graph tools
```

연결된 기존 영역:

- `llm/client.py`: local structured generation
- `qa/service.py`: 기존 단일 QA 경로 유지
- `discord/routing.py`: `--agent`, `--agent-resume` routing
- `db/models.py`: `AgentRun`, `AgentToolCall`
- `runtime.py`: NEXON/local Tool registry와 DurableAgentService wiring

향후 상태 변경 툴에는 `agent_confirmations`, Discord 승인·취소 view, 동시 실행 lease가 추가로
필요하다.

## 6. 단계별 확장 순서

1. **읽기 전용 패치 영향 분석 Agent — 기반 구현 완료**
   - 지식 그래프, 저장된 패치노트, RAG만 사용한다.
   - 외부 상태를 변경하지 않아 초기 검증에 적합하다.
2. **캐릭터 기반 성장·보스 준비 Agent — MVP 완료**
   - 제한된 계획과 실행 checkpoint를 저장·재개한다.
   - 명시적 추가 질문 UI와 장기 profile cache는 후속 과제다.
3. **운영자 Agent — 미구현**
   - 백필 상태, 재색인, 승인된 동기화를 툴로 제공한다.
   - Discord 확인과 owner 권한을 강제한다.
4. **공통 Tool SDK — 내부 registry 구현, MCP adapter 미구현**
   - `ToolSpec`, 입력·출력 schema, risk level, timeout, version, idempotency를 표준화한다.

## 7. 핵심 원칙

- 프레임워크 도입 자체보다 `계획 → 툴 선택 → 실행 → 평가 → 승인·재시도 → 상태 저장`의
  실제 동작과 감사 가능성을 우선한다.
- LLM은 계획과 후보 행동을 제안하지만 권한, 도메인 범위, 위험도, 실행 한도는 코드가 강제한다.
- 읽기 전용 툴부터 시작하고 상태 변경 툴에는 사용자 확인과 감사 로그를 적용한다.
- 모든 외부 데이터 소스는 별도의 이용 권한과 라이선스 근거가 있어야 한다.
- Agent의 최종 답변뿐 아니라 계획, 툴 호출, 전이, fallback을 평가 데이터로 남긴다.
