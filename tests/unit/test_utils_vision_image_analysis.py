"""Unit tests for the lightweight vision analysis helpers."""

import base64
import struct

import pytest

from utils.vision import image_analysis as ia


def _make_png(width: int, height: int) -> bytes:
    """Create minimal PNG bytes with the requested dimensions."""
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr_length = struct.pack(">I", 13)
    chunk_type = b"IHDR"
    dimensions = struct.pack(">II", width, height)
    remainder = b"\x08\x02\x00\x00\x00"
    return signature + ihdr_length + chunk_type + dimensions + remainder


def _make_gif(width: int, height: int) -> bytes:
    """Create minimal GIF bytes with the requested dimensions."""
    header = b"GIF89a"
    dims = struct.pack("<HH", width, height)
    return header + dims + b"\x00\x00\x00\x00"


def _make_jpeg(width: int, height: int) -> bytes:
    """Create minimal JPEG bytes that contain a SOF0 segment."""
    data = bytearray(b"\xff\xd8")  # SOI marker
    # APP0 marker with arbitrary payload
    data.extend(b"\xff\xe0\x00\x10" + b"JFIF" + b"\x00" * 12)
    # SOF0 segment declaring the dimensions
    sof_payload = b"\x08" + struct.pack(">H", height) + struct.pack(">H", width) + b"\x03\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    data.extend(b"\xff\xc0" + struct.pack(">H", len(sof_payload) + 2) + sof_payload)
    data.extend(b"\xff\xd9")  # EOI marker
    return bytes(data)


def test_strip_data_url_and_decode_errors():
    encoded = "data:image/png;base64, aGVsbG8="
    assert ia._strip_data_url(encoded) == "aGVsbG8="
    with pytest.raises(ValueError):
        ia._decode_base64_image("   ")


def test_png_and_gif_dimension_extractors():
    png_bytes = _make_png(2, 2)
    assert ia._extract_png_dimensions(png_bytes) == (2, 2)

    gif_bytes = _make_gif(3, 4)
    assert ia._extract_gif_dimensions(gif_bytes) == (3, 4)

    # Invalid headers or truncated payloads should yield graceful fallbacks
    assert ia._extract_png_dimensions(b"not a png") is None
    assert ia._extract_gif_dimensions(b"GIF00") is None


def test_jpeg_dimension_extractor():
    jpeg_bytes = _make_jpeg(5, 6)
    assert ia._extract_jpeg_dimensions(jpeg_bytes) == (5, 6)


@pytest.mark.parametrize(
    "payload",
    [
        b"\xff\xd8",  # Too short to contain a marker segment
        b"\xff\xd8\xff\xd8\x00\x00",  # Additional SOI marker should be skipped
        b"\xff\xd8\xff\xe0\x00",  # Marker without a declared length should abort
        b"\xff\xd8\xff\xe0\x00\x01",  # Declared length shorter than header
        b"\xff\xd8\xff\xc0\x00\x05abcde",  # SOF marker with insufficient payload
    ],
)
def test_jpeg_dimension_extractor_handles_incomplete_segments(payload):
    """Malformed JPEG streams should return ``None`` instead of raising."""

    assert ia._extract_jpeg_dimensions(payload) is None


def test_derive_dimensions_handles_unknown_types():
    assert ia._derive_dimensions(None, b"data") == (None, None)
    assert ia._derive_dimensions("bmp", b"data") == (None, None)

    # Known extractor returning no dimensions should propagate the fallback
    assert ia._derive_dimensions("png", b"truncated") == (None, None)


def test_analyze_base64_image_orientation_variants(monkeypatch):
    png_landscape = base64.b64encode(_make_png(4, 2)).decode()
    analysis = ia.analyze_base64_image(png_landscape)
    assert analysis["format"] == "png"
    assert analysis["width"] == 4
    assert analysis["height"] == 2
    assert analysis["orientation"] == "landscape"

    png_square = base64.b64encode(_make_png(3, 3)).decode()
    analysis = ia.analyze_base64_image(png_square)
    assert analysis["orientation"] == "square"

    png_portrait = base64.b64encode(_make_png(2, 5)).decode()
    analysis = ia.analyze_base64_image(png_portrait)
    assert analysis["orientation"] == "portrait"

    # Force an unknown type to exercise the graceful fallback path
    monkeypatch.setattr(ia.imghdr, "what", lambda *_a, **_k: None)
    analysis = ia.analyze_base64_image(png_landscape)
    assert analysis["format"] is None
    assert analysis["width"] is None
    assert analysis["height"] is None


def test_analyze_base64_image_accepts_uppercase_data_scheme():
    """Data URLs should be handled case-insensitively."""

    payload = base64.b64encode(_make_png(2, 1)).decode()
    uppercase_url = f"DATA:image/png;base64,{payload}"

    analysis = ia.analyze_base64_image(uppercase_url)

    assert analysis["format"] == "png"
    assert analysis["width"] == 2
    assert analysis["height"] == 1


def test_analyze_base64_image_handles_embedded_whitespace():
    """Base64 payloads with incidental whitespace should still decode."""

    payload = base64.b64encode(_make_png(4, 3)).decode()
    # Insert whitespace and newlines in the payload to mimic wrapped base64 output
    wrapped = f"{payload[:6]}\n  {payload[6:12]}\t{payload[12:]}  "

    analysis = ia.analyze_base64_image(wrapped)

    assert analysis["format"] == "png"
    assert analysis["width"] == 4
    assert analysis["height"] == 3


def test_summarize_analysis_variants():
    single = {"format": "png", "width": 1, "height": 2, "size_bytes": 42, "orientation": "portrait"}
    assert ia.summarize_analysis(single) == "Vision analysis: PNG image, 1x2 px, 42 bytes, portrait orientation."

    multiple = [single, {"format": None, "size_bytes": 10}]
    summary = ia.summarize_analysis(multiple)
    assert "1. PNG image" in summary
    assert "2. UNKNOWN image" in summary

    assert ia.summarize_analysis([]) == "Vision analysis unavailable."
