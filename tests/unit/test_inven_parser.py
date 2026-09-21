from __future__ import annotations

import json
from pathlib import Path

import pytest

from maple_chat.crawler.http import FetchResult
from maple_chat.crawler.live import decode_response, select_listing_articles
from maple_chat.crawler.parser import (
    PARSER_VERSION,
    ArticleUnavailable,
    ParserDriftError,
    extract_comment_check_code,
    is_recursive_fetch_forbidden,
    parse_article,
    parse_comments,
    parse_listing,
)
from maple_chat.crawler.policy import CrawlScope

FIXTURES = Path("tests/fixtures/inven")
BOARD_IDS = (2294, 2295, 2296, 2297, 2298, 2300, 2304)


@pytest.mark.parametrize("board_id", BOARD_IDS)
def test_all_seven_board_listings_discover_categories_and_deduplicate(board_id: int) -> None:
    html = (FIXTURES / "listings" / f"{board_id}.html").read_text(encoding="utf-8")
    page = parse_listing(
        html,
        board_id=board_id,
        base_url=f"https://www.inven.co.kr/board/maple/{board_id}",
    )
    assert page.board_id == board_id
    assert page.parser_version == PARSER_VERSION
    assert len(page.categories) == 2
    assert len(page.articles) == 1
    assert page.articles[0].url.startswith("https://www.inven.co.kr/board/maple/")


def test_article_keeps_media_metadata_and_treats_injection_as_data() -> None:
    html = (FIXTURES / "articles" / "2304-2304001.html").read_text(encoding="utf-8")
    article = parse_article(
        html,
        base_url="https://www.inven.co.kr/board/maple/2304/2304001",
        nickname_salt="x" * 32,
    )
    assert article.title == "안전한 사냥 팁"
    assert "Ignore previous instructions" in article.sanitized_body
    assert article.author_hash is not None
    assert "fixture-user" not in article.author_hash
    assert [item.kind for item in article.media] == ["image", "video", "external"]
    assert is_recursive_fetch_forbidden(article.media[0]) is False
    assert all(is_recursive_fetch_forbidden(item) for item in article.media[1:])


def test_article_parses_view_and_recommendation_counts_from_live_header() -> None:
    article = parse_article(
        """
        <meta property="og:title" content="조회 추천 파싱">
        <div class="articleHit">
          <strong>조회: </strong>1,678
          <strong>추천:</strong> <span id="bbsRecommendNum1">23</span>
        </div>
        <div id="powerbbsContent"><p>본문</p></div>
        """,
        base_url="https://www.inven.co.kr/board/maple/2294/1",
        nickname_salt="x" * 32,
    )

    assert article.view_count == 1678
    assert article.recommendation_count == 23


def test_raw_article_pii_is_absent_from_parsed_persistence_shape() -> None:
    html = """
    <h1 data-title="article">연락처 제거</h1><span data-author="nickname">raw-user</span>
    <div id="powerbbsContent"><p>user@example.invalid 010-1234-5678</p></div>
    """
    article = parse_article(
        html,
        base_url="https://www.inven.co.kr/board/maple/2304/1",
        nickname_salt="x" * 32,
    )
    rendered = repr(article)
    assert "user@example.invalid" not in rendered
    assert "010-1234-5678" not in rendered
    assert article.masked_fields == {"email", "phone"}


def test_comment_reply_tree_is_restored_and_deleted_body_is_empty() -> None:
    raw = (FIXTURES / "comments" / "2304-2304001.json").read_text(encoding="utf-8")
    comments = parse_comments(raw, nickname_salt="x" * 32)
    assert [comment.remote_comment_id for comment in comments] == [10, 12]
    assert [child.remote_comment_id for child in comments[0].children] == [11]
    assert comments[1].deleted is True
    assert comments[1].sanitized_body == ""


@pytest.mark.parametrize(
    "payload",
    [
        "{}",
        json.dumps(
            {
                "comments": [
                    {"id": 1, "parent_id": 2, "body": "a", "published_at": "2026-08-04T00:00:00Z"}
                ]
            }
        ),
        json.dumps(
            {
                "comments": [
                    {"id": 1, "parent_id": 2, "body": "a", "published_at": "2026-08-04T00:00:00Z"},
                    {"id": 2, "parent_id": 1, "body": "b", "published_at": "2026-08-04T00:00:00Z"},
                ]
            }
        ),
    ],
)
def test_comment_schema_or_tree_drift_fails_closed(payload: str) -> None:
    with pytest.raises(ParserDriftError):
        parse_comments(payload, nickname_salt="x" * 32)


