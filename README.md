# Maple Chat

단일 private Discord guild에서 동작하는 한국어 MapleStory 근거 기반 RAG 봇입니다.

AI 연구 중심의 기획 의도, 구현 item, RAG·지식 그래프·Agent 평가 방법론과 Tool/MCP 확장 방법은
[`PROJECT_PORTFOLIO.md`](PROJECT_PORTFOLIO.md)에 포트폴리오 형태로 정리되어 있습니다.

RAG, 정보검색 수학, 임베딩·ANN, hybrid 검색, PostgreSQL, 지식 그래프, 평가, 보안과 운영을
기초부터 전문가 수준까지 공부하려면
[`docs/rag-knowledge-graph-textbook.md`](docs/rag-knowledge-graph-textbook.md)를 읽으세요. 실제
실패 사례를 전체 이론과 시스템 설계에 연결한 프로젝트 교과서입니다.

실사용 품질 실패와 해결·검증 이력은
[`docs/service-quality-log.md`](docs/service-quality-log.md)에 누적합니다.
현재 RAG·PostgreSQL·운영 상태와 지식 그래프 구현 여부는
[`docs/current-system-overview.md`](docs/current-system-overview.md)에 정리되어 있습니다.
직업 계층·아이템 약어 지식 그래프의 이론, 온톨로지, DB 구조, RAG 결합 방식은
[`docs/knowledge-graph.md`](docs/knowledge-graph.md)에 정리되어 있습니다.
수집 데이터에서 공식 명칭 차이를 계속 검사하는 결과와 한계는
[`docs/knowledge-audit.md`](docs/knowledge-audit.md)에 정리되어 있습니다.
실제 local Qwen·RAG·Agent·Discord 멘션 E2E 결과는
[`docs/e2e-verification-2026-09-02.md`](docs/e2e-verification-2026-09-02.md)에 정리되어 있습니다.
권리 승인 기반 Inven 라이브 수집, 개인정보 정제, 이미지 OCR, BGE-M3 색인/검색,
로컬 Qwen vLLM 생성, Discord 답변과 요청자 전용 출처 버튼까지 연결되어 있습니다.

## 한 번에 실행

봇을 Discord 서버에 추가한 뒤 저장소에서 다음을 실행합니다.

```bash
scripts/setup_local.sh
cp .env.example .env
chmod 600 .env
```

`.env`에서 아래 값을 채웁니다.

- `DISCORD_TOKEN`, `DISCORD_GUILD_IDS`, `DISCORD_OWNER_ID`, `DISCORD_CHANNEL_IDS`
  - 서버가 여러 개면 `DISCORD_GUILD_IDS`에 서버 ID를 쉼표로 구분합니다.
  - 기존 단일 서버용 `DISCORD_GUILD_ID`도 계속 지원합니다.
- `POSTGRES_PASSWORD`, `PII_HASH_SALT`
- `NEXON_API_KEY`: 공식 NEXON Open API 기반 캐릭터 Agent를 사용할 때 설정
- `NEXON_ANALYTICS_SCRIPT`: 정적 프로젝트 페이지를 Open API Analytics에 연결할 때 설정
- `CRAWLER_USER_AGENT` (`CRAWLER_CONTACT`는 선택)
- `LIVE_CRAWL_APPROVAL_FILE`: 저장소 밖 승인 JSON의 절대 경로
- 승인 범위 확인 후 `LIVE_CRAWL_ENABLED=true`

로그인 셸에 이미 설정된 `DISCORD_TOKEN` 등은 `.env`의 해당 값이 비어 있으면 그대로
사용하므로 토큰을 파일에 중복 저장할 필요가 없습니다.

그다음 아래 한 명령을 실행합니다.

```bash
scripts/run_live_bot.sh
```

이 명령은 PostgreSQL 시작/migration, 승인된 라이브 수집, OCR/BGE 색인, 로컬 vLLM
준비 확인 또는 기동, Discord 봇 실행을 순서대로 수행합니다. 수집·색인만 다시 실행하려면:

```bash
scripts/sync_live.sh
```

## RAG query modes

