"""Exercise upgraded native OCR and file-cache APIs in the isolated CI environment."""

from __future__ import annotations

import multiprocessing
from importlib.metadata import version
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from cachecontrol.caches import FileCache
from filelock import FileLock, Timeout
from packaging.version import Version
from PIL import Image, ImageDraw, ImageFont
from pytesseract import get_languages

from maple_chat.crawler.media import (
    ImageMetadata,
    MediaRejected,
    TesseractOCRProvider,
    inspect_image,
    run_bounded_ocr,
    validate_image,
)


def test_upgraded_major_versions_are_actually_exercised() -> None:
    assert Version("13") <= Version(version("Pillow")) < Version("14")
    assert Version("4.2.1") <= Version(version("filelock")) < Version("5")


@pytest.mark.parametrize(
    ("format_name", "mime"), [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")]
)
def test_real_image_codecs_and_resource_lifecycle(format_name: str, mime: str) -> None:
    with Image.new("RGB", (32, 24), "white") as image:
        ImageDraw.Draw(image).rectangle((8, 8, 24, 16), fill="black")
        buffer = BytesIO()
        image.save(buffer, format=format_name)
    data = buffer.getvalue()
    metadata = inspect_image(data, mime)
    assert (metadata.width, metadata.height, metadata.byte_count) == (32, 24, len(data))
    opened = Image.open(BytesIO(data))
    try:
        opened.load()
        pixel = opened.convert("RGB").getpixel((16, 12))
        assert isinstance(pixel, tuple)
        assert sum(pixel) < 60
    finally:
        opened.close()
    with pytest.raises(ValueError, match="closed"):
        opened.getpixel((16, 12))


def test_real_decoder_and_existing_policy_fail_closed() -> None:
    with pytest.raises(MediaRejected, match="cannot be decoded"):
        inspect_image(b"not an image", "image/png")
    with pytest.raises(MediaRejected, match="byte size"):
        validate_image(ImageMetadata("image/png", 6 * 1024 * 1024, 32, 24))
    with pytest.raises(MediaRejected, match="pixel count"):
        validate_image(ImageMetadata("image/png", 100, 5000, 5000))


@pytest.mark.asyncio
async def test_real_korean_english_tesseract_round_trip() -> None:
    assert {"kor", "eng"} <= set(get_languages()), "Required OCR language data is missing"
    with Image.new("RGB", (700, 120), "white") as image:
        ImageDraw.Draw(image).text(
            (20, 20), "MAPLE TEST 123", fill="black", font=ImageFont.load_default(size=48)
        )
        buffer = BytesIO()
        image.save(buffer, format="PNG")
    data = buffer.getvalue()
    provider = TesseractOCRProvider(language="kor+eng")
    sanitized, confidence = await run_bounded_ocr(provider, data, inspect_image(data, "image/png"))
    assert "MAPLE" in sanitized.text.upper()
    assert "TEST" in sanitized.text.upper()
    assert sanitized.text.strip()
    assert 0 <= confidence <= 1


def _probe_lock(path: str, queue: Any) -> None:
    try:
        with FileLock(path, timeout=0.1):
            queue.put("acquired")
    except Timeout:
        queue.put("timeout")


def _assert_process_lock_result(path: Path, expected: str) -> None:
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    process = context.Process(target=_probe_lock, args=(str(path), queue))
    process.start()
    try:
        assert queue.get(timeout=10) == expected
    finally:
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        queue.close()
        queue.join_thread()
    assert process.exitcode == 0


def test_real_cachecontrol_consumer_and_cross_process_lock(tmp_path: Path) -> None:
    cache = FileCache(str(tmp_path / "cache"))
    cache.set("fixture", b"payload")
    assert cache.get("fixture") == b"payload"
    cache.delete("fixture")
    assert cache.get("fixture") is None
    path = tmp_path / "shared.lock"
    with FileLock(str(path)):
        _assert_process_lock_result(path, "timeout")
    _assert_process_lock_result(path, "acquired")
