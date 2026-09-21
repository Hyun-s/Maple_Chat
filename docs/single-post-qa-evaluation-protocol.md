# 단일 백필 게시글 기반 Q&A 평가 프로토콜

작성일: 2026-09-03  
프로토콜 ID: `zero-community-qa-2026-09-03`  
평가 manifest: [`tests/evaluation/backfilled-post-zero-v1.json`](../tests/evaluation/backfilled-post-zero-v1.json)

## 1. 목적

기존 110개 고정 fixture는 코드 회귀를 빠르게 막는 데 유용하지만 실제 백필 데이터의 검색 난이도를
대표하지 않는다. 이 프로토콜은 백필된 게시글 하나에 포함된 부모 질문 댓글과 자식 답변 댓글을
gold pair로 고정해 다음 연구 질문을 검증한다.

> 질문과 답변이 서로 다른 chunk로 저장된 긴 커뮤니티 Q&A thread에서, hybrid retriever가 질문의
> 표면형이 바뀌어도 실제 답변 댓글을 찾고 local LLM이 그 댓글에만 근거한 답을 생성하는가?

이 평가는 게임 지식의 객관적 진실성 자체보다 **검색 연결성, source faithfulness, authority
calibration, abstention**을 측정한다. 커뮤니티 답변은 공식 사실로 승격하지 않는다.

## 2. 평가 대상 선정

| 항목 | 값 |
|---|---|
| source key | `article:2294:452426` |
| 게시판 | 전사 |
| 제목 | `제로 모든 질문 다 받아드립니다` |
| 게시 시점 | 2026-06-14 |
| snapshot | 2026-09-03 |
| indexed comments | 344 |
| 재수집 여부 | 없음; 기존 PostgreSQL 백필 snapshot만 사용 |

선정 기준은 다음과 같다.

1. 명시적 Q&A 목적의 게시글이다.
2. `parent_comment_id`로 질문–답변 관계를 재구성할 수 있다.
3. 수치·순서·선택 이유처럼 원자 주장으로 분해할 수 있는 답변이 여러 개 있다.
4. 작성자와 URL을 평가 fixture에 복제하지 않고 stable source key만으로 lineage를 보존할 수 있다.

현재 pilot은 무기 강화, 시드링 선택, 스토리 진행 시점, 필터키 설정의 gold pair 4개를 사용한다.
각 답변의 원문 전체를 정답 문자열로 복제하지 않고 사람이 확인한 atomic claim과 answer source ID를
gold label로 저장한다.

## 3. 평가 조건

### 3.1 Condition A — post-restricted diagnostic

후보 chunk를 다음 범위로 제한한다.

```text
source_key == article:2294:452426
or metadata.article_source_key == article:2294:452426
```

이 조건은 한 thread 내부에서 의미적으로 맞는 답변을 연결하는 능력을 격리한다. 다른 게시글의
동일 표현이나 공식 지식 graph가 정답을 대신하는 것을 허용하지 않는다.

### 3.2 Condition B — open-corpus production

운영과 동일하게 전체 active corpus와 knowledge graph를 검색한다. 정답 댓글 외의 corroborating
source는 관찰할 수 있지만, 단일 게시글 실험의 답변은 target post의 gold answer를 실제로 찾았을
때만 retrieval hit로 센다. A와 B를 섞어 하나의 수치로 보고하지 않는다.

## 4. Query set

총 10개 case로 구성한다.

| 유형 | 개수 | 목적 |
|---|---:|---|
| direct | 2 | 원 질문과 가까운 표현의 기본 회수 |
| semantic/temporal/constraint paraphrase | 4 | 어휘가 달라도 같은 intent를 연결하는지 평가 |
| Korean typo | 1 | 형태 오류에 대한 dense–lexical 보완성 평가 |
| future unanswerable | 1 | snapshot 이후 정보를 만들어내지 않는지 평가 |
| authority unanswerable | 1 | 커뮤니티 댓글을 NEXON 공식 정책으로 오인하지 않는지 평가 |
| 추가 constraint probe | 1 | 금지 조건과 해방 후 수치를 함께 회수하는지 평가 |

Query는 gold 답변을 그대로 복사하지 않고 사용자가 실제로 쓸 법한 문장으로 다시 작성한다. 모델
학습에는 사용하지 않으며 prompt·retriever 파라미터를 수정한 뒤에는 test set을 보지 않고 별도
development pair로 튜닝해야 한다.

## 5. Gold annotation

각 answerable case는 다음 label을 갖는다.

```json
{
  "expected_answer_sources": [
    "comment:article:2294:452426:1736688"
  ],
  "required_claims": [
    ["3형"],
    ["17~18성", "17성~18성", "17성에서 18성"]
  ]
}
```

- `expected_answer_sources`: 부모 질문이 아니라 claim을 제공한 답변 댓글이다.
- `required_claims`: AND로 연결되는 atomic claim group이다.
- 한 group 안의 표현은 의미가 같은 허용 표면형으로 OR 처리한다.
- `&amp;nbsp;`처럼 중첩된 HTML entity와 공백·구두점은 scorer가 정규화한다.
- source에 없는 상식이나 최신 상태를 사람이 정답에 보충하지 않는다.

Gold 작성자와 검수자는 가능하면 분리한다. 30 pair 이상으로 확장할 때 20%를 이중 주석하고,
claim 포함 여부와 answerability에 Cohen's kappa를 함께 보고한다.

## 6. Metrics

