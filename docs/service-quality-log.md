# Maple Chat 서비스 품질 개선 로그

이 문서는 Discord 실사용 및 평가 과정에서 발견한 품질 실패를 누적 관리한다. 새 문제는 수정
전에 기록하고, 코드 변경·회귀 테스트·실사용 재검증 결과를 같은 항목에 계속 추가한다.

각 문제의 기술적 배경과 전문 설계 원리는
[RAG·데이터베이스·지식 그래프 시스템 교과서](rag-knowledge-graph-textbook.md)에서 학습하고,
이 문서에서 실제 실패·해결·검증 이력을 확인한다.

개인 식별 정보, Discord 사용자 ID, 토큰, 원문 비밀값은 기록하지 않는다. 질문과 답변은 문제를
재현하는 데 필요한 최소 범위만 남긴다.

## 운영 규칙

### 상태

- `reported`: 실패 사례만 접수됨
- `reproduced`: 자동 또는 수동 재현 완료
- `implemented`: 해결 코드가 적용됐으나 실서비스 재검증 전
- `verified`: 자동 회귀 테스트와 실서비스 재검증 모두 통과
- `monitoring`: 재발 여부를 일정 기간 관찰 중
- `closed`: 종료 조건을 충족해 마감

### 심각도

- `S1`: 안전·보안·데이터 손상 또는 서비스 전체 장애
- `S2`: 사용자의 의사결정을 잘못 이끌 수 있는 사실 오류·상호 모순
- `S3`: 용어·분류·가독성 오류 등 신뢰도를 낮추는 문제
- `S4`: 경미한 표현 및 UX 문제

### 항목 작성 순서

1. 실제 결과와 기대 결과로 **문제를 정의**한다.
2. 확인된 원인과 아직 검증되지 않은 추론을 구분한다.
3. **해결 방안**과 변경 파일, 회귀 테스트를 기록한다.
4. 자동 검증과 Discord 실사용 결과를 **결과 및 Discussion**에 누적한다.
5. 남은 위험과 종료 조건이 모두 해소된 경우에만 `verified` 또는 `closed`로 바꾼다.

---

## SQ-2026-08-24-001 — 동의어 질문 간 직작 순서 모순

- 상태: `monitoring`
- 심각도: `S2`
- 분류: 일관성, 상충 근거 처리, 검색 안정성
- 발견 경로: Discord 수동 테스트

### 문제 정의

의미가 같은 두 질문에서 서로 다른 작업 순서가 단정적으로 생성됐다.

- 질문 A: `제논 방어구 직작할 때 어떻게 해야 할까?`
  - 결과: `놀긍 → 스타포스 → 추옵`
- 질문 B: `제논 방어구 직작 순서를 알려줘`
  - 결과: `추옵 → 놀긍 → 스타포스`

기대 결과는 표현만 다른 질문에는 같은 결론을 내리는 것이다. 검색 근거가 실제로 상충한다면
어느 하나를 임의로 선택하지 않고, 상충하는 순서와 확정할 수 없는 이유를 함께 밝혀야 한다.

### 원인 분석

#### 코드로 확인된 사실

- 기존 검색은 사용자의 원문 질문을 그대로 임베딩·키워드 검색에 사용했다.
- 기존 프롬프트는 한 번의 검색 결과 안에 상충 자료가 동시에 들어온 경우에만 상충을 밝히도록
  했다. 표현이 바뀌어 서로 다른 자료가 검색되면 답변 간 일관성을 보장하지 못했다.
- 절차, 평균, 최소컷을 단일 경험담에서 일반화하지 못하게 하는 명시적 규칙이 없었다.

#### 추론

- 두 표현의 검색 후보와 재정렬 순위가 달라져 각각 다른 커뮤니티 의견을 선택했을 가능성이 높다.
- 현재 데이터는 공식 공략집이 아니라 커뮤니티 경험담이므로, 단순히 최신 글 하나를 우선하는
  것만으로는 보편적인 작업 순서를 확정할 수 없다.

### 폐기된 1차 해결 방안

1. `직작 + 순서/방법/어떻게` 질문에 공통 검색 힌트 `장비 강화 작업 순서`를 추가한다.
2. 관련 약어와 직업군을 정규화해 표현이 달라도 검색어의 핵심 축을 동일하게 만든다.
3. 절차·순서·수치가 단일 경험담에만 있으면 일반화하지 않도록 프롬프트를 강화한다.
4. 자료가 상충하면 양쪽을 제시하고 근거 없이 하나를 정답 처리하지 못하게 한다.
5. 장기적으로는 답변 직전 구조화된 주장 추출과 `주장-근거 수/상충 수` 검증 단계를 추가한다.

### 1차 적용 및 롤백

- `src/maple_chat/domain.py`: 공식 용어·직업군 및 검색 확장 규칙 추가
- `src/maple_chat/qa/service.py`: 검색 확장, 상충·일반화 방지 프롬프트 적용
- `tests/unit/test_domain_knowledge.py`: 두 직작 질문에 동일한 핵심 검색 힌트가 붙는지 검증
- `tests/unit/test_qa_prompt.py`: 상충 및 단일 경험담 일반화 금지 규칙 검증
- 2026-08-24: 위 변경은 사례별 키워드와 소프트 프롬프트에 과적합됐다고 판단해 전부 제거했다.

### 결과 및 Discussion

- 1차 휴리스틱의 자동 회귀 및 스모크 테스트는 통과했지만 일반화 증거가 아니므로 해결 완료로
  인정하지 않는다.
- 2026-08-25 현재 실제 RAG 파이프라인으로 원문 case1·case2를 재실행했으며 순서 모순이 그대로
  재현됐다.
  - `제논 방어구 직작할 때 어떻게 해야할까?` → `놀긍 → 추옵 및 스타포스`
  - `제논 방어구 직작 순서를 알려줘` → `추옵 → 놀긍 → 스타포스`
- 검색된 evidence 구성이 두 질문에서 달랐고, 절차 주장을 구조화해 상충 비교하는 단계가 없어서
  모델이 각각 다른 순서를 단정했다.
- Discord 실사용 재검증: 자동 파이프라인에서 실패 재현, 수정 후 Discord 재검증 필요
- 현재 활성 코드에는 질문별 검색어 확장이나 사례별 상충 프롬프트가 없다.
- 2026-08-25 일반화된 근거 역할 분리를 적용했다.
  - `질문과 답변` 게시판의 article chunk는 `question_only`로 분류해 주장 근거에서 제외한다.
  - `qa_branch=true`인 comment chunk는 chunker가 만든 `질문 문맥`과 `댓글 N:` 경계를 사용해
    댓글 답변 부분만 모델과 지식 그래프에 전달한다.
  - 이는 `제논`, `직작`, 특정 순서를 찾는 문자열 규칙이 아니라 source type/metadata 계약에
    따른 구조적 처리다.
- 운영 DB 64,582개 활성 청크, 실제 BGE-M3/pgvector/pg_trgm/BGE reranker와 로컬 Qwen으로
  원문 두 질문을 다시 실행했다. 두 답변 모두 `순서는 중요하지 않다`를 결론으로 내리고 힘 추가
  옵션이라는 선행 조건만 설명했다. 이전의 서로 다른 절대 순서는 재현되지 않았다.
- 현재 결과는 운영 파이프라인 트랜잭션 롤백 스모크 1회씩이다. 종료 조건의 질문별 3회 반복과
  Discord Gateway 경유 확인이 남아 있어 `verified`가 아니라 `monitoring`으로 둔다.

### 종료 조건

- 두 질문을 각각 3회 이상 반복했을 때 답변의 핵심 순서가 서로 모순되지 않는다.
- 상충 근거가 검색되면 답변에 상충 사실이 표시된다.
- 한 개의 경험담만으로 `평균`, `일반적`, `최소컷`을 만들지 않는다.

---

## SQ-2026-08-24-002 — `놀긍`의 잘못된 확장 `놀러긍`

- 상태: `monitoring`
- 심각도: `S3`
- 분류: 도메인 용어, 환각
- 발견 경로: Discord 수동 테스트

### 문제 정의

봇이 `놀긍`을 존재하지 않는 표현인 `놀러긍`으로 풀어 썼다. 공식 아이템명은
`놀라운 긍정의 혼돈 주문서`다.

기대 결과는 약어를 그대로 쓰거나, 처음 한 번만 정확한 공식 명칭을 병기하는 것이다. 사전에 없는
약어 확장을 모델이 추측해서 만들면 안 된다.

### 원인 분석

- 기존 시스템에는 메이플스토리 약어와 공식 명칭을 연결하는 신뢰 가능한 용어 사전이 없었다.
- 모델이 커뮤니티 약어를 음성적으로 추측해 존재하지 않는 확장을 생성했다.

### 폐기된 1차 해결 방안

1. 운영자 검증 용어 사전에 `놀긍 = 놀라운 긍정의 혼돈 주문서`를 등록했다.
2. 질문에 `놀긍`이 있으면 공식 명칭을 검색어와 신뢰 컨텍스트에 함께 넣는다.
3. 생성 결과에 `놀러긍` 또는 `놀긍(놀러긍)`이 남으면 공식 명칭으로 교정하는 결정적 출력
   가드레일을 추가했다.
