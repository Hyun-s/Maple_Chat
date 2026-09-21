# RAG-Anything query architecture adaptation

## Upstream baseline

- repository: [`HKUDS/RAG-Anything`](https://github.com/HKUDS/RAG-Anything)
- reviewed revision: [`0ef67b4`](https://github.com/HKUDS/RAG-Anything/commit/0ef67b4d476b66407a03a39e5b17e756fc0aef29)
- review date: 2026-09-05
- upstream license: MIT

RAG-Anything은 LightRAG를 이용해 `local`, `global`, `hybrid`, `naive`, `mix`, `bypass`
query mode를 제공한다. Maple Chat은 이미 PostgreSQL, pgvector, pg_trgm, BGE reranker와
검수형 knowledge graph를 운영하고 있으므로 MinerU와 LightRAG 저장소 전체를 추가하지 않고
query mode의 경계와 graph context 구성 방식을 현재 파이프라인에 맞게 이식한다.

이 변경은 upstream 소스 파일을 복사한 것이 아니라 공개 query mode 계약을 독립 구현한 것이다.
따라서 기존 데이터베이스, 임베딩 revision, 출처 보존, 개인정보 정제 경계를 그대로 유지한다.

## Mode mapping

| mode | community evidence | knowledge graph | Maple Chat 동작 |
|---|---:|---:|---|
| `local` | 아니요 | 1-hop | 질문에서 exact-linked entity의 검수 관계 |
| `global` | 아니요 | 최대 2-hop | 연결된 상위·인접 entity 관계를 최대 128개로 제한 |
| `hybrid` | 예 | 최대 2-hop | 질문 기반 global graph와 dense/lexical/RRF/rerank 결과 결합 |
| `naive` | 예 | 아니요 | graph anchor도 사용하지 않는 기존 community 검색 |
| `mix` | 예 | 1-hop | 기존 graph+vector 동작에 검색 근거 본문의 entity linking을 추가; 기본값 |
| `bypass` | 아니요 | 아니요 | 검색 없이 로컬 LLM 호출 |

`global`의 hop 수와 relation 수는 고정 상한이다. 사용자가 임의 깊이를 지정할 수 없으므로
taxonomy처럼 degree가 큰 node에서도 프롬프트 크기와 DB 조회량이 무제한 증가하지 않는다.

## Public interface

Discord mention의 질문 맨 앞에서 mode를 선택한다.

```text
@MapleChat --rag-mode <local|global|hybrid|naive|mix|bypass> 질문
```

- 지정하지 않으면 `mix`다.
- `--no-rag`는 하위 호환 alias이며 `bypass`로 정규화된다.
- `--rag-mode` 값이 없거나 지원하지 않는 값이면 요청을 실행하지 않는다.
- `--agent`와 함께 사용할 수 있지만 Agent는 자체 tool plan을 사용하므로 query mode는 일반
  QA 경로에만 직접 적용된다.

## Deliberately not imported

- MinerU/Docling 문서 parser: 현재 corpus가 Inven/NEXON HTML 전용이고 별도 parser가 검증되어 있다.
- LightRAG storage: PostgreSQL이 원문, vector, graph provenance의 단일 운영 저장소다.
- RAG-Anything VLM query: OCR 코드는 있으나 운영 미디어 corpus가 아직 0건이므로 이번 단계에서는
  활성화하지 않는다.
- upstream LLM cache: 답변별 source와 relation을 현재 DB에 보존하는 감사 계약을 우선한다.

멀티모달 문서가 실제 입력 범위에 포함되면 RAG-Anything을 별도 optional adapter로 평가한다.
그 전에는 무거운 parser/storage 의존성을 운영 경로에 추가하지 않는다.
