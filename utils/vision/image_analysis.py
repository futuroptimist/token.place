"""Helpers for analyzing base64-encoded images without external dependencies."""

from __future__ import annotations

import base64
import imghdr
import struct
from typing import Dict, List, Optional, Sequence, Tuple, Union

BytesLike = Union[bytes, bytearray, memoryview]
AnalysisRecord = Dict[str, Optional[Union[str, int]]]


def _strip_data_url(encoded: str) -> str:
    """Remove any data URL prefix from a base64 string."""
    trimmed = encoded.lstrip()
    if trimmed[:5].lower() == "data:":
        _, _, payload = trimmed.partition(",")
        return payload.strip()
    return trimmed.strip()


def _decode_base64_image(encoded: str) -> bytes:
    """Decode base64 content, validating the alphabet."""
    normalized = _strip_data_url(encoded)
    if not normalized:
        raise ValueError("Image payload is empty")
    return base64.b64decode(normalized, validate=True)


def _extract_png_dimensions(data: BytesLike) -> Optional[Tuple[int, int]]:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def _extract_gif_dimensions(data: BytesLike) -> Optional[Tuple[int, int]]:
    if len(data) < 10 or data[:6] not in {b"GIF87a", b"GIF89a"}:
        return None
    width, height = struct.unpack("<HH", data[6:10])
    return int(width), int(height)


def _extract_jpeg_dimensions(data: BytesLike) -> Optional[Tuple[int, int]]:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None

    index = 2
    length = len(data)
    while index + 1 < length:
        if data[index] != 0xFF:
            index += 1
            continue

        marker = data[index + 1]
        index += 2

        if marker in (0xD8, 0xD9):
            continue

        if index + 2 > length:
            break

        segment_length = struct.unpack(">H", data[index:index + 2])[0]
        if segment_length < 2 or index + segment_length > length:
            break

        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            if segment_length < 7:
                break
            height = struct.unpack(">H", data[index + 3:index + 5])[0]
            width = struct.unpack(">H", data[index + 5:index + 7])[0]
            return int(width), int(height)

        index += segment_length

    return None


_DIMENSION_EXTRACTORS = {
    "png": _extract_png_dimensions,
    "gif": _extract_gif_dimensions,
    "jpeg": _extract_jpeg_dimensions,
    "jpg": _extract_jpeg_dimensions,
}


def _derive_dimensions(image_type: Optional[str], data: BytesLike) -> Tuple[Optional[int], Optional[int]]:
    if not image_type:
        return None, None

    extractor = _DIMENSION_EXTRACTORS.get(image_type)
    if extractor is None:
        return None, None

    dimensions = extractor(data)
    if not dimensions:
        return None, None
    return dimensions


def analyze_base64_image(encoded: str) -> AnalysisRecord:
    """Return lightweight metadata for a base64-encoded image."""
    binary = _decode_base64_image(encoded)
    image_type = imghdr.what(None, h=binary)
    width, height = _derive_dimensions(image_type, binary)

    orientation: Optional[str] = None
    if width and height:
        if width == height:
            orientation = "square"
        elif width > height:
            orientation = "landscape"
        else:
            orientation = "portrait"

    return {
        "format": image_type,
        "width": width,
        "height": height,
        "size_bytes": len(binary),
        "orientation": orientation,
    }


def summarize_analysis(
    entries: Union[AnalysisRecord, Sequence[AnalysisRecord]],
) -> str:
    """Convert one or more analysis dicts into a human-readable description."""
    if isinstance(entries, dict):
        records: List[AnalysisRecord] = [entries]
    else:
        records = list(entries)

    if not records:
        return "Vision analysis unavailable."

    lines: List[str] = []
    for record in records:
        fmt = record.get("format") or "unknown"
        fmt_label = fmt.upper()
        segments = [f"{fmt_label} image"]

        width = record.get("width")
        height = record.get("height")
        if width and height:
            segments.append(f"{width}x{height} px")

        size_bytes = record.get("size_bytes")
        if isinstance(size_bytes, int):
            segments.append(f"{size_bytes} bytes")

        orientation = record.get("orientation")
        if isinstance(orientation, str):
            segments.append(f"{orientation} orientation")

        lines.append(", ".join(segments))

    if len(lines) == 1:
        return f"Vision analysis: {lines[0]}."

    enumerated = [f"{idx}. {line}" for idx, line in enumerate(lines, start=1)]
    return "Vision analysis:\n" + "\n".join(enumerated)