4. 새 약어 오류는 이 로그에 등록한 뒤 사전과 회귀 테스트를 함께 추가한다.

2026-08-24에 위 사례별 사전·문자열 치환·테스트를 전부 제거했다.

### 결과 및 Discussion

- 1차 문자열 교정 테스트는 통과했지만 새로운 약어 오류에 일반화되지 않아 해결 완료로 인정하지
  않는다.
- 2026-08-25에 문자열 치환이 아닌 entity/relation 기반 아이템 명칭 그래프를 구현했다.
- 약어와 아이템을 별도 entity로 만들고 `약어 -[abbreviation_of]-> 공식 아이템명` 관계를
  저장한다. 질문 문자열을 사후 교정하지 않고 모든 약어에 동일한 entity linking/traversal을
  적용한다.
- 공식 아이템명은 넥슨 주문서/스크롤 확률 안내에서 확인한다. 약어 자체는 넥슨 공식 용어가
  아니므로 `Maple Chat maintainers`가 검수한 별도 source로 provenance를 구분한다.
- `놀러긍`은 alias나 entity로 등록하지 않았다. 따라서 이 문자열을 올바른 표현으로 인정하거나
  조용히 정규화하지 않는다.
- 현재 등록 관계는 다음과 같다.

| 검수 약어 | canonical target |
|---|---|
| 놀긍 | 놀라운 긍정의 혼돈 주문서 |
| 긍혼 | 긍정의 혼돈 주문서 |
| 놀혼 | 놀라운 혼돈의 주문서 |
| 프악공 | 프리미엄 악세서리 공격력 주문서 |
| 프악마 | 프리미엄 악세서리 마력 주문서 |
| 미악공 | 미라클 악세서리 공격력 주문서 |
| 미악마 | 미라클 악세서리 마력 주문서 |
| 프펫공 | 프리미엄 펫 장비 공격력 주문서 |
| 프펫마 | 프리미엄 펫 장비 마력 주문서 |
| 아크이노 | 아크 이노센트 주문서 |
| 추옵 | 추가 옵션 |
- RAG 모드에서는 위 relation이 canonical context로 제공되고 답변별 relation provenance가 남는다.
- `--no-rag`는 사용자가 지식 그래프까지 끄는 명시적 모드이므로 이 모드에는 공식 명칭 보장을
  적용하지 않는다. 이 차이를 반영해 종료 조건을 아래처럼 수정한다.
- 2026-08-25 원문 case1·case2 재실행에서 새로운 잘못된 확장이 확인됐다.
  - `놀긍(놀라운 긍정)`
  - `놀긍(놀라운 긍지)`
  - `프악공(프레이아크 공작)`
  - `아크이노베이션`
- 원인은 현재 entity linking이 **사용자 질문만** 검사하기 때문이다. 원문 질문에는 `놀긍`이나
  `프악공`이 없고 검색 evidence에만 등장하므로 아이템 `abbreviation_of` relation이 생성
  context에 포함되지 않았다. 실제 graph context에는 `제논→도적/해적`만 있었다.
- 따라서 직접 `놀긍의 원래 이름`을 묻는 테스트는 통과하지만 원래 실패 케이스를 해결한 것은
  아니다. 검색된 evidence에서도 entity를 연결해 canonical relation을 다시 조회하는 2단계
  enrichment가 필요하다.
- 2026-08-25 해당 2단계 enrichment를 구현했다. 사용자 질문의 entity와, 역할 분리 후 남은
  **댓글 주장 텍스트**의 entity를 각각 연결하고 relation ID로 합친다. 원문 질문에 약어가 없어도
  검색 댓글에 `놀긍`, `프악공`, `아크이노`, `추옵`이 있으면 canonical relation이 생성 문맥에
  포함된다.
- 생성 결과에도 relation 기반 validator를 추가했다. `abbreviation_of`가 제공된 약어를 괄호나
  등호로 풀어 쓴 명칭이 object의 canonical name과 다르면 초안을 폐기하고 correction JSON으로
  한 번 재생성한다. 두 번째도 불일치하면 생성 답변을 노출하지 않고 graph relation fallback으로
  전환한다. 알려진 오답 문자열을 치환하지 않는다.
