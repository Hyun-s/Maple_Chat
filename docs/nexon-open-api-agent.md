# NEXON Open API 캐릭터 Agent

## 현재 구현 상태

Maple Chat에는 공식 NEXON Open API, 저장된 community/공식 패치 RAG와 검수된 지식 그래프를
결합하는 읽기 전용 캐릭터 분석 Agent가 연결되어 있다. 기존 QA 경로는 그대로 유지되며,
Discord에서 `--agent` 옵션을 사용한 요청만 Agent 경로로 전달된다.

```text
@MapleChat --agent 캐릭터 아델유저의 보스 준비 관점에서 스탯과 장비를 요약해줘
```

현재 Agent는 먼저 목표와 1~6개 단계를 계획하고, 한 번에 하나의 툴을 선택해 결과를 평가한 다음
추가 조회 또는 최종 답변을 결정한다. 정해진 순서로 모든 API를 무조건 호출하는 파이프라인이
아니다. 계획과 각 툴 호출은 PostgreSQL에 저장되며 실패한 실행은 저장된 observation에서 재개한다.

```text
사용자 질문
  → 로컬 LLM의 제한된 계획 JSON 생성
  → agent_runs에 계획과 checkpoint 저장
  → 로컬 LLM의 다음 행동 JSON 생성
  → 툴 이름·인자 schema 검증
  → NEXON API / RAG / 지식 그래프 읽기 전용 툴 호출
  → bounded result를 agent_tool_calls에 저장
  → 로컬 LLM이 다음 행동 또는 종료 결정
  → 한국어 답변, 실행 ID와 출처 표기
```

## 제공 툴

| 툴 | 공식 API | 용도 |
|---|---|---|
| `resolve_character` | `/maplestory/v1/id` | 캐릭터명으로 최신 `ocid` 조회 |
| `get_character_basic` | `/maplestory/v1/character/basic` | 월드, 직업, 레벨, 길드 등 조회 |
| `get_character_stat` | `/maplestory/v1/character/stat` | 종합 능력치 조회 |
| `get_character_equipment` | `/maplestory/v1/character/item-equipment` | 장착 장비와 잠재능력 조회 |
| `get_union` | `/maplestory/v1/user/union` | 유니온 레벨과 등급 조회 |

내부 근거 툴:

| 툴 | 데이터 계층 | 용도 |
|---|---|---|
| `search_community` | hybrid RAG | 저장·색인된 커뮤니티 공략과 경험 근거 검색 |
| `search_official_updates` | official RAG | 저장·색인된 NEXON 공식 패치·업데이트 검색 |
| `query_knowledge_graph` | exact graph traversal | 직업·보스·아이템·콘텐츠의 검수 관계 조회 |

장비 API 응답은 LLM 문맥을 과도하게 사용하지 않도록 장비명, 부위, 스타포스, 잠재능력 등 분석에
필요한 필드만 요약한다. 캐릭터 이미지와 아이템 아이콘 URL은 Agent 문맥에 전달하지 않는다.

## 설정

NEXON Open API에서 개발 애플리케이션을 등록하고 발급받은 키를 환경 변수로 설정한다.

```dotenv
NEXON_API_KEY=
AGENT_MAX_TOOL_CALLS=4
NEXON_ANALYTICS_SCRIPT=
```

- API 키가 없으면 기존 RAG 봇은 정상 동작하지만 `--agent` 요청에는 미설정 안내를 반환한다.
- API 키는 로그, 설정 요약, 객체 표현에 출력하지 않는다.
- `AGENT_MAX_TOOL_CALLS`는 1~8 범위이며 기본값은 4다.
- API 원문 응답은 저장하지 않는다. Agent 재개에 필요한 최소 필드로 축약한 툴 결과만
  `agent_tool_calls`에 저장하고 재개 가능 TTL을 7일로 제한한다. 만료 row의 물리 삭제 정책은
  후속 운영 과제다.

`NEXON_ANALYTICS_SCRIPT`는 API 호출 인증값이 아니라 공개 웹 서비스에 삽입하는 Analytics
스크립트다. `scripts/configure_nexon_analytics.py`는 NEXON 공식 HTTPS 호스트, Analytics
경로, 단일 `app_id`, `async` 속성을 검증한 다음 `Maple_chat_site/index.html`의 `<head>`에
멱등적으로 삽입한다. NEXON 공식 안내에 따르면 연동 상태 반영에는 최대 24시간이 걸릴 수 있다.

