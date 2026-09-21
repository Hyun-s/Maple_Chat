from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from maple_chat.crawler.nexon_updates import (
    OfficialNexonClient,
    PatchNoteParserDrift,
    parse_patch_note_body,
    parse_patch_note_listing,
    validate_nexon_update_url,
)

LISTING_HTML = """
<div class="update_board"><ul><li>
  <p><a href="/news/update/811?page=1"><span>
    <em class="modify_common" data-modifytime="2026-08-20 11:22">수정2</em>
    클라이언트 1.2.418 업데이트 안내
  </span></a></p>
  <div class="heart_date"><dl><dd>2026.08.20</dd></dl></div>
</li><li>
  <p><a href="/news/update/810?page=1">클라이언트 1.2.417(3) 업데이트 안내</a></p>
  <div class="heart_date"><dl><dd>2026.07.24</dd></dl></div>
</li></ul></div>
"""

DETAIL_HTML = """
<div class="qs_text"><div class="new_board_con">
  <h2>신규 보스 : 벨로나</h2>
  <p>하드 난이도가 추가됩니다.</p>
  <script>ignore_this()</script>
</div></div>
"""


def test_official_update_parsers_preserve_provenance_and_body_structure() -> None:
    entries = parse_patch_note_listing(LISTING_HTML)

    assert [entry.remote_article_id for entry in entries] == [811, 810]
    assert entries[0].title == "수정2 클라이언트 1.2.418 업데이트 안내"
    assert entries[0].published_at == datetime(2026, 8, 19, 15, 0, tzinfo=UTC)
    assert entries[0].source_modified_at == datetime(2026, 8, 20, 2, 22, tzinfo=UTC)
    assert entries[1].source_modified_at is None
    body = parse_patch_note_body(DETAIL_HTML)
    assert "신규 보스 : 벨로나" in body
    assert "하드 난이도가 추가됩니다." in body
    assert "ignore_this" not in body


def test_official_update_parsers_fail_closed_on_structure_drift() -> None:
    with pytest.raises(PatchNoteParserDrift):
        parse_patch_note_listing("<html>empty</html>")
    with pytest.raises(PatchNoteParserDrift):
        parse_patch_note_body("<div>wrong container</div>")


def test_official_client_rejects_hosts_paths_and_unsafe_redirects() -> None:
    validate_nexon_update_url("https://maplestory.nexon.com/News/Update?page=1")
    validate_nexon_update_url("https://maplestory.nexon.com/news/update/811")
    for invalid in (
        "http://maplestory.nexon.com/News/Update",
        "https://evil.example/News/Update",
        "https://maplestory.nexon.com/Community/Free",
        "https://maplestory.nexon.com/News/Update?page=0",
    ):
        with pytest.raises(ValueError, match="official update URL"):
            validate_nexon_update_url(invalid)


async def test_official_client_get_is_bounded_and_html_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, headers={"content-type": "text/html"}, text=LISTING_HTML)

    async with OfficialNexonClient(
        "MapleChat test collector",
        minimum_delay_seconds=0,
        transport=httpx.MockTransport(handler),
    ) as client:
        assert "update_board" in await client.get_html(
            "https://maplestory.nexon.com/News/Update?page=1"
        )