- 첫 실제 스모크에서 `추옵(추천 옵션)`이라는 새 실패가 발견돼 이를 별도 검수 관계
  `추옵 -[abbreviation_of]-> 추가 옵션`으로 추가했다. 넥슨
  [공식 게임 용어집](https://gi.maplestory.nexon.com/Guide/GameInformation/Terms/Terms)이
  `추옵`과 `아크이노`의 줄임 관계를 직접 명시하므로 이 source를 provenance로 사용한다.
- 같은 공식 용어집의 효과 설명도
  `아크 이노센트 주문서 -[has_effect]-> 잠재능력과 스타포스 강화를 제외한 모든 옵션을 표준
  능력치로 초기화` 관계로 저장했다. 이 관계 추가 전 스모크에서 발견한 `아크이노로 힘을
  확보한다`는 잘못된 효과 귀속도 최종 재실행에서는 사라졌다.
- 최종 운영 파이프라인 재실행 결과:
  - case1: `추가 옵션`, `놀라운 긍정의 혼돈 주문서`, `아크 이노센트 주문서`,
    `프리미엄 악세서리 공격력 주문서`로 정확히 생성
  - case2: `놀라운 긍정의 혼돈 주문서(놀긍)`, `추가 옵션(추옵)`,
    `아크 이노센트 주문서(아크이노)`로 정확히 생성
  - 두 case 모두 잘못된 `놀라운 긍정/긍지`, `프레이아크 공작`, `아크이노베이션`,
    `추천 옵션`은 출력되지 않음

### 종료 조건

- RAG 답변에서 등록 약어를 풀어 쓸 때 graph의 공식 명칭과 일치한다.
- 등록되지 않은 문자열을 임의로 공식 약어로 인정하지 않는다.
- Discord RAG 대표 질문을 반복해 `놀긍 → 놀라운 긍정의 혼돈 주문서`를 확인한다.
- `--no-rag`는 공식 명칭 보장 대상이 아님을 운영 문서에 명시한다.

---

## SQ-2026-08-24-003 — 직업군과 하위 직업 계층 오류

- 상태: `implemented-awaiting-live-validation`
- 심각도: `S2`
- 분류: 도메인 온톨로지, 메타데이터 오해
- 발견 경로: Discord 수동 테스트

### 문제 정의

전체 직업군별 최소컷 답변에서 `렌(마법사)`처럼 잘못된 상위 직업군이 붙거나, 게시판 이름과
본문에 언급된 직업이 혼합됐다.

공식 기준의 대표 예시는 다음과 같다.

- 메카닉 → 해적
- 렌 → 전사
- 소울마스터(소마) → 전사
- 제논 → 도적/해적 하이브리드

기대 결과는 `전사/마법사/궁수/도적/해적 → 개별 직업` 계층을 일관되게 유지하고, 게시글이
올라온 게시판을 본문 속 캐릭터의 직업군으로 간주하지 않는 것이다.

### 원인 분석

- 청크 메타데이터의 `board` 값과 실제 본문에서 언급한 직업 사이의 의미가 구분되지 않았다.
- 생성 모델에 검증된 직업 계층이 제공되지 않아, 게시판·직업·작성자의 캐릭터를 임의로 연결했다.
- 일부 직업은 제논처럼 복수 직업군을 사용하므로 단일 상위 분류만 강제해도 오류가 된다.

### 폐기된 1차 해결 방안

1. 넥슨 공식 직업 소개 및 메이플 유니온 분류를 기준으로 직업 계층을 코드에 버전 관리한다.
2. 질문에 등장한 직업만 신뢰 컨텍스트에 넣고, `전체 직업군별` 요청에는 전체 계층을 제공한다.
3. 프롬프트에 `게시판 분류는 글의 위치일 뿐 직업군 증거가 아니다`라는 규칙을 추가했다.
4. `렌(마법사)`, `메카닉(전사)` 같은 명백한 괄호형 오류는 출력 가드레일로 교정한다.
5. 공식 분류가 바뀌거나 신규 직업이 추가되면 버전과 확인 날짜를 갱신하고 회귀 테스트를 추가한다.

2026-08-24에 위 문자열 탐색·정규식 출력 교정·정적 직업 목록 연결을 전부 제거했다.

### 공식 확인 자료

- [넥슨 메이플스토리 직업소개](https://maplestory.nexon.com/Guide/N23Job)
- [넥슨 메이플 유니온 직업군 표](https://maplestory.nexon.com/Guide/N23GameInformation/Articles/407)
- [넥슨 제논 직업소개](https://maplestory.nexon.com/Guide/N23Job/View/44)
- [넥슨 놀라운 긍정의 혼돈 주문서 확률 안내](https://maplestory.nexon.com/Guide/OtherProbability/game/gameOrderSheet)

### 결과 및 Discussion

- 1차 테스트는 알려진 괄호형 오류만 검증했으므로 일반화된 계층 검증으로 인정하지 않는다.
- 2026-08-25에 사례별 문자열 교정 없이 provenance-bearing 지식 그래프를 구현했다.
- 넥슨 공식 직업소개를 버전된 JSON catalog로 관리하고 PostgreSQL의 source/entity/alias/relation
  테이블에 idempotent upsert한다.
- 질문의 별칭을 DB entity에 연결하고 ontology type에 따라 exact graph traversal한다.
- 제논은 도적·해적 두 `is_a` relation으로 저장한다.
- 생성 프롬프트에서 공식 graph fact와 비신뢰 community evidence를 분리하고, 답변별 사용 relation을
  `answer_knowledge_sources`에 저장한다.
- 자동 검증은 통과했으나 실제 Discord에서 대표 질문을 다시 확인할 때까지 상태를 종료하지 않는다.

### 종료 조건

- 대표 회귀 질문에서 잘못된 직업군 조합이 출력되지 않는다.
- 게시판 이름을 본문 속 캐릭터 직업으로 해석하지 않는다.
- 전체 직업 요청에서 근거 없는 직업의 수치를 추측해 채우지 않는다.

---

## 검증 이력

| 시각(KST) | 대상 | 결과 | 비고 |
|---|---|---|---|
| 2026-08-24 | SQ-001~003 단위 테스트 | 통과 | 전체 unit 132개 통과 |
| 2026-08-24 | QA 트랜잭션 통합 테스트 | 통과 | 격리 PostgreSQL에서 검색 확장·출력 교정 확인 |
| 2026-08-24 | 정적 검증 | 통과 | Ruff, 포맷, Mypy 47개 소스, Bash 구문 검사 |
| 2026-08-24 | 상충 근거 로컬 모델 스모크 | 통과 | 한 순서를 선택하지 않고 두 주장과 확정 불가를 명시 |
| 2026-08-24 | Discord 봇 반영 | 통과 | 새 프로세스로 재시작, DB·LLM·임베딩 preflight 통과 |
| 2026-08-24 | 휴리스틱 롤백 결정 | 완료 | 사례별 검색 확장·사전·정규식 교정·관련 테스트 제거 |
| 2026-08-24 | 롤백 코드 검증 | 통과 | unit 128개, QA 통합 2개, Ruff, 포맷, Mypy, Bash 구문 통과 |
| 2026-08-24 | 롤백 Discord 반영 | 통과 | 새 봇 프로세스, DB·LLM·임베딩 preflight 통과 |
| 2026-08-24 | Discord 사례 재질문 | 대기 중 | SQ-001~003은 일반 해결책 전까지 `reported` 유지 |
| 2026-08-25 | 지식 그래프 단위 검증 | 통과 | 48 jobs, 5 families, 제논 복수 관계, longest alias linking |
| 2026-08-25 | 지식 그래프 DB 통합 검증 | 통과 | migration up/down/up, sync idempotency, exact traversal, 답변 relation 보존 |
| 2026-08-25 | 실 DB graph 조회 | 통과 | 렌·메카닉·소울마스터·제논 관계와 source version 확인 |
| 2026-08-25 | 로컬 Qwen graph-only smoke | 통과 | 렌→전사, 메카닉→해적, 소마→전사, 제논→도적/해적 반영 |
| 2026-08-25 | Discord 런타임 반영 | 통과 | g009 적용 후 봇·일일 scheduler 재기동, DB/Gateway 연결 확인 |
| 2026-08-25 | 아이템 약어 그래프 단위 검증 | 통과 | 9개 abbreviation_of 관계, `놀러긍` 미등록 확인 |
| 2026-08-25 | 아이템 약어 DB 통합 검증 | 통과 | 놀긍·프악공 exact traversal 및 provenance 확인 |
| 2026-08-25 | 아이템 약어 로컬 Qwen smoke | 통과 | 놀긍·프악공을 graph의 원래 아이템명으로 정확히 생성 |
| 2026-08-25 | 약어 그래프 전체 검증 | 통과 | 156 tests, coverage 90.21%, Ruff, Mypy |
| 2026-08-25 | 기존 64,582 청크 명칭 감사 | 통과 | 등록 약어 9개, 명시적 다른 확장 0건, `놀러긍` 0건 |
| 2026-08-25 | 원문 case1·case2 실제 RAG 재실행 | 실패 재현 | 순서 모순과 놀라운 긍정/긍지·프레이아크 공작 환각 확인 |
| 2026-08-25 | 근거 역할·2단계 KG·생성 validator 전체 검증 | 통과 | 164 tests, coverage 90.48%, Ruff, format, Mypy, Bash |
| 2026-08-25 | 운영 지식 graph 동기화 | 통과 | 3 sources, 79 entities, 115 aliases, 77 relations |
| 2026-08-25 | 원문 case1·case2 운영 RAG 재실행 | 통과 | 두 case 모두 절대 순서 부정, 놀긍·아크이노·추옵 공식 명칭 정확 |
| 2026-08-25 | 아크이노 효과 관계 운영 RAG 재실행 | 통과 | 초기화 효과를 정확히 분리하고 힘 부여 효과로 오인하지 않음 |
| 2026-08-25 | Discord 런타임 최종 반영 | 통과 | 새 bot PID, Gateway·PostgreSQL 연결, 일일 수집 timer, DB·LLM preflight 확인 |
| 2026-08-25 | SQ-007 운영 corpus/답변 provenance 재현 | 통과 | 동일 글 댓글 5/8 점유, 92·93퍼 직접 성공 글 검색 누락 확인 |
| 2026-08-25 | SQ-007 실제 BGE/Qwen 운영 RAG 재실행 | 통과 | 92퍼 직접 성공 evidence 회수, 관측 최저와 보편 최소컷 분리 |
| 2026-08-25 | SQ-007 Discord 런타임 반영 | 통과 | 새 bot·일일 sync PID, DB·Discord Gateway TLS 연결 확인 |

---

## SQ-2026-08-24-004 — 사례별 휴리스틱에 과적합된 품질 가드레일

- 상태: `reported`
- 심각도: `S2`
- 분류: 아키텍처, 일반화, 검증 체계
- 발견 경로: 해결안 설계 검토

### 문제 정의

SQ-001~003의 즉시 수정에는 `직작` 키워드, `놀긍/놀러긍` 문자열, 괄호형 직업군 표기처럼
관찰된 사례를 직접 겨냥한 규칙이 포함되어 있다. 이 규칙은 동일 패턴의 재발은 막지만 다음과 같은
새 실패를 일반적으로 검출하지 못한다.

- 다른 제작·강화 절차에서 서로 다른 순서를 단정하는 경우
- 사전에 없는 새로운 약어를 잘못 풀어 쓰는 경우
- `렌은 마법사다`처럼 괄호를 사용하지 않은 계층 오류
- 여러 경험담에서 표본 수와 조건이 다른데 평균·최소컷으로 일반화하는 경우
- 표현은 다르지만 의미가 같은 질문에 과거 답변과 모순되는 경우

### 원인 분석

- 현재 검색 확장은 규칙에 등록된 문자열이 있어야 작동한다.
- 상충 판단을 생성 모델의 프롬프트 준수에 의존하며, 코드가 근거의 주장을 구조화해 비교하지 않는다.
- 출력 교정은 알려진 잘못된 문자열과 문장 형태만 수정한다.
- 생성 답변을 독립적으로 검증하는 사실성·계층·인용 검사 단계가 없다.
- 의미가 같은 과거 질문과 답변을 비교하는 일관성 저장소가 없다.

### 일반화된 해결 방향

1. 질문을 `대상/행위/조건/요청 형태/기준 시점`의 구조화된 의도로 변환한다.
2. 원문과 구조화된 의도에서 복수 검색 질의를 만들고 결과를 합쳐 표현 차이의 영향을 줄인다.
3. 각 근거에서 `주장/값/단위/조건/시점/출처`를 구조화해 추출한다.
4. 동일 주장끼리 지지·상충·조건부 차이를 판정하고 근거 수와 권위 수준을 계산한다.
5. 생성 모델에는 원문 청크가 아니라 검증된 주장 묶음과 상충 판정 결과를 전달한다.
6. 생성 후 모든 핵심 문장이 실제 주장 ID를 인용하는지 검사하고, 실패하면 재생성하거나 거절한다.
7. 도메인 용어와 직업 계층은 정규식이 아니라 버전된 지식베이스와 엔티티 링커로 관리한다.
8. 의미가 같은 질문 묶음에 대해 답변 핵심 주장의 모순률을 지속 측정한다.

### 결과 및 Discussion

- SQ-001~003에 적용했던 휴리스틱은 2026-08-24에 제거했다.
- 새로운 품질 문제의 해결책을 정규식 추가로 삼지 않고, 구조화된 주장 검증 단계의 평가 사례로
  추가한다.

### 종료 조건

- 등록되지 않은 절차 질문에서도 상충하는 순서를 자동 검출한다.
- 신규 약어가 사전에 없으면 임의 확장하지 않고 원문을 유지하거나 불확실성을 표시한다.
- 문장 형식과 무관하게 직업 계층 오류를 검출한다.
- 의미가 같은 질문 평가 세트에서 핵심 주장 모순률이 합의된 기준 이하가 된다.
- 답변의 핵심 사실마다 검증 가능한 근거 또는 공식 지식 항목이 연결된다.

---

## SQ-2026-08-25-005 — 수집 데이터의 명칭 drift가 지속 검수되지 않음

- 상태: `monitoring-active`
- 심각도: `S3`
- 분류: 도메인 용어, 데이터 품질, 지속 검증
- 발견 경로: 지식 그래프 운영 검토

### 문제 정의

지식 그래프에 약어와 공식 명칭을 등록해도 새 게시글에서 다른 확장, 붙임형 표현, 신규 약어가
계속 등장할 수 있다. 정적 catalog만 갱신하면 수집 corpus와 canonical knowledge의 차이를 늦게
발견하며, 빈도만 보고 자동 등록하면 커뮤니티 오표기를 공식 사실로 오염시킬 위험이 있다.

기대 결과는 수집·색인 이후 corpus를 read-only로 검사하고, 공식 관계와 다른 명시적 확장과 신규
surface 후보를 누적하되 자동으로 canonical relation을 변경하지 않는 것이다.

### 해결 방안 및 적용 내용

1. 활성 `abbreviation_of` relation을 감사 기준으로 사용한다.
2. 활성 청크에서 약어·공식 전체 명칭 빈도를 집계한다.
3. `약어(확장)`, `약어 = 확장`만 명시적 이름 확장 후보로 추출한다.
4. official target과 normalized comparison하고 다른 후보를 source/chunk ID와 함께 기록한다.
5. 붙임형 surface는 별도 빈도로 기록하지만 alias라고 자동 판정하지 않는다.
6. `scripts/sync_live.sh`가 수집·색인 뒤 `maple-chat audit-knowledge`를 실행한다.
7. 실행 결과는 `logs/knowledge-audit.jsonl`에 append-only로 누적한다.

변경 파일:

- `src/maple_chat/knowledge/audit.py`
- `src/maple_chat/cli.py`
- `scripts/sync_live.sh`
- `docs/knowledge-audit.md`
- `tests/unit/test_knowledge_audit.py`
- `tests/integration/test_knowledge_audit.py`

### 결과 및 Discussion

- 현재 활성 청크 64,582개와 등록 약어 11개를 검사했다.
- 공식 명칭과 다른 명시적 확장 후보는 0건이며 `놀러긍`도 0건이다.
- 공식 전체 명칭은 약어보다 매우 드물다. 예를 들어 `놀긍`은 350개 청크, 공식 전체 명칭은
  2개 청크에 등장해 생성 시 canonical graph 제공이 필요하다는 점을 확인했다.
- `놀긍혼` 32개, `놀긍혼주문서` 6개가 추가 alias 후보로 관찰됐다. 자동 등록하지 않고 검수
  대상으로 유지한다.
- 새로 등록한 `아크이노`는 37개, `추옵`은 1,318개 활성 청크에서 관찰됐다. 각 canonical
  명칭은 `아크 이노센트 주문서`, `추가 옵션`이며 명시적 다른 확장 후보는 0건이다.
- `프악공마`, `프펫공마`는 공격력/마력 아이템을 함께 지칭할 수 있어 하나의 official item에
  연결하면 안 되는 모호한 후보로 유지한다.
- 괄호·등호 없이 잘못 풀어 쓴 문장과 완전히 새로운 약어는 아직 정확 비교하지 못한다.

### 종료 조건

- 매 일일 수집 뒤 감사 JSONL이 추가된다.
- 다른 명시적 확장이 발견되면 source/chunk ID와 함께 검수 가능하다.
- 사람의 검수와 공식 명칭 확인 없이 graph relation이 자동 변경되지 않는다.
- 신규 약어 discovery 범위를 확대하더라도 false positive와 자동 승격 방지 계약을 유지한다.

---

## SQ-2026-08-25-006 — 학습 문서가 입문 안내 수준에 머묾

- 상태: `verified`
- 심각도: `S3`
- 분류: 문서 품질, 지식 전달, 운영 인수인계
- 발견 경로: 사용자 문서 검토

### 문제 정의

기존 학습 문서는 RAG 흐름과 현재 구현을 쉽게 소개했지만, 실습 목록과 독립 용어 사전 중심의
입문 안내에 가까웠다. 정보검색 수학, dense 표현 학습, ANN/HNSW, context engineering,
ontology semantics, claim contradiction, 계층형 평가, poisoning, transaction, capacity planning과
같은 전문가 필수 영역이 하나의 이론 체계로 연결되지 않았다.

기대 결과는 초심자가 첫 장부터 시작해 실제 RAG·지식 그래프 시스템을 설계하고, component
metric과 failure mode를 구분하며, production architecture와 연구 방향까지 판단할 수 있는
교과서형 문서다.

### 원인 분석

- 기존 문서는 “무엇인지”와 “어디를 읽을지”는 설명했지만 “왜 이 설계가 필요한지”, “수학적으로
  무엇을 최적화하는지”, “어떤 조건에서 실패하는지”를 충분히 연결하지 않았다.
- Maple Chat 구현 설명과 일반 IR/KG 이론이 분리돼 있어 다른 프로젝트에 지식을 전이하기 어려웠다.
- 컴포넌트별 평가와 보안·운영이 후속 참고사항으로만 취급됐다.

### 해결 방안 및 적용 내용

- 기존 입문 가이드를 `docs/rag-knowledge-graph-textbook.md` 교과서로 전면 교체했다.
- 32개 장, 6개 부로 구성하고 기초→시스템→전문 이론→지식 표현→평가·보안→전문가 목표
  아키텍처 순서를 만들었다.
- 다음 내용을 수식, logical model, failure taxonomy, production contract와 함께 연결했다.
  - Precision/Recall/MRR/nDCG, RRF, cosine similarity
  - sparse/BM25/trigram과 dense bi-encoder/cross-encoder
  - HNSW와 ANN recall-latency trade-off
  - chunking, query orchestration, context packing, abstention
  - relational/RDF/property graph, open-world, domain/range, cardinality
  - entity linking, NIL detection, bounded traversal, GraphRAG 구분
  - claim schema, qualifier, contradiction, procedure DAG와 통계 claim
  - retrieval/generation/E2E 평가, hard negative, consistency, faithfulness
  - prompt injection, knowledge poisoning, 개인정보와 source governance
  - idempotency, revision, transaction, backpressure, SLO와 GPU capacity
  - canonical KG와 extracted claim graph를 분리한 목표 아키텍처
- Maple Chat의 실제 실패 case와 현재 코드 경로를 각 이론의 지속적인 사례 연구로 사용했다.
- 실습 문제와 독립 용어 사전은 제거하고 개념을 본문 논증 안에서 완결되게 설명했다.
- README, 현재 시스템 설명서, 지식 그래프 문서와 이 로그의 진입 링크를 교과서로 교체했다.

### 결과 및 Discussion

- 교과서는 33개 장(0~32장), 2,820줄·10,444 whitespace-delimited words 규모이며 현재 구현과
  미래 목표를 명확히 구분한다.
- RAG, DPR, RRF, HNSW, BEIR, long-context, GraphRAG, PoisonedRAG의 원 연구 링크를 참고문헌으로
  포함했다.
- 장 번호 연속성, 상대 링크, code fence 균형, 폐기 문서 링크 부재, 필수 전문 주제와 현재
  graph/corpus 수치의 일관성 검증을 통과했다.

### 종료 조건

- 입문 정의에서 시작해 IR/KG 수학·논리·평가·보안·운영까지 단일 학습 경로가 존재한다.
- 현재 구현과 아직 구현하지 않은 전문가 목표 구조가 혼동되지 않는다.
- 모든 내부 링크와 code fence가 유효하고, 폐기된 입문 문서 링크가 남지 않는다.
- 서비스 실패 사례가 일반 이론과 실제 코드·검증에 연결된다.

---

## SQ-2026-08-25-007 — 저장된 제논 하드 메이린 성공 기록 검색 누락

- 상태: `monitoring`
- 심각도: `S2`
- 분류: 검색 recall, 재랭킹, 근거 역할, 수치 주장
- 발견 경로: Discord 실사용 재검증

### 문제 정의

- 질문: `제논의 하드메이린 최소컷 알려줄래?`
- 실제 결과: 봇은 “명확한 수치가 없다”고 답하고 템셋팅, 타 직업, 현재 계산값 `98퍼`만 설명했다.
- 기대 결과: corpus에 있는 제논 직접 성공 기록을 회수해 검색 근거에서 관측된 최저 성공 수치를
  먼저 제시하고, 이를 모든 사용자에게 보장되는 보편 최소컷과 구분해야 한다.
- 사용자 영향: 존재하는 근거를 없다고 답해 RAG와 수집 데이터 전체의 신뢰를 떨어뜨린다.

### 원인 분석

#### 확인된 사실

1. 당시 운영 답변의 최종 8개 근거 중 5개가 동일한 `제논 템셋팅 질문입니다` 글에서 파생된 서로
   다른 댓글이었다.
2. 댓글 청크마다 `source_key`가 달라 기존 source diversity가 이 중복을 막지 못했다.
3. 재랭커는 질문 문맥을 포함한 댓글 전체를 평가했지만 생성 단계는 질문 문맥을 주장 근거에서
   제거했다. 검색 점수의 대상과 생성 근거의 대상이 달랐다.
4. 기존 품질 boost 상한 `0.12`는 dense·lexical 양쪽 1위의 RRF 약 `0.0328`보다 커 관련도를
   뒤집을 수 있었다.
5. 운영 corpus에는 다음 직접 성공 글이 실제로 존재한다.
   - `article:2298:224856`: `하드 메이린 93% 클리어`, 본문에 부캐 제논 성공 명시
   - `article:2298:224946`: `제논 뉴비 응애 하드메이린92퍼 클리어`, 본문에 92퍼 성공 명시
6. BGE 재랭커 원점수는 92퍼 직접 성공 글에 `0.0107`, 템셋팅 질문 글에 `0.9830`을 부여했다.
   cross-encoder 단독 threshold가 직접 성공 근거를 false negative로 제거했다.

#### 추론

- 같은 질문 문맥의 반복과 긴 문서 길이가 BGE 점수를 포화시켰을 가능성이 있다.
- 짧고 비문이 섞인 성공 글은 모델이 질문과의 함의를 충분히 잡지 못한 것으로 보인다.

### 해결 방안

1. 댓글을 개별 ID가 아니라 parent article thread 단위로 다양성 제어한다.
2. 재랭킹 전 thread당 최대 3개, 최종 근거에는 thread당 최대 1개만 허용한다.
3. 댓글 재랭킹은 반복 질문이 아니라 실제 `댓글 N:` 주장 부분으로 수행한다.
4. `question_context`와 `claim_text`를 별도 필드로 생성 모델에 제공한다.
5. 지식 그래프가 질문에서 정확히 연결한 entity를 검색 anchor로 재사용한다.
6. 품질 prior 상한을 `0.012`로 축소하고 dense 후보를 1,000개까지 넓힌다.
7. 원 게시글은 cross-encoder와 제목 문자 bigram coverage의 OR ensemble로 false negative를
   보완한다. 댓글에는 제목 fallback을 적용하지 않는다.
8. 수치를 성공, 실패, 현재 계산값, 계획, 타인 기록 인용으로 분리하고 직접 성공 기록 중 관측
   최저값을 핵심 결론에 먼저 쓰도록 grounded prompt 계약을 명시한다.

### 적용 내용

- `src/maple_chat/retrieval/evidence.py`: claim/context 분리, parent article thread group
- `src/maple_chat/retrieval/hybrid.py`: thread cap, claim-only reranking, KG anchor, 품질 prior 축소,
  dense recall 확대, article title ensemble
- `src/maple_chat/runtime.py`: graph entity를 hybrid retriever anchor로 전달
- `src/maple_chat/indexing/service.py`: 신규 댓글 metadata에 `article_source_key` 저장
- `src/maple_chat/qa/service.py`: 구조화된 context/claim과 수치 stance 응답 계약
- `tests/unit/test_hybrid_ranking.py`, `tests/unit/test_qa_prompt.py`: 일반화된 회귀 테스트
- `docs/rag-knowledge-graph-textbook.md`: 실제 실패를 IR·reranking·diversity 사례로 추가

### 결과 및 Discussion

- 단위 회귀에서 sibling comment가 하나의 thread로 묶이고 원 게시글은 독립 근거로 유지됨을
  확인했다.
- 댓글 재랭킹 입력에서 질문의 `98%가 최소컷` 가정이 제거되고 실제 댓글 답변만 남음을 확인했다.
- 지식 그래프 anchor와 제목 ensemble 적용 후 운영 BGE/DB 검색에서 92퍼 직접 성공 글이 최종
  evidence 7위로 회수됐다.
- 같은 실행에서 108퍼 타임아웃, 105퍼 실패 시도, 97·98퍼 도전과 92퍼 직접 성공이 서로 다른
  관찰로 제공됐다.
- 최종 운영 Qwen 스모크 답변은 핵심 결론에 `커뮤니티 사례로 확인된 최저 성공 기록은 92퍼`를
  제시했고, 97·98·105·108퍼는 실패 또는 도전 사례로 분리했으며 92퍼가 보편 기준이 아니라는
  한계를 명시했다.
- 자동 검증은 171 tests, coverage 90.54%, Ruff, format, Mypy, Bash syntax를 통과했다.
- 새 Discord bot 프로세스와 일일 최신 수집 루프를 재기동했고, PostgreSQL 및 Discord Gateway
  TLS 연결을 운영 소켓에서 확인했다.
- 수치 stance를 자연어 모델이 최종 분류하는 부분은 아직 extracted claim graph가 아니므로,
  장기적으로 `주장/값/단위/성공 여부/조건/출처` 구조화와 결정적 검증이 필요하다.

### 종료 조건

- 운영 RAG에서 92퍼 직접 성공 글이 최종 evidence에 포함된다.
- 핵심 결론에 “검색 근거에서 확인된 최저 성공 기록 92%”가 표시된다.
- 97·98·105·106·108퍼 시도/실패를 성공 기록으로 잘못 합치지 않는다.
- 동일 게시글 댓글이 최종 근거를 두 개 이상 점유하지 않는다.
- 전체 자동 검증과 Discord 봇 재시작이 통과한다.

---

## SQ-2026-08-25-008 — 벨로나 질문에 메이린 근거를 대입한 보스 엔티티 혼동

- 상태: `monitoring`
- 심각도: `S2`
- 분류: 엔티티 동일성, 보스 난이도, 검색 범위, 콘텐츠 용어
- 발견 경로: Discord 실사용 재검증

### 문제 정의

- 질문: `제논의 벨로나 최소컷 배율은?`
- 실제 결과: 답변이 `벨로나(메이린)`이라고 두 보스를 같은 대상으로 표시하고, 제논의 하드
  메이린 97~98% 자료를 벨로나 질문의 근거로 사용했다.
- 기대 결과: 벨로나와 메이린을 서로 다른 보스 entity로 유지하고, 질문에 지정된 보스와 난이도가
  일치하는 근거만 수치 답변에 사용해야 한다. 직접 근거가 없으면 다른 보스 수치를 대입하지 말고
  해당 보스의 근거 부족을 밝혀야 한다.
- 사용자 영향: 다른 보스의 배율은 체력·패턴·난이도 조건이 달라 의사결정을 잘못 이끌 수 있다.

### 원인 분석

#### 확인된 사실

1. 15:20 운영 답변의 최종 근거 4개는 모두 제논/메이린 글이었다. 벨로나가 포함된 근거는 한
   건도 없었다.
2. 당시 canonical graph는 직업, 아이템, 일반 게임 용어만 포함했고 보스 identity와 난이도
   변형을 표현하지 않았다.
3. `제논`처럼 specificity가 높은 job과 새 boss entity가 함께 연결될 경우 기존 전역 specificity
   최대값 정책은 boss match를 버리는 구조였다. 독립 온톨로지 축을 전역 한 줄로 비교한 설계
   오류다.
4. 넥슨 클라이언트 1.2.418 업데이트는 벨로나를 신규 보스로 정의하고 이지·노멀·하드 격파
   미션을 각각 명시한다.
5. 넥슨 클라이언트 1.2.416 업데이트는 메이린을 별도 시즌 보스로 정의하고 노멀·하드 난이도를
   명시한다.
6. 당시 catalog에서 `유챔`은 `유니온 챔피언`, `데스티니`는 무기·초월 체계로만 분류했다.
   데스티니의 보스 모드 의미 누락은 이후 공식 1.2.401을 확인한 SQ-010에서 정정했다.

#### 추론

- `벨로나` 자체가 최신 entity라 dense/re-ranker가 직업 일치도가 높은 기존 메이린 글을 더 높게
  선택했고, canonical identity 제약이 없어 생성 모델이 두 대상을 임의로 합친 것으로 판단한다.

### 해결 방안

1. 보스 base entity와 난이도별 `boss_variant`를 분리하고
   `난이도 변형 -[variant_of]-> base boss`로 저장한다.
2. 벨로나와 메이린은 source가 다른 별도 catalog로 유지해 provenance를 섞지 않는다.
3. `노말`은 공식 표기 `노멀`의 검색 alias로만 두고 canonical 이름은 공식 표기를 사용한다.
4. 유니온 챔피언과 데스티니 무기를 각각 `content_system`, `weapon`으로 모델링한다. `유챔`과
   `데티`는 별도 abbreviation entity로 canonical target에 연결한다.
5. entity linker의 specificity는 job ontology 내부에서만 비교하고 boss/content/item 같은 독립
   facet은 함께 보존한다.
6. 질문에 canonical job과 boss명이 명시되면 검색 후보가 **job facet AND boss facet**을 모두
   만족해야 한다. 같은 facet 안의 여러 entity는 OR로 처리한다. 일치 근거가 0건이면 다른
   직업이나 보스로 fallback하지 않는다.
7. evidence text에서 추가 연결한 graph fact도 질문 job/boss와 다른 facet이면 병합하지 않는다.
8. 생성 계약에 `boss`, `content_system`, `weapon` type을 서로 대체하지 않고, 다른 보스의 수치를
   질문 보스에 사용하지 않는 규칙을 명시한다.

### 적용 내용

- `boss-bellona-v1.json`: 벨로나 및 이지·노멀·하드 variant
- `boss-meyrin-v1.json`: 메이린 및 노멀·하드 variant
- `union-champion-v1.json`: 유니온 챔피언, 유챔, 챔피언 모드
- `destiny-weapon-v1.json`: 데스티니 무기·초월·모드와 데티
- `src/maple_chat/knowledge/catalog.py`: 내장 catalog 7개 로딩
- `src/maple_chat/knowledge/service.py`: job과 boss의 독립 facet linking
- `src/maple_chat/retrieval/hybrid.py`, `src/maple_chat/runtime.py`: canonical job AND boss scope filter
- `src/maple_chat/qa/service.py`: job/boss-scoped graph merge 및 type/identity 생성 계약
- 회귀 테스트: catalog schema, 제논+노말 벨로나 동시 linking, 벨로나/메이린 교차 방지,
  유챔·데스티니 분류, 다른 보스 candidate 제거, prompt identity 계약

### 결과 및 Discussion

- 공식 자료로 벨로나 `이지/노멀/하드`, 메이린 `노멀/하드`를 확인했다.
- 새 내장 graph 규모는 source 7개, entity 94개, alias 151개, relation 89개다.
- 관련 단위 회귀 27건과 PostgreSQL graph integration 3건이 통과했다.
- `제논의 노말 벨로나 최소컷` graph 조회에는 제논→도적/해적과
  `벨로나 (노멀)→벨로나`만 포함되고 메이린 relation은 0건이다.
- `유챔`은 유니온 챔피언 abbreviation으로, `데스티니`는 weapon으로 조회되며 boss type은
  포함되지 않는다.
- 보스 catalog는 현재 실패에 등장한 벨로나·메이린부터 provenance를 확정해 추가했다. 모든
  역사적 보스의 전체 난이도 catalog는 별도 확장 과제이며, 미등록 보스를 등록된 보스로 추정하지
  않는다.
- 첫 운영 DB 스모크에서 boss만 제한했을 때 팬텀의 노멀 벨로나 102.6%를 제논 기록으로 대입하는
  추가 failure가 발견됐다. 이를 job AND boss facet 제약으로 일반화한 뒤 재실행했다.
- 최종 운영 DB/BGE/Qwen 스모크에서는 `하드벨로나 112% 18분대`라는 **제논·벨로나를 모두
  포함한 글 1건만** 최종 evidence로 남았다. 메이린 글과 팬텀·엔버·데벤 등 다른 직업 글은
  모두 제외됐다.
- 생성 답변에는 `벨로나(메이린)`이나 `제논(팬텀)` 표현이 없었고, 제논의 112% 개인 성공
  기록과 보편 최소컷을 구분했다.
- 다만 같은 답변의 첫 문장이 “직접 성공 기록이 없다”는 표현과 “112% 개인 성공 기록”을 함께
  사용해 수치 stance 문구의 미세한 모순이 남았다. entity 혼동은 해결됐지만 이 표현 문제는
  extracted claim graph 도입 전까지 monitoring한다.
- 전체 검증은 179 tests, coverage 90.69%, Alembic upgrade/downgrade/upgrade, Ruff, format, Mypy,
  Bash syntax와 ShellCheck를 통과했다.
- 운영 bot을 PID `790808`, 일일 최신 수집 루프를 PID `790758`로 재시작했다. PostgreSQL과
  Discord Gateway의 새 TLS 연결을 확인했고 production graph는 7/94/151/89로 동기화됐다.
- Discord 채널에서 사용자가 동일 질문을 다시 호출하는 최종 실사용 확인은 남아 있다.

### 종료 조건

- 벨로나 질문의 최종 community evidence에 메이린 전용 글이 포함되지 않는다.
- 답변이 `벨로나(메이린)`처럼 서로 다른 boss ID를 동의어 처리하지 않는다.
- 벨로나·메이린의 공식 난이도 variant가 정확히 반환된다.
- 당시 `유챔`은 유니온 챔피언, `데스티니`는 무기/초월 체계로 분류됐다. 데스티니 보스 모드 의미는 SQ-010에서 추가했다.
- 전체 테스트·정적 검사와 운영 bot 재시작 후 동일 질문 스모크가 통과한다.

---

## SQ-2026-08-25-009 — 보스 온톨로지 부분 구현과 공식 패치 지식의 최신성 공백

- 상태: `monitoring`
- 심각도: `S2`
- 분류: 온톨로지 완전성, temporal RAG, 공식 출처, 운영 자동화
- 발견 경로: 사용자 설계 검토

### 문제 정의

- 조건: 사용자는 벨로나·메이린 두 사례만 고치는 것이 아니라 메이플스토리 보스 전체를 entity로
  만들고, 최근 1년 패치노트도 자동으로 검색 근거에 반영하기를 원했다.
- 실제 상태: SQ-008 해결은 벨로나와 메이린만 catalog에 등록했다. 다른 보스 질문은 identity와
  난이도 제약을 받지 못했고, 공식 패치노트는 운영 corpus에 자동 유입되지 않았다.
- 기대 결과: 공식 보스 가이드의 모든 상시 보스와 실제 난이도 조합을 versioned graph로 관리하고,
  최근 1년 공식 업데이트는 신규·수정분만 정기 수집·색인해야 한다.
- 사용자 영향: 미등록 보스는 다른 보스 수치로 대체될 수 있고, 패치 후 변경된 규칙을 오래된
  커뮤니티 경험담으로 답할 가능성이 있다.

### 원인 분석

#### 확인된 사실

1. 기존 내장 graph에는 base boss 2개와 variant 5개만 있었다.
2. 넥슨 공식 `보스별 주요 보상` 가이드는 현재 상시 보스 33개와 실제 난이도 조합 79개를
   제공한다.
3. 넥슨 업데이트 목록은 안정적인 update ID, 게시일, 선택적 수정 시각과 상세 URL을 제공한다.
4. 공식 업데이트 상세 본문은 `.qs_text .new_board_con` 영역에 있으며, 1.2.418 실페이지에서
   37,236자의 본문을 구조적으로 추출할 수 있었다.
5. 기존 `articles`/`chunks` schema는 synthetic official board를 추가하면 별도 migration 없이
   공식 문서의 provenance, 시간, content hash와 vector를 저장할 수 있다.

#### 추론

- 특정 failure entity만 추가한 것은 빠른 차단에는 유효했지만 ontology completeness를 목표로
  하지 않아 같은 종류의 미등록 보스 failure가 반복될 구조였다.
- 패치 사실과 커뮤니티 체감을 같은 authority로 취급하면 최신 공식 변경이 검색 순위와 생성
  단계에서 충분히 우선되지 않는다.

### 해결 방안

1. 공식 보스 가이드를 source로 `taxonomy:bosses`, base `boss`, 난이도별 `boss_variant`, 다섯
   `difficulty` node를 만들고 `part_of`, `variant_of`, `has_difficulty` 관계를 저장한다.
2. 실제 공식 조합만 node로 만들고 존재하지 않는 난이도는 생성하지 않는다. `노말`은 공식
   `노멀`을 찾는 alias로만 둔다.
3. 상시 가이드에 없는 시즌 보스 메이린은 별도 공식 update provenance를 유지한다.
4. 구체 entity가 함께 나온 분류 질문에서는 `보스` taxonomy alias가 전체 boss graph로 확장되지
   않도록 generic taxonomy expansion guard를 적용한다.
5. Nexon 전용 GET-only client를 만들고 HTTPS host와 `/News/Update` 경로, redirect, content type,
   응답 크기와 요청 간격을 fail-closed로 검증한다. Inven 권리 승인 경계는 변경하지 않는다.
6. 최근 365일 목록을 cutoff까지 읽고, update ID·제목·게시 시각·수정 시각이 달라진 상세만
   수집한다. window 밖 문서는 row를 보존하고 검색 vector만 비활성화한다.
7. 공식 청크에 `source_authority=official`을 저장하고 제한된 ranking prior 및 공식 패치 사실
   우선 생성 계약을 적용한다. 외부 본문의 지시는 authority와 무관하게 실행하지 않는다.
8. 매주 목요일 14:00 KST에 별도 lock으로 실행하고, 실패 시 Discord bot은 계속 운영한다.

### 적용 내용

- `boss-taxonomy-v1.json`: 33 base boss, 79 variant, 5 difficulty와 provenance
- `src/maple_chat/knowledge/catalog.py`, `service.py`: 전체 boss catalog와 taxonomy guard
- `src/maple_chat/crawler/nexon_updates.py`: 공식 목록/본문 parser, bounded client, rolling sync
- `src/maple_chat/indexing/service.py`, `retrieval/hybrid.py`, `qa/service.py`: official metadata,
  bounded authority prior, 생성 계약
- `maple-chat sync-patch-notes`, `scripts/sync_patch_notes.sh`,
  `scripts/weekly_patch_notes_loop.sh`: 운영 entrypoint와 목요일 scheduler
- `.env.example`, 운영·시스템·지식 그래프·교과서 문서 갱신
- 회귀 테스트: 전체 보스 수/관계, taxonomy 분류 질문, Nexon parser/URL boundary, 신규·수정·무변경·
  만료 sync, official metadata, KST 목요일 schedule

### 결과 및 Discussion

- 내장 graph는 source 7개, entity 217개, alias 755개, relation 292개로 확장됐다.
- 카링은 이지/노멀/하드/익스트림, 검은 마법사는 하드/익스트림처럼 공식 가이드에 존재하는
  조합만 반환한다.
- 당시 `데스티니는 보스야?`에서 generic `보스` taxonomy가 33개 보스로 확장되지 않고 weapon
  relation만 남도록 했다. 이 결과가 공식 데스티니 보스 모드를 누락한 사실은 SQ-010에서 발견해
  다의어 linking과 모드별 boss variant로 정정했다.
- 운영 초기 백필은 목록 4페이지에서 최근 1년 패치노트 30건을 수집하고 555개 vector를 생성했다.
- 즉시 재실행은 `details_fetched=0`, `unchanged=30`으로 상세 페이지를 다시 받지 않았다.
- PostgreSQL 운영 DB에는 공식 패치노트 synthetic board `-1001`, rolling cutoff와 latest update ID가
  저장됐다.
- 전체 검증은 188 tests, coverage 90.44%, Alembic upgrade/downgrade/upgrade, Ruff, format, Mypy,
  Bash syntax를 통과했다. 현재 환경에는 ShellCheck binary가 없어 이번 변경에서 재실행하지 못했다.
- Discord bot을 PID `872564`, 일일 Inven loop를 PID `872440`, 주간 공식 패치노트 loop를 PID
  `872441`로 재기동했다. bot의 PostgreSQL loopback 연결과 Discord Gateway TLS 연결을 확인했고,
  다음 주간 실행 시각은 2026-08-27 14:00 KST다.
- 공식 보스 catalog 자체의 upstream 변경 자동 diff/승인은 아직 없다. 패치노트 자동 수집이 새
  보스 변경을 corpus에 빠르게 반영하지만 graph node 변경은 검수된 catalog 갱신이 필요하다.
- 이 범위의 “전체 보스”는 공식 `보스별 주요 보상` 가이드의 상시 보스와 별도 확인된 시즌 보스를
  뜻한다. 필드 보스·이벤트 몬스터까지 무제한으로 boss entity로 간주하지 않는다.

### 종료 조건

- 공식 가이드 33개 base boss와 79개 variant가 DB에서 active다.
- 특정 보스 질문은 다른 보스 graph/evidence로 대체되지 않는다.
- 최근 365일 공식 패치노트가 초기 색인되고 같은 입력의 재실행이 detail fetch 0건이다.
- 매주 목요일 14:00 KST scheduler가 bot과 함께 재기동된다.
- 전체 테스트·정적 검사와 운영 프로세스/DB 스모크가 통과한다.

---

## SQ-2026-08-25-010 — 데스티니 해방 퀘스트 보스 모드 누락

- 상태: `monitoring`
- 심각도: `S2`
- 분류: 온톨로지 완전성, 다의어 entity linking, 공식 패치 provenance
- 발견 경로: 사용자 사실 정정

### 문제 정의

- 질문/조건: 데스티니 난이도 또는 데스티니 해방 퀘스트 보스를 질문한다.
- 실제 결과: 기존 graph는 `데스티니`를 무기와 초월 체계로만 연결하고 보스 난이도 의미가 없다고
  단정했다.
- 기대 결과: 데스티니 무기 의미는 보존하면서, 해방 퀘스트에서 세렌·칼로스·카링에 적용되는
  데스티니 보스 모드와 각 특수 조건도 공식 provenance로 조회되어야 한다.
- 사용자 영향: 실제 존재하는 콘텐츠를 없다고 답하고, 무기와 보스 모드를 같은 이름 때문에 잘못
  분류할 수 있다.

### 원인 분석

#### 확인된 사실

1. 넥슨 클라이언트 1.2.401 업데이트는 데스티니 무기 퀘스트를 추가했다.
2. 같은 문서는 결전 퀘스트에서 보스를 `데스티니 모드`로 1인 격파한다고 명시한다.
3. 결전, 선택받은 세렌은 최종 데미지 80% 감소, 결전, 감시자 칼로스는 데스 카운트 3,
   결전, 사도 카링은 최종 데미지 20% 증가 조건을 가진다.
4. 따라서 `데스티니`는 무기와 보스 콘텐츠 모드에 함께 쓰이는 다의어이며 어느 한 의미를
   삭제해서는 안 된다.

#### 추론

- 기존 설계는 표면어 하나가 entity 하나에만 연결된다는 단의어 가정을 로더와 linker에 사실상
  강제했다. 이 category error가 공식 보스 모드의 누락으로 이어졌다.

### 해결 방안

1. `데스티니`만 검수된 intentional ambiguous alias로 선언해 같은 span의 `weapon`과
   `content_mode` candidate를 모두 보존한다.
2. 데스티니를 base boss로 만들지 않고, 세렌·칼로스·카링의 `boss_variant`가
   `uses_mode → 데스티니 모드`, `variant_of → base boss`로 연결되게 한다.
3. 특수 보정은 `challenge_condition` entity와 `has_condition` 관계로 분리한다.
4. 결전 퀘스트는 `requires → 해당 boss_variant`로 연결한다.
5. catalog 간 base boss 참조는 선언된 `external_entity_ids`만 허용하고 DB 동기화 시 실제 활성
   entity 존재를 검증한다.
6. 생성 prompt가 `weapon`, `content_mode`, `boss_variant`를 구분하고 문맥에 따라 의미를 선택하되
   다른 의미가 존재하지 않는다고 단정하지 못하게 한다.

### 적용 내용

- `destiny-weapon-v1.json`: 공식 1.2.401 source, 데스티니 mode, 3개 boss variant, 3개 조건,
  3개 결전 퀘스트와 관계 추가
- `knowledge/catalog.py`: 검수 다의 alias와 외부 entity 선언 schema
- `knowledge/service.py`: 동일 span의 서로 다른 entity 보존, 외부 참조 활성성 검증
- `qa/service.py`: `uses_mode`, `has_condition` 의미와 다의어 생성 계약
- 회귀 테스트: bare 데스티니 다중 entity, 긴 `데스티니 난이도` mode 우선, 세렌 조건 exact
  traversal, 전체 graph cardinality

### 결과 및 Discussion

- 운영 graph는 source 7개, entity 217개, alias 755개, relation 292개로 동기화됐다.
- `데스티니 세렌 조건 알려줘` 운영 DB 조회는 세렌 variant의 `variant_of`, `uses_mode`,
  `has_condition`과 결전 퀘스트 `requires` 네 관계만 반환했다.
- 기존 데스티니 무기 entity와 성장 관계는 삭제하지 않았으므로 무기 질문도 계속 답할 수 있다.
- 자동 검증은 전체 193 tests, coverage 90.35%, Alembic 왕복, Ruff/format/Mypy를 통과했다.
- 남은 위험은 향후 다른 데스티니 해방 퀘스트 단계나 조건이 추가될 때 수동 검수 catalog 갱신이
  필요하다는 점이다. 공식 패치노트는 자동 수집되지만 graph 자동 승격은 의도적으로 하지 않는다.

### 종료 조건

- 데스티니 무기와 보스 모드가 모두 보존된다.
- 데스티니 모드 보스가 세렌·칼로스·카링 base entity에 정확히 연결된다.
- 세 보스의 특수 조건과 퀘스트 provenance가 정확히 반환된다.
- 전체 회귀와 운영 DB smoke가 통과한다.

---

## SQ-2026-08-25-011 — Discord 답변의 토큰 처리량·지연 관측 불가

- 상태: `monitoring`
- 심각도: `S3`
- 분류: 생성 성능, 사용자 가시성, 구조화 운영 로그
- 발견 경로: 사용자 기능 요청

### 문제 정의

- 질문/조건: 봇이 일반 또는 RAG 답변을 생성한다.
- 실제 결과: 답변과 로그에 입력·출력 토큰 수, TPS, 전체 처리 시간이 없어 느린 응답의 원인이
  긴 prompt인지 생성 속도인지 검색 단계인지 구분할 수 없었다.
- 기대 결과: 생성 답변 하단과 구조화 로그에 입력/출력/총 토큰, 입력/출력 TPS, 전체 처리 시간을
  동일한 정의로 제공해야 한다.
- 사용자 영향: 성능 회귀를 체감으로만 판단하며 답변별 비용과 병목을 비교할 수 없다.

### 해결 방안

1. 로컬 OpenAI 호환 호출을 usage 포함 SSE streaming으로 전환한다.
2. 입력 유효 TPS는 `prompt_tokens / TTFT`, 출력 TPS는
   `completion_tokens / (stream 종료 - 첫 content 시각)`으로 정의한다.
3. QA 진입부터 DB 근거 저장 완료까지 `total_seconds`를 측정한다.
4. canonical 명칭 repair로 모델이 여러 번 호출되면 토큰과 구간 시간을 모두 합산한다.
5. 모델 생성이 실제로 있었을 때만 사용자 footer를 붙이고, retrieval-only는 0 token footer를
   오해하게 만들지 않는다.
6. `discord_answer_completed` JSON 로그에는 숫자 지표만 남기고 질문·답변 원문·사용자 ID·인증
   token은 남기지 않는다.

### 적용 내용

- `llm/client.py`: `GenerationResult`, `GenerationMetrics`, SSE parser, usage 검증, TTFT/stream timing
- `qa/service.py`: 다중 호출 집계, `AnswerProcessingMetrics`, Discord footer, end-to-end timer
- `runtime.py`: 답변 완료 JSON event와 bot 시작 시 JSON logging 구성
- `operations.md`: 지표의 수학적 정의, 프라이버시와 해석 범위
- 회귀 테스트: SSE usage 계약·usage 누락 fail-closed·TPS 계산·footer·로그 숫자 보존

### 결과 및 Discussion

- 실제 운영 vLLM smoke에서 usage 포함 stream이 정상 동작했고 20 input, 2 output, 총 22 token을
  반환했다. 측정 예시는 입력 68.34 tok/s, 출력 8.93 tok/s, 모델 요청 0.517초였다. 이는 짧은
  단일 요청의 순간값이며 성능 기준선으로 일반화하지 않는다.
- 사용자 footer에는 입력/출력/총 token, 입력 TPS(TTFT 기반), 출력 TPS, QA 전체 시간이 표시된다.
- 로그에는 추가로 모델 호출 수와 모델 합산 시간도 기록한다.
- 입력 TPS는 queue·HTTP·prefill을 포함한 end-to-end 유효값이며 GPU kernel 전용 throughput이
  아니다. 출력 TPS도 마지막 usage chunk 전달 overhead를 포함한다.
- 자동 검증은 전체 193 tests, coverage 90.35%, Alembic 왕복, Ruff/format/Mypy를 통과했다.
- 운영 봇은 새 코드로 재시작했고 PostgreSQL 및 Discord Gateway 연결을 확인했다. 실제 Discord
  질문 1건으로 footer와 `discord_answer_completed` 한 쌍을 대조하는 사용자 실사용 확인은 남아 있다.

### 종료 조건

- 생성 응답 footer와 완료 로그가 같은 token cardinality를 보고한다.
- 다중 생성 호출은 숨김없이 합산된다.
- 로그에 질문·답변·인증 token·원문 Discord ID가 포함되지 않는다.
- 실제 Discord 질의에서 footer와 JSON event가 확인된다.

---

## SQ-2026-08-25-012 — 공식 부재 중심 결론과 고정 보고서형 제목

- 상태: `monitoring`
- 심각도: `S2`
- 분류: RAG 근거 해석, 수치 의미론, 응답 표현
- 발견 경로: Discord 실사용 실패 사례

### 문제 정의

- 질문/조건: 사용자가 이지 벨로나의 평균적인 최소 환산을 묻는다.
- 실제 결과: 검색 결과에 `환산 6.8 기준` 클리어 경험이 있었지만 답변 첫 문장은 공식 평균이
  없다는 사실만 제시했다. 이후에도 `핵심 결론`, `근거 현황`이라는 고정 제목을 반복했고,
  별도 글의 `102%` 보스 배율을 절대 환산 스탯과 같은 종류처럼 표현했다.
- 기대 결과: 직접 관련된 커뮤니티 성공 관찰값을 출처 성격·조건·표본 수와 함께 먼저 답하고,
  평균이나 보편 최소컷으로 일반화할 수 없다는 한계는 그 다음에 설명해야 한다. 제목이 필요하면
  실제 내용에 맞는 제목을 사용하고 서로 다른 수치 단위를 분리해야 한다.
- 사용자 영향: 검색이 성공했는데도 답변이 사실상 “모른다”로 읽히며, 서로 다른 지표를 같은
  최소컷 자료로 오해할 수 있다.

### 원인 분석

#### 확인된 사실

1. 검색 1위 글 `이지 벨로나 후기`에는 환산 6.8 기준으로 1페이즈 약 4분, 2페이즈 약 6분에
   클리어한 직접 경험이 포함되어 있었다.
2. 검색 2위 글의 `102%`는 가능 여부를 묻는 질문에 적힌 보스 배율이며 클리어 성공 기록이 아니다.
3. 절대 환산 스탯 6.8과 퍼센트형 보스 배율 102%는 단위와 의미가 달라 비교하거나 평균낼 수 없다.
4. 기존 생성 스타일은 “먼저 핵심 결론”이라고 지시해 모델이 이를 고정 제목으로 복제하기 쉬웠다.

#### 추론

- 생성 계약이 관찰 가능한 커뮤니티 답변보다 공식성·보편성의 부재를 먼저 강조해, 근거의
  불확실성을 정직하게 표시하는 것과 유용한 관찰값을 답하는 것을 잘못된 양자택일로 만들었다.
- 검색 문서의 질문/성공 여부 및 수치 단위를 구조적으로 추출하는 단계가 아직 제한적이므로,
  prompt 규칙만으로 모든 모호한 수치를 완전히 분류할 수는 없다.

### 해결 방안

1. 첫 문단을 질문에 대한 직접 답변 1~2문장으로 제한하고 `핵심 결론`, `근거 현황`, `참고 사항`
   같은 고정 제목을 금지한다.
2. 공식 기준이 없어도 직접 관련된 커뮤니티 성공·경험 관찰값은 `검색된 커뮤니티 사례`로 명시해
   수치·조건·표본 수와 함께 먼저 제시한다.
3. 평균 질문에서 비교 가능한 복수 관찰값이 있을 때만 해당 표본의 평균·범위를 말한다. 직접 성공
   사례가 한 건이면 전체 평균은 추정하지 않되 그 한 건의 관찰값은 숨기지 않는다.
4. 성공 보고, 실패, 가능 여부를 묻는 질문의 수치, 현재 계산값과 계획값을 서로 분리한다.
5. 절대 환산 스탯과 퍼센트형 보스/스카우터 배율은 비교·평균하지 않고, 퍼센트 값을
   `환산 102%`라고 부르지 못하게 한다.

### 적용 내용

- 변경 파일: `src/maple_chat/qa/service.py`, `tests/unit/test_qa_prompt.py`
- 회귀 테스트: 커뮤니티 관찰 우선 답변, 단일 표본의 평균 제한, 수치 단위 분리, 고정 제목 금지와
  내용 기반 제목 계약

### 결과 및 Discussion

- 자동 검증: QA prompt 단위 테스트 13개, 전체 194 tests, coverage 90.35%, Alembic 왕복,
  Ruff format/check와 Mypy를 통과했다.
- 실사용 재검증: 실패 답변 `4d6beed3-eaf4-428f-bed1-20358404a567`에 저장된 동일 검색 근거로
  로컬 모델을 다시 생성했다. 새 답변은 첫 문장에서 커뮤니티 성공 사례 1건과 환산 6.8을
  제시하고, 102%는 성공 기록이 아닌 가능 여부 질문의 보스 배율로 분리했으며, 단일 표본으로
  전체 평균을 확정할 수 없다고 설명했다. 고정 보고서형 제목은 생성되지 않았다. 운영 봇도 새
  코드로 재시작해 Discord Gateway 재연결과 일일·주간 수집 loop 재기동을 확인했다.
- 남은 위험: 게시판 일반 글에 질문형 문장이 들어간 경우 이를 `question_only`로 분류하는 구조화
  claim classifier가 아직 없다. 이번 규칙은 질문에 적힌 수치를 성공값으로 승격하지 못하게 하지만,
  장기적으로는 검색 후 claim type과 metric type을 구조화하는 단계가 필요하다.

### 종료 조건

- 이지 벨로나 질의가 환산 6.8 직접 클리어 관찰을 첫 문단에서 답한다.
- 한 건을 평균으로 일반화하지 않고, 102%를 환산 스탯으로 표현하지 않는다.
- 고정 제목이 사라지고 필요할 때만 내용 기반 제목이 생성된다.
- 전체 회귀·정적 검사와 운영 봇 재시작이 통과한다.

---

## 새 실패 항목 템플릿

```markdown
## SQ-YYYY-MM-DD-NNN — 짧은 제목

- 상태: `reported`
- 심각도: `S1|S2|S3|S4`
- 분류:
- 발견 경로:

### 문제 정의
- 질문/조건:
- 실제 결과:
- 기대 결과:
- 사용자 영향:

### 원인 분석
#### 확인된 사실
#### 추론

### 해결 방안

### 적용 내용
- 변경 파일:
- 회귀 테스트:

### 결과 및 Discussion
- 자동 검증:
- 실사용 재검증:
- 남은 위험:

### 종료 조건
```