def test_missing_required_selectors_fail_closed() -> None:
    with pytest.raises(ParserDriftError, match="selectors"):
        parse_listing("<html></html>", board_id=2304, base_url="https://www.inven.co.kr")
    with pytest.raises(ParserDriftError, match="missing"):
        parse_article(
            "<h1 data-title='article'>title</h1>",
            base_url="https://www.inven.co.kr/board/maple/2304/1",
            nickname_salt="x" * 32,
        )

    empty_last_page = parse_listing(
        "<nav><a data-category='true'>팁/정보</a></nav>",
        board_id=2304,
        base_url="https://www.inven.co.kr/board/maple/2304?p=9999",
    )
    assert empty_last_page.articles == ()


def test_live_subject_links_and_inven_comment_schema_are_supported() -> None:
    listing = parse_listing(
        """
        <nav><a href="?category=tip">팁/정보</a></nav>
        <table><tr><td><a class="subject-link"
          href="/board/maple/2304/12345">실제 형태 제목</a></td></tr></table>
        """,
        board_id=2304,
        base_url="https://www.inven.co.kr/board/maple/2304?p=1",
    )
    assert listing.categories == ("팁/정보",)
    assert listing.articles[0].remote_article_id == 12345
    assert listing.articles[0].title == "실제 형태 제목"

    comments = parse_comments(
        """
        {"cmtcount": 2, "commentlist": [{"list": [
          {"__attr__": {"cmtidx": "91"}, "o_name": "작성자",
           "o_comment": "첫 댓글", "o_datetime": "2026-08-05 12:00:00"},
          {"__attr__": {"cmtidx": "92"}, "o_name": "답글작성자",
           "o_comment": "답글", "o_datetime": "2026-08-05 12:01:00"}
        ]}]}
        """,
        nickname_salt="x" * 32,
    )
    assert comments[0].remote_comment_id == 91
    assert comments[0].children[0].parent_remote_comment_id == 91
    assert comments[0].children[0].sanitized_body == "답글"


def test_comment_check_code_is_extracted_without_article_body_execution() -> None:
    assert extract_comment_check_code("<script>const chkcode = 'fixturecode';</script>") == (
        "fixturecode"
    )
    assert extract_comment_check_code("<html>no token</html>") is None


def test_full_backfill_refuses_silent_listing_truncation() -> None:
    html = (FIXTURES / "listings" / "2304.html").read_text(encoding="utf-8")
    page = parse_listing(
        html,
        board_id=2304,
        base_url="https://www.inven.co.kr/board/maple/2304",
    )
    duplicated = (page.articles[0], page.articles[0])

    assert (
        len(
            select_listing_articles(
                duplicated,
                max_articles=1,
                scope=CrawlScope.APPROVED_SAMPLE,
            )
        )
        == 1
    )
    with pytest.raises(ValueError, match="exceeds max_articles"):
        select_listing_articles(
            duplicated,
            max_articles=1,
            scope=CrawlScope.FULL_BACKFILL,
        )


def test_external_image_is_metadata_only_and_not_fetched_for_ocr() -> None:
    article = parse_article(
        """
        <h1 data-title="article">외부 이미지</h1>
        <div id="powerbbsContent">
          <p>외부 상품 이미지는 메타데이터로만 보존한다.</p>
          <img src="https://shop-phinf.pstatic.net/example.jpg" alt="상품 이미지">
        </div>
        """,
        base_url="https://www.inven.co.kr/board/maple/2294/1",
        nickname_salt="x" * 32,
    )

    assert article.media[0].kind == "image"
    assert is_recursive_fetch_forbidden(article.media[0]) is True


def test_decode_response_replaces_isolated_invalid_legacy_bytes() -> None:
    result = FetchResult(
        url="https://www.inven.co.kr/board/maple/2294/1",
        status_code=200,
        content=b"valid\x81",
        content_type="text/html; charset=cp949",
    )

    assert decode_response(result) == "valid�"