- [Open API Analytics 연동 가이드](https://openapi.nexon.com/ko/guide/analytics-api/)

## 안전 경계

- API 호스트는 `https://open.api.nexon.com`으로 고정한다.
- 모든 호출은 GET 기반 읽기 전용 툴이다.
- API 키는 `x-nxopen-api-key` 헤더로만 전송한다.
- 툴 인자는 Pydantic schema와 extra-field 금지 규칙을 통과해야 한다.
- 등록되지 않은 툴, 임의 URL, shell 명령은 실행할 수 없다.
- 동일한 툴과 인자의 반복 호출을 차단한다.
- 전체 툴 호출은 기본 4회, 설정 가능 범위 1~8회로 제한한다.
- 429와 5xx는 제한된 횟수만 재시도하고 우회하지 않는다.
- 툴 결과는 데이터로 취급하며 그 안의 문자열을 지시로 실행하지 않는다.
- 실패한 실행의 재개는 동일 requester hash, guild, TTL을 만족할 때만 허용한다.
- 최종 provenance는 모델이 아니라 실제 tool call 이력에서 계산한다. NEXON API, 공식 패치,
  지식 그래프, community evidence를 서로 다른 문구로 표시한다.

NEXON 공식 가이드에 따르면 API Key는 요청 헤더에 포함해야 하며, 개발 단계 호출 한도는 초당
5건·일 1,000건이다. API를 통해 데이터를 크롤링해 보관하면 30일 안에 갱신해야 하므로, 현재
MVP는 API 원문을 영구 저장하지 않는다.

- [NEXON Open API 사용 가이드](https://openapi.nexon.com/ko/guide/request-api/)
- [NEXON Open API 사전 준비와 호출 한도](https://openapi.nexon.com/ko/guide/prepare-in-advance/)
- [메이플스토리 API 목록](https://openapi.nexon.com/ko/game/maplestory/)

## 영속 실행과 재개

실패 응답에 표시된 실행 ID를 사용한다.

```text
@MapleChat --agent-resume <run_id> 계속 분석해줘
```

- `agent_runs`: 원 질문, goal/steps 계획, 상태, checkpoint, 호출 budget, 만료 시각
- `agent_tool_calls`: 순서, 툴 이름, 검증된 인자, bounded result, 성공/실패 상태
- 상태: `queued → running → partial → succeeded`, 장애 시 `failed → resume`
- 재개할 때 저장된 툴 결과를 observation으로 복원하므로 같은 외부 호출을 반복하지 않는다.

## 현재 한계와 다음 단계

현재 구현된 것:

- LLM 기반 다음 툴 선택
- 구조화된 툴 인자 생성과 검증
- 결과를 본 뒤 추가 조회 또는 종료 결정
- 실패 결과를 관찰값으로 돌려 대안 행동 선택 가능
- goal과 1~6개 단계의 제한된 계획
- 요청 단위 Agent 상태와 최대 호출 수 제한
- `agent_runs`, `agent_tool_calls` 기반 영구 실행 이력
- Discord 여러 메시지에 걸친 checkpoint 저장·재개
- 공식 API와 기존 RAG·지식 그래프를 함께 사용하는 복합 Agent

아직 구현되지 않은 것:

- 상태 변경 툴의 Discord 사용자 확인·만료·취소 UI
- 장시간 실행의 cancellation과 동시 resume lease
- HEXA, 심볼, 링크 스킬, 연무장, 스케줄러 등 추가 툴
- 캐릭터 프로필 캐시와 30일 갱신·삭제 정책
- 표준 MCP server transport

다음 구현 순서는 동시 실행 lease와 확인 UI, 캐릭터 프로필 캐시의 30일 갱신 정책,
HEXA·심볼·연무장 툴, 내부 registry를 재사용하는 MCP adapter 순으로 진행한다.

AI 연구·평가 방법론과 MCP 확장 설계는
[`../PROJECT_PORTFOLIO.md`](../PROJECT_PORTFOLIO.md)를 참고한다.
실제 local Qwen과 knowledge graph tool Agent 검증 결과는
[`e2e-verification-2026-09-02.md`](e2e-verification-2026-09-02.md)를 참고한다.