[`HKUDS/RAG-Anything`](https://github.com/HKUDS/RAG-Anything)의 LightRAG query mode 계약을
현재 PostgreSQL/pgvector 구조에 맞게 적용했습니다. 기본값은 graph와 community 검색을 모두
사용하는 `mix`입니다.

```text
@MapleChat --rag-mode local 메카닉은 어떤 직업군이야?
@MapleChat --rag-mode global 메카닉과 연결된 직업 체계를 요약해줘
@MapleChat --rag-mode naive 벨로나 공략 경험담을 찾아줘
@MapleChat --rag-mode mix 제논 노말 벨로나 최소컷을 알려줘
```

지원 모드는 `local`, `global`, `hybrid`, `naive`, `mix`, `bypass`입니다. 기존 `--no-rag`는
`bypass`와 같은 동작을 유지합니다. 구체적인 이식 범위와 upstream 차이는
[`docs/rag-anything-adaptation.md`](docs/rag-anything-adaptation.md)에 정리했습니다.

로컬 LLM은 기본적으로
`/home/hyuns/local-claude-code/docker-compose.qwen38-27b.yml`의 Qwen3.8-27B
`local-coder`를 `http://127.0.0.1:8001/v1`에서 사용합니다. 이미 준비되어 있지 않으면
해당 프로젝트의 `docker-compose-manager.sh up qwen38-27b` 경로로 시작합니다.

### BGE-M3 HTTP embedding

기존 동작은 `EMBEDDING_PROVIDER=local`이며 immutable commit으로 고정한 로컬
SentenceTransformer를 오프라인으로 로드합니다. DCM이 BGE-M3 TEI 서비스를 실행 중이면 다음
설정으로 동일한 revision 계약을 유지하면서 HTTP embedding을 사용할 수 있습니다.

```dotenv
EMBEDDING_PROVIDER=remote
EMBEDDING_BASE_URL=http://127.0.0.1:8081/v1
EMBEDDING_REMOTE_MODEL=bge-m3
EMBEDDING_TIMEOUT_SECONDS=60
EMBEDDING_MODEL=BAAI/bge-m3
EMBEDDING_MODEL_COMMIT=5617a9f61b028005a4858fdac845db406aefb181
```

`EMBEDDING_REMOTE_MODEL`은 TEI의 OpenAI 호환 요청에 사용하는 모델 별칭이며, immutable revision에
사용하는 `EMBEDDING_MODEL`과는 별도입니다. 클라이언트는 각 batch 전에 `/info`의 `model_id`와
`model_sha`가 설정한 model/commit과 정확히 같은지 확인한 뒤 `/v1/embeddings`에 `model`, `input`,
`encoding_format` 표준 OpenAI 요청 필드만 보냅니다. 응답의 순서, 개수, 유한값,
1024차원, L2 정규화를 검증합니다. OpenAI-compatible 응답 자체는 Hugging Face commit을 증명하지 않으므로
Maple Chat은 TEI `/info` attestation이 실패하면 embedding 요청을 보내지 않습니다. 원격 오류가 발생했을 때
GPU 로컬 모델을 갑자기 로드하지 않도록 자동 fallback은 하지 않습니다. 명시적으로
`EMBEDDING_PROVIDER=local`로 되돌리는 것이 안전한 fallback입니다.

호스트에서 실행하는 Maple Chat은 위 loopback URL을 사용합니다. 앱을 컨테이너에서 실행할 때는
Compose의 app 서비스가 외부 `dcm-model-plane` network에 참여하고 기본값
`http://dcm-embedding:80/v1`로 DCM의 정확한 DNS alias를 사용합니다. DCM이 이 network를 먼저
생성해야 하며, loopback으로 publish한 호스트 포트가 `host.docker.internal`에서 접근 가능하다고
가정하지 않습니다.

## NEXON Open API Agent

`NEXON_API_KEY`를 설정하면 Discord에서 다음처럼 읽기 전용 캐릭터 분석 Agent를 사용할 수
있습니다.

```text
@MapleChat --agent 캐릭터 아델유저의 보스 준비 관점에서 스탯과 장비를 요약해줘
```

Agent는 로컬 LLM이 제한된 계획을 만든 뒤 필요한 툴을 선택하고, Pydantic으로 검증된 인자만
실행합니다. 공식 캐릭터 정보 5개 툴과 community RAG, 공식 패치 검색, 지식 그래프 조회를
결합할 수 있으며 외부 상태를 변경하지 않습니다. 계획과 툴 결과는 PostgreSQL에 checkpoint로
저장되어 실패 후 `--agent-resume <run_id>`로 재개할 수 있습니다. 상세 계약은
[`docs/nexon-open-api-agent.md`](docs/nexon-open-api-agent.md)를 참고하세요.

공식 Analytics 스크립트를 정적 프로젝트 페이지 `<head>`에 검증 후 삽입하려면 다음을
실행합니다. 허용된 NEXON HTTPS 호스트와 Analytics 경로가 아니면 파일을 변경하지 않습니다.

```bash
conda run -n maple-chat python scripts/configure_nexon_analytics.py
```

서면 허가 또는 법률 검토 기록에 `full_backfill` 범위가 포함된 경우, 7개 게시판 전체를
최신 페이지부터 체크포인트 기반으로 수집·색인하려면 다음을 실행합니다.

```bash
scripts/run_full_backfill.sh
```

중단 후 같은 명령을 다시 실행하면 게시판별 저장된 다음 페이지부터 재개합니다. 승인 파일이
없거나 `full_backfill` 범위가 없으면 PostgreSQL 또는 Inven 네트워크 요청 전에 중단됩니다.
기본 수집 순서는 각 게시판 10페이지씩 순환하며, 모든 게시판을 200페이지까지 수집합니다.
범위는 `FULL_BACKFILL_PAGE_BATCH`와 `FULL_BACKFILL_TARGET_PAGE`로 조정할 수 있습니다.

봇이 실행 중이면 기본적으로 매일 05:00 KST에 동일한 권리 게이트를 거쳐 7개 게시판의
신규 글을 먼저 수집합니다. 각 게시판의 최신 페이지부터 순서대로 확인하다가 페이지 전체가
이미 저장된 ID이면 신규 영역의 끝으로 판단합니다. `(board_id, remote_article_id)`로 이미
저장된 글의 상세·댓글 요청은 생략하며, 신규 수집이 모든 게시판에서 끝난 뒤에만 게시판별
과거 체크포인트를 정확히 1페이지씩 이어갑니다. 수집 후에는 로컬 vLLM을 잠시 내린 상태에서
변경된 데이터만 BGE-M3로 즉시 색인하고 기존 컨테이너를 복구합니다.

수집·색인이 끝난 동일한 KST 날짜의 커뮤니티 패치 반응을 공식 직업 카탈로그의 하위 직업마다
개별 검색·개별 요약하려면 다음을 실행합니다. 제논처럼 두 직업군에 속하는 직업은 관련 게시판을
모두 검색하되 보고서는 한 번만 만들며, 결과는 `reports/patch-reactions/YYYY-MM-DD/` 아래에
48개 개별 Markdown 파일과 진행 상황 manifest로 저장됩니다. 중단 후 재실행하면 완성된 직업
파일은 건너뜁니다.

```bash
scripts/summarize_patch_reactions.sh 2026-09-10
```

## 라이브 수집 안전 경계

- 기본값은 `LIVE_CRAWL_ENABLED=false`입니다.
- `approved_sample`은 한 게시판, 첫 페이지, 최대 5개 글로 강제 제한됩니다.
- 여러 게시판/페이지 수집은 승인 JSON에 `full_backfill` 범위가 있어야 합니다.
- 전체 백필은 `LIVE_CRAWL_RESUME=true`로 게시판별 DB 체크포인트부터 재개할 수 있습니다.
- 승인 파일, `.env`, 토큰과 salt는 커밋하거나 출력하지 않습니다.
- 반복 403/429, CAPTCHA, parser drift가 발생하면 우회하지 않고 중단합니다.
- 댓글 요청은 공개 JSON endpoint의 read-only `act=list` 동작만 허용합니다.

승인 JSON 형식은 [`docs/compliance-gate.md`](docs/compliance-gate.md), 전체 운영 절차는
[`docs/operations.md`](docs/operations.md)를 참고합니다.

## Conda와 검증

호스트 `/home/hyuns/anaconda3`의 전용 `maple-chat` 환경만 사용하며 `base`는 변경하지
않습니다. 환경에는 CUDA PyTorch, GPU BGE embedding/reranker, Tesseract `kor+eng`, OCR
adapter가 포함됩니다.

```bash
conda run -n maple-chat scripts/verify_all.sh
```

## 구현 구성

- `src/maple_chat/crawler`: 권리 정책, 라이브/fixture 수집, PII 정제, OCR/media
- `src/maple_chat/indexing`, `retrieval`: revisioned BGE-M3와 pgvector/pg_trgm 검색
- `src/maple_chat/knowledge`: 공식 직업 계층과 검수 아이템·게임 용어·효과 entity/relation 그래프
- `src/maple_chat/qa`, `llm`, `discord`, `runtime.py`: 로컬 생성과 Discord vertical slice
- `migrations`: PostgreSQL 16, pgvector, pg_trgm schema와 rollback
- `tests/evaluation`: 110개 한국어 품질/보안 회귀 세트
- `docs/single-post-qa-evaluation-protocol.md`: 실제 백필 Q&A thread 기반 retrieval·generation·기권 평가 설계

단일 게시글 평가 scorer의 oracle contract는 다음처럼 확인한다. Oracle 파일은 evaluator 검증용이며
실제 모델 성능 결과가 아니다.

```bash
maple-post-evaluate \
  --protocol tests/evaluation/backfilled-post-zero-v1.json \
  --predictions tests/evaluation/backfilled-post-zero-oracle-v1.json
```
