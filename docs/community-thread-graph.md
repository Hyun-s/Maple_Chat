# 게시글·댓글 관계 그래프

커뮤니티 그래프는 `articles`와 `comments`의 기존 관계를 조회 시 노드·간선으로 구성한다.
별도 그래프 DB나 원문 복제, 전체 임베딩 재생성은 필요하지 않다. 기존 수집 데이터와 이후
신규 데이터 모두 같은 조회 경로를 사용한다.

- 게시글 노드: `article:<board_id>:<remote_article_id>`
- 댓글 노드: `comment:article:<board_id>:<remote_article_id>:<remote_comment_id>`
- `게시글 → has_comment → 댓글`: 댓글이 속한 게시글
- `댓글 → reply_to → 부모 댓글`: 대댓글이 응답한 대상

이 관계는 수집된 구조적 사실이다. 동의·반박·정답 판정이나 직업·보스·아이템 언급 추출은
포함하지 않으며, 검수된 `knowledge_relations`의 공식 게임 지식으로 승격하지 않는다.

## 답변 검색

`DatabaseHybridRetriever`는 검색·재순위화가 끝난 후보에 `metadata.thread_graph`를 붙인다.
댓글 후보는 원문과 최대 4단계 부모 댓글을, 게시글 후보는 원문과 ID 순 최대 3개 댓글을
포함한다. 노드 본문은 400자로 제한한다. 추가 노드는 검색 점수가 있는 독립 근거가 아니라
대명사·질문 조건을 해석하는 문맥이며, 선택된 후보의 주장 본문과 순위는 유지한다.

운영 답변에는 `indexed` 상태의 게시글·댓글만 연결한다. 삭제·차단된 데이터와 다른 게시글의
부모 댓글은 포함하지 않는다. 순환 관계는 응답 간선에서 제외하고 `incomplete=true`로 표시한다.
깊이·개수 제한 또는 부모 누락도 표시하며 본문 잘림은 노드의 `text_truncated`로 표시한다.
전체 스레드 조회는 댓글 개수 제한 안의 부분 그래프이며, 댓글 지정 조회는 해당 댓글의
조상 경로이다. 모든 형제 댓글을 읽었다는 의미는 아니다.

그래프 문맥은 응답 시 조회되는 값으로 별도 스냅샷을 저장하지 않는다. 노드별 source key,
content hash, URL을 제공하지만 이후 원문 변경 뒤 동일한 문맥을 완전히 재현하는 영속 감사
기능은 제공하지 않는다. 기존 답변 출처 보존은 선택된 검색 청크와 공식 관계에 적용된다.

## 조회·검증

환경을 로드한 후 다음 읽기 전용 CLI로 JSON을 확인할 수 있다. ID는 원격 Inven ID다.

```bash
maple-chat thread-graph --board 2300 --article <게시글ID> --max-comments 32
maple-chat thread-graph --board 2300 --article <게시글ID> --comment <댓글ID> --indexed-only
```

CLI 기본값은 정제 완료(`sanitized`)와 색인 완료(`indexed`) 노드를 모두 표시한다. 각 노드의
`status`로 임베딩 대기 여부를 확인하며, 이를 완료 건수로 해석하면 안 된다.

`scripts/verify_thread_graph.py`는 운영 DB의 대댓글 관계와 실제 임베딩·검색·reranker·LLM을
사용하는 QA 경로를 검사한다. 테스트 답변 DB 트랜잭션은 롤백하며 Discord 메시지는 보내지
않는다. Discord Gateway 연결 확인과 실제 사용자 멘션 수신·전송 테스트는 별도 검증이다.

단위 테스트는 순환·삭제·차단·교차 게시글·프롬프트 신뢰 경계를 검증한다. 통합 테스트는
격리된 PostgreSQL에서 깊이 제한, 임베딩 대기 데이터 제외, 삭제의 즉시 반영을 검증한다.

## 2026-10-02 운영 반영 및 검증

- 단위 테스트 298개, 격리 PostgreSQL의 그래프·검색·QA·라이브 런타임 통합 테스트 20개 통과.
- Ruff 검사 및 변경 파일 포맷 검사, Mypy strict 통과.
- 실제 DB의 대댓글 그래프: 노드 3개, 간선 3개, 조회 약 0.08초.
- 실제 모델 QA: 커뮤니티 그래프를 포함한 근거 2개/3개로 각각 생성 완료. 관측된 총 응답 시간은
  105.75초/91.45초, 질의 임베딩은 67.33초/59.68초였다. 일반 성능 보장 수치가 아니다.
- 최초 고정 보스 질의는 그래프가 포함된 커뮤니티 근거가 없어 그래프 연결 smoke 검사에 실패했다.
  저장된 스레드 제목을 질의로 사용하는 검사에서 위의 성공 결과를 확인했다.
- 테스트 답변 트랜잭션은 롤백했고 `requester_hash=thread-graph-smoke`인 영속 답변은 0건이다.
- 16:23 KST에 새 봇의 `community_thread_graph_enabled` 및 Discord Gateway 연결 로그 확인.
  실제 Discord 사용자 멘션 전송·응답 시험은 수행하지 않았다.

기존 수동 실행 봇은 `maple-chat-bot.service`라는 사용자 systemd transient 서비스로 교체했다.
기존 셸에만 있던 Discord 토큰은 값을 출력하거나 파일에 저장하지 않고 사용자 서비스 매니저의
환경으로 전달했다. 재시작은 `systemctl --user restart maple-chat-bot.service`로 수행한다.
이 서비스는 현재 사용자 매니저에서 관리되며 **재부팅 후 자동 기동용 영구 unit 설치는 아니다.**

봇 종료 시 함께 종료되는 기존 주간 스케줄러는 `maple-chat-weekly-patch-notes.service` transient
서비스가 인계한다. 기존 패치 작업이 보유한 스케줄러 잠금이 해제될 때까지 300초마다 재시도한다.
기존 일일 수집 서비스와 그 하위 댓글 색인 프로세스는 중지하지 않았다.

임베딩 서버의 2026-09-28 시작 로그에는 `CUDA is not available`, `Using CPU instead`,
`Starting Bert model on Cpu`가 기록돼 있다. GPU가 연결된 컨테이너라도 현재 BGE-M3 모델 실행은
CPU로 전환된 상태다. 이번 변경은 GPU 복구를 포함하지 않는다. 2026-10-02 16:23 KST 부근의
댓글 상태는 색인 완료 161,801건, 대기 136,963건, 삭제 342건이었다.
