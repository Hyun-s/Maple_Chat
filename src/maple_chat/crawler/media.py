"""Bounded image validation and replaceable OCR boundary."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from importlib import import_module
from io import BytesIO
from typing import Any, Protocol

from maple_chat.crawler.sanitizer import SanitizedText, sanitize_text

ALLOWED_IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})


class MediaRejected(ValueError):
    pass


class OCRExecutionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ImageMetadata:
    mime_type: str
    byte_count: int
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class OCRResult:
    text: str
    confidence: float


class OCRProvider(Protocol):
    async def recognize(self, image: bytes) -> OCRResult: ...


class TesseractOCRProvider:
    """Optional local Tesseract adapter; no image bytes leave the host."""

    def __init__(
        self,
        *,
        language: str = "kor+eng",
        page_segmentation_mode: int = 6,
        command_timeout_seconds: float = 18.0,
    ) -> None:
        if (
            not language
            or not 0 <= page_segmentation_mode <= 13
            or not 0 < command_timeout_seconds <= 120
        ):
            raise ValueError("invalid Tesseract OCR configuration")
        try:
            self._image_module: Any = import_module("PIL.Image")
            self._tesseract: Any = import_module("pytesseract")
        except ImportError as exc:  # pragma: no cover - optional production dependency
            raise RuntimeError("install the 'ocr' optional dependency for local OCR") from exc
        self._language = language
        self._config = f"--psm {page_segmentation_mode}"
        self._command_timeout_seconds = command_timeout_seconds

    async def recognize(self, image: bytes) -> OCRResult:
        return await asyncio.to_thread(self._recognize_sync, image)

    def _recognize_sync(self, image: bytes) -> OCRResult:
        try:
            opened = self._image_module.open(BytesIO(image))
            try:
                opened.load()
                data = self._tesseract.image_to_data(
                    opened,
                    lang=self._language,
                    config=self._config,
                    output_type=self._tesseract.Output.DICT,
                    timeout=self._command_timeout_seconds,
                )
            finally:
                opened.close()
        except (OSError, RuntimeError, ValueError) as exc:
            raise OCRExecutionError("local OCR execution failed") from exc

        words: list[str] = []
        confidences: list[float] = []
        for raw_text, raw_confidence in zip(data["text"], data["conf"], strict=True):
            text = str(raw_text).strip()
            try:
                confidence = float(raw_confidence)
            except (TypeError, ValueError):
                continue
            if text and confidence >= 0:
                words.append(text)
                confidences.append(confidence)
        mean_confidence = sum(confidences) / len(confidences) / 100 if confidences else 0.0
        return OCRResult(" ".join(words), min(1.0, max(0.0, mean_confidence)))


def validate_image(
    metadata: ImageMetadata,
    *,
    max_bytes: int = 5 * 1024 * 1024,
    max_pixels: int = 20_000_000,
) -> None:
    if metadata.mime_type.lower() not in ALLOWED_IMAGE_MIME_TYPES:
        raise MediaRejected("image MIME type is not allowed")
    if metadata.byte_count <= 0 or metadata.byte_count > max_bytes:
        raise MediaRejected("image byte size is outside the allowed bound")
    if metadata.width <= 0 or metadata.height <= 0:
        raise MediaRejected("image dimensions must be positive")
    if metadata.width * metadata.height > max_pixels:
        raise MediaRejected("image pixel count exceeds the allowed bound")


def inspect_image(image: bytes, mime_type: str) -> ImageMetadata:
    """Read only bounded image metadata; decoded pixels are not retained."""
    try:
        image_module: Any = import_module("PIL.Image")
    except ImportError as exc:  # pragma: no cover - optional production dependency
        raise RuntimeError("install the 'ocr' optional dependency for image inspection") from exc
    try:
        opened = image_module.open(BytesIO(image))
        try:
            width, height = opened.size
        finally:
            opened.close()
    except (OSError, ValueError) as exc:
        raise MediaRejected("image cannot be decoded") from exc
    metadata = ImageMetadata(mime_type, len(image), int(width), int(height))
    validate_image(metadata)
    return metadata


async def run_bounded_ocr(
    provider: OCRProvider,
    image: bytes,
    metadata: ImageMetadata,
    *,
    timeout_seconds: float = 20.0,
) -> tuple[SanitizedText, float]:
    validate_image(metadata)
    try:
        result = await asyncio.wait_for(provider.recognize(image), timeout=timeout_seconds)
    except TimeoutError as exc:
        raise MediaRejected("OCR timed out") from exc
    except OCRExecutionError as exc:
        raise MediaRejected("OCR failed") from exc
    if not 0.0 <= result.confidence <= 1.0:
        raise MediaRejected("OCR confidence is invalid")
    return sanitize_text(result.text), result.confidence
