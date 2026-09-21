"""Fail-closed crawler circuit and Retry-After handling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


class CrawlCircuitOpen(RuntimeError):
    pass


@dataclass(slots=True)
class CrawlCircuit:
    threshold: int = 2
    consecutive_denials: int = 0
    reason: str | None = None

    @property
    def open(self) -> bool:
        return self.reason is not None

    def require_closed(self) -> None:
        if self.reason is not None:
            raise CrawlCircuitOpen(f"crawler circuit is open: {self.reason}")

    def record_status(self, status_code: int) -> None:
        if status_code in {403, 429}:
            self.consecutive_denials += 1
            if self.consecutive_denials >= self.threshold:
                self.reason = f"repeated_{status_code}"
        elif 200 <= status_code < 400:
            self.consecutive_denials = 0

    def record_captcha(self) -> None:
        self.reason = "captcha"

    def record_parser_drift(self) -> None:
        self.reason = "parser_drift"


def retry_after_seconds(value: str | None, *, now: datetime, cap_seconds: int = 3600) -> int | None:
    if value is None:
        return None
    try:
        seconds = int(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value).astimezone(UTC)
            seconds = max(0, int((retry_at - now.astimezone(UTC)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0, min(cap_seconds, seconds))