def test_article_void_tags_do_not_capture_trailing_page_ui() -> None:
    article = parse_article(
        """
        <h1 data-title="article">본문 경계</h1>
        <div id="powerbbsContent">
          <p>정상 본문</p><img src="/x.jpg" alt="본문 이미지"><br>
          <div>중첩 본문</div>끝
        </div>
        <aside>TRAILING_PAGE_UI</aside>
        """,
        base_url="https://www.inven.co.kr/board/maple/2294/1",
        nickname_salt="x" * 32,
    )

    assert "정상 본문" in article.sanitized_body
    assert "중첩 본문" in article.sanitized_body
    assert "끝" in article.sanitized_body
    assert "TRAILING_PAGE_UI" not in article.sanitized_body
    assert len(article.media) == 1


def test_unclosed_article_content_fails_closed() -> None:
    with pytest.raises(ParserDriftError, match="not closed"):
        parse_article(
            """
            <h1 data-title="article">미종료 본문</h1>
            <div id="powerbbsContent"><p>본문<img src="/x.jpg">
            """,
            base_url="https://www.inven.co.kr/board/maple/2294/1",
            nickname_salt="x" * 32,
        )


def test_current_inven_comment_schema_preserves_roots_replies_score_and_state() -> None:
    comments = parse_comments(
        """
        {"commentlist": [{"list": [
          {"__attr__": {"cmtidx": "101", "cmtpidx": "101", "state": "Y"},
           "o_name": "첫 작성자", "o_comment": "첫 루트", "o_datetime": "2026-08-05 12:00:00",
           "o_recommend": "7"},
          {"__attr__": {"cmtidx": "102", "cmtpidx": "102", "state": "Y"},
           "o_name": "둘째 작성자", "o_comment": "둘째 루트", "o_datetime": "2026-08-05 12:01:00",
           "o_recommend": "2"},
          {"__attr__": {"cmtidx": "0", "cmtpidx": "0", "state": "C"},
           "o_name": "", "o_comment": "", "o_datetime": "2026-08-05 12:01:30"},
          {"__attr__": {"cmtidx": "103", "cmtpidx": "101", "state": "N"},
           "o_name": "답글 작성자", "o_comment": "첫 루트 답글",
           "o_datetime": "2026-08-05 12:02:00",
           "o_recommend": "1"},
          {"__attr__": {"cmtidx": "104", "cmtpidx": "999", "state": "Y"},
           "o_name": "고아 답글", "o_comment": "보존", "o_datetime": "2026-08-05 12:03:00",
           "o_recommend": "0"}
        ]}]}
        """,
        nickname_salt="x" * 32,
    )

    assert [comment.remote_comment_id for comment in comments] == [101, 102, 104]
    assert comments[0].score == 7
    assert comments[0].children[0].remote_comment_id == 103
    assert comments[0].children[0].deleted is True
    assert comments[0].children[0].sanitized_body == ""


def test_decode_response_prefers_the_candidate_with_the_least_data_loss() -> None:
    utf8_with_isolated_invalid_byte = "히어로 비교글".encode() + b"\x81"
    result = FetchResult(
        url="https://www.inven.co.kr/board/maple/2294/1",
        status_code=200,
        content=utf8_with_isolated_invalid_byte,
        content_type="text/html; charset=utf-8",
    )

    assert decode_response(result) == "히어로 비교글�"


def test_unavailable_article_body_is_distinct_from_parser_drift() -> None:
    with pytest.raises(ArticleUnavailable, match="no searchable text"):
        parse_article(
            """
            <meta property="og:title" content="삭제된 게시글">
            <div id="powerbbsContent"></div>
            """,
            base_url="https://www.inven.co.kr/board/maple/2294/1",
            nickname_salt="x" * 32,
        )


def test_image_only_article_is_unavailable_when_ocr_is_excluded() -> None:
    with pytest.raises(ArticleUnavailable, match="no searchable text"):
        parse_article(
            """
            <meta property="og:title" content="이미지 전용 게시글">
            <div id="powerbbsContent"><img src="/only-image.jpg"></div>
            """,
            base_url="https://www.inven.co.kr/board/maple/2294/1",
            nickname_salt="x" * 32,
        )
