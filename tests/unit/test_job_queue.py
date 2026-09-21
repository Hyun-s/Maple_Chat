from __future__ import annotations

from datetime import timedelta

import pytest

from maple_chat.jobs.queue import retry_delay


def test_retry_delay_is_exponential_and_bounded() -> None:
    assert retry_delay(1) == timedelta(seconds=5)
    assert retry_delay(4) == timedelta(seconds=40)
    assert retry_delay(100) == timedelta(seconds=3600)


def test_retry_delay_rejects_nonpositive_attempt() -> None:
    with pytest.raises(ValueError, match="positive"):
        retry_delay(0)
