from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from maple_chat.crawler import media
from maple_chat.crawler.circuit import CrawlCircuit, CrawlCircuitOpen, retry_after_seconds
from maple_chat.crawler.media import (
    ImageMetadata,
    MediaRejected,
    OCRExecutionError,
    OCRResult,
    TesseractOCRProvider,
    run_bounded_ocr,
    validate_image,
)


class FakeOCR:
    async def recognize(self, image: bytes) -> OCRResult:
        assert image
        return OCRResult("user@example.invalid 안전한 글자", 0.8)


class SlowOCR:
    async def recognize(self, image: bytes) -> OCRResult:
        await asyncio.sleep(1)
        return OCRResult("late", 0.5)


def valid_metadata() -> ImageMetadata:
    return ImageMetadata("image/png", 100, 10, 10)


@pytest.mark.parametrize(
    "metadata",
    [
        ImageMetadata("image/svg+xml", 100, 10, 10),
        ImageMetadata("image/png", 6 * 1024 * 1024, 10, 10),
        ImageMetadata("image/png", 100, 0, 10),
        ImageMetadata("image/png", 100, 5000, 5000),
    ],
)
def test_image_policy_rejects_unbounded_or_unsupported_media(metadata: ImageMetadata) -> None:
    with pytest.raises(MediaRejected):
        validate_image(metadata)


@pytest.mark.asyncio
async def test_ocr_is_bounded_and_sanitized() -> None:
    result, confidence = await run_bounded_ocr(FakeOCR(), b"fixture", valid_metadata())
    assert "example.invalid" not in result.text
    assert confidence == 0.8


@pytest.mark.asyncio
async def test_ocr_timeout_is_fail_closed() -> None:
    with pytest.raises(MediaRejected, match="timed out"):
        await run_bounded_ocr(
            SlowOCR(),
            b"fixture",
            valid_metadata(),
            timeout_seconds=0.001,
        )


@pytest.mark.asyncio
async def test_tesseract_provider_runs_locally_and_normalizes_confidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    class Image:
        def load(self) -> None:
            calls["loaded"] = True

        def close(self) -> None:
            calls["closed"] = True

    image_module = SimpleNamespace(open=lambda _: Image())

    def image_to_data(image: Image, **kwargs: object) -> dict[str, list[object]]:
        calls["image"] = image
        calls["kwargs"] = kwargs
        return {"text": ["메이플", "", "스토리"], "conf": ["90", "bad", 70]}

    tesseract = SimpleNamespace(
        Output=SimpleNamespace(DICT="dict"),
        image_to_data=image_to_data,
    )
    monkeypatch.setattr(
        media,
        "import_module",
        lambda name: image_module if name == "PIL.Image" else tesseract,
    )

    provider = TesseractOCRProvider(language="kor", page_segmentation_mode=6)
    result = await provider.recognize(b"fixture")

    assert result == OCRResult("메이플 스토리", 0.8)
    assert calls["loaded"] is True
    assert calls["closed"] is True
    assert calls["kwargs"] == {
        "lang": "kor",
        "config": "--psm 6",
        "output_type": "dict",
        "timeout": 18.0,
    }


def test_tesseract_provider_rejects_invalid_configuration() -> None:
    with pytest.raises(ValueError, match="invalid"):
        TesseractOCRProvider(language="", page_segmentation_mode=6)


@pytest.mark.asyncio
async def test_ocr_execution_failure_is_fail_closed() -> None:
    class BrokenOCR:
        async def recognize(self, image: bytes) -> OCRResult:
            raise OCRExecutionError("private detail")

    with pytest.raises(MediaRejected, match="OCR failed"):
        await run_bounded_ocr(BrokenOCR(), b"fixture", valid_metadata())


def test_repeated_denials_captcha_and_parser_drift_open_circuits() -> None:
    denial = CrawlCircuit()
    denial.record_status(429)
    assert denial.open is False
    denial.record_status(429)
    with pytest.raises(CrawlCircuitOpen, match="repeated_429"):
        denial.require_closed()

    captcha = CrawlCircuit()
    captcha.record_captcha()
    assert captcha.open is True
    drift = CrawlCircuit()
    drift.record_parser_drift()
    assert drift.reason == "parser_drift"


def test_retry_after_is_parsed_and_bounded() -> None:
    now = datetime(2026, 8, 4, tzinfo=UTC)
    assert retry_after_seconds("120", now=now) == 120
    assert retry_after_seconds("99999", now=now) == 3600
    assert retry_after_seconds("Tue, 04 Aug 2026 00:05:00 GMT", now=now) == 300
    assert retry_after_seconds("invalid", now=now) is None