### 6.1 Retrieval

정답 댓글 집합을 \(G_i\), 상위 \(k\)개 source를 \(R_i^k\)라 하면:

\[
AnswerSourceRecall@k = \frac{1}{N}\sum_i \mathbb{1}[G_i \cap R_i^k \neq \emptyset]
\]

\[
MRR@k = \frac{1}{N}\sum_i \frac{1}{rank_i}
\]

`source_containment_rate`는 answerable case에서 반환 source가 모두 target post에 속한 비율이다.
Condition A에서는 1.0이어야 하며, Condition B에서는 진단값으로만 보고한다.

### 6.2 Generation

- `claim_recall`: 회수된 gold atomic claim / 전체 gold atomic claim
- `complete_answer_rate`: 필요한 claim을 모두 포함한 case 비율
- `attribution_rate`: `게시글`, `댓글`, `커뮤니티`, `작성자` 중 하나로 비공식 근거임을 밝힌 비율
- human faithfulness: 답변의 검증 가능 주장 중 source가 entail하는 주장 비율

문자열 claim scorer는 회귀 검사용이다. 최종 연구 결과에서는 두 명의 사람이 blind condition으로
correct / partial / incorrect와 supported / unsupported를 판정한다. LLM-as-a-judge는 보조 분석으로만
사용하고, judge model·prompt·temperature·agreement를 함께 기록한다.

### 6.3 Abstention

`abstention_accuracy`는 unanswerable control에서 source를 반환하지 않고 근거 부족을 명시한 비율이다.
미래 정보나 공식성 보증 요청에 관련 댓글을 억지로 붙이면 실패다.

## 7. Pilot gate

| metric | threshold |
|---|---:|
| Answer source Recall@8 | >= 0.80 |
| MRR@8 | >= 0.60 |
| Source containment, Condition A | 1.00 |
| Atomic claim recall | >= 0.90 |
| Complete answer rate | >= 0.80 |
| Community attribution | 1.00 |
| Abstention accuracy | 1.00 |

10개 case의 pilot gate는 배포 일반화 성능을 주장하기 위한 통계가 아니다. 구조적 결함을 빠르게
드러내고 실험 코드를 고정하는 smoke threshold다. 외부 타당성을 주장하려면 게시판·직업·시점별로
최소 30개 thread, 150개 이상의 query mutation으로 확장하고 bootstrap confidence interval을
보고해야 한다.

## 8. 2026-09-03 open-corpus retrieval baseline

프로덕션 `BGE-M3 → dense/pg_trgm → RRF → BGE reranker`를 전체 corpus에 그대로 적용한 4개
무기 강화 query의 retrieval-only 결과다. 생성과 negative control은 실행하지 않았으므로 전체
프로토콜 점수로 간주하지 않는다.

| case | gold answer rank@8 | result |
|---|---:|---|
| direct | 1 | hit |
| semantic paraphrase | - | miss |
| constraint probe | 1 | hit |
| Korean typo | - | miss |

- Answer source Recall@8: **0.50**
- MRR@8: **0.50**
- 각 query에서 target post source 하나는 나왔지만, miss 두 건은 다른 sibling 댓글이었다.
- 원시 실행 trace: [`evaluation-baseline-single-post-2026-09-03.json`](evaluation-baseline-single-post-2026-09-03.json)

현재 answer comment chunk에는 부모 질문 본문 대신 게시글 본문이 `질문 문맥`으로 반복되고, 저장
문장에 중첩 HTML entity가 남아 있다. 이것이 paraphrase/typo miss의 원인일 가능성이 높지만 현재
결과만으로 인과를 확정하지 않는다. 다음 ablation은 `parent question + answer` joint chunk와 entity
정규화를 각각 독립적으로 적용해야 한다.

## 9. 실행과 재현

Scorer 자체의 oracle contract 확인:

```bash
maple-post-evaluate \
  --protocol tests/evaluation/backfilled-post-zero-v1.json \
  --predictions tests/evaluation/backfilled-post-zero-oracle-v1.json
```

Prediction 형식:

```json
{
  "case-id": {
    "sources": ["comment:article:..."],
    "answer": "모델 답변"
  }
}
```

Oracle prediction은 scorer가 모든 차원을 올바르게 집계하는지 검증하는 fixture이지 모델 성능
결과가 아니다. 실제 실험에서는 다음 항목을 run artifact에 고정한다.

- protocol ID와 Git revision
- DB snapshot date, active chunk/embedding revision
- embedding/reranker/LLM model과 immutable revision
- retrieval condition A 또는 B
- top-k, candidate limit, rerank limit, threshold
- raw source keys, rank, score, answer, token/latency metrics
- 자동 scorer 결과와 human judgment

## 10. 다음 실험 순서

1. 현 구조 baseline을 10개 전체 case에 실행한다.
2. nested HTML entity 정규화만 적용한 뒤 재측정한다.
3. answer chunk에 `parent question + child answer`를 결합하고 재색인한다.
4. dense-only, lexical-only, hybrid, reranker ablation을 수행한다.
5. Condition A 개선이 Condition B의 전체 Korean suite를 악화시키지 않는지 확인한다.
6. 여러 thread로 확장하기 전 failure taxonomy와 annotation guide를 고정한다.

이 순서를 지키면 retrieval 개선과 generation prompt 보정을 혼동하지 않고, 어떤 변경이 실제로
질문–답변 연결성을 높였는지 설명할 수 있다.
