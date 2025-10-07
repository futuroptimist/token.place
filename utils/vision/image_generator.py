"""Deterministic local image generation utilities."""

from __future__ import annotations

import base64
import hashlib
import io
import random
import textwrap
from dataclasses import dataclass
from typing import Optional, Tuple

from PIL import Image, ImageDraw, ImageFont


class ImageGenerationError(RuntimeError):
    """Raised when local image generation fails."""


@dataclass
class LocalImageGenerator:
    """Generate simple placeholder imagery for prompts.

    The generator intentionally produces lightweight PNGs so unit tests can
    validate OpenAI-compatible image responses without requiring heavyweight
    Stable Diffusion or Flux runtimes.
    """

    default_size: Tuple[int, int] = (512, 512)

    def generate(
        self,
        prompt: str,
        *,
        width: int | None = None,
        height: int | None = None,
        seed: Optional[int] = None,
    ) -> str:
        """Return a base64-encoded PNG depicting the provided prompt."""
        if not isinstance(prompt, str) or not prompt.strip():
            raise ImageGenerationError("Prompt must be a non-empty string")

        width = width or self.default_size[0]
        height = height or self.default_size[1]

        if width <= 0 or height <= 0:
            raise ImageGenerationError("Image dimensions must be positive")

        entropy = seed
        if entropy is None:
            digest = hashlib.sha256(prompt.encode("utf-8")).digest()
            entropy = int.from_bytes(digest[:8], "big")

        rng = random.Random(entropy)

        try:
            image = Image.new("RGB", (width, height))
            draw = ImageDraw.Draw(image)
        except Exception as exc:  # pragma: no cover - Pillow internal failures
            raise ImageGenerationError("Failed to initialise image canvas") from exc

        background = self._choose_palette(rng)
        self._paint_gradient(draw, width, height, background)
        self._overlay_prompt(draw, prompt.strip(), width, height, background)

        buffer = io.BytesIO()
        try:
            image.save(buffer, format="PNG")
        except Exception as exc:  # pragma: no cover - unexpected Pillow failure
            raise ImageGenerationError("Failed to encode PNG") from exc

        return base64.b64encode(buffer.getvalue()).decode("ascii")

    @staticmethod
    def _choose_palette(rng: random.Random) -> Tuple[Tuple[int, int, int], Tuple[int, int, int]]:
        base = tuple(rng.randint(32, 128) for _ in range(3))
        accent = tuple(min(255, value + rng.randint(64, 120)) for value in base)
        return base, accent

    @staticmethod
    def _paint_gradient(
        draw: ImageDraw.ImageDraw,
        width: int,
        height: int,
        palette: Tuple[Tuple[int, int, int], Tuple[int, int, int]],
    ) -> None:
        base, accent = palette
        for y in range(height):
            factor = y / max(height - 1, 1)
            color = tuple(
                int(base[idx] * (1 - factor) + accent[idx] * factor)
                for idx in range(3)
            )
            draw.line([(0, y), (width, y)], fill=color)

    @staticmethod
    def _overlay_prompt(
        draw: ImageDraw.ImageDraw,
        prompt: str,
        width: int,
        height: int,
        palette: Tuple[Tuple[int, int, int], Tuple[int, int, int]],
    ) -> None:
        font = ImageFont.load_default()
        max_line_length = max(12, width // 8)
        wrapped = textwrap.fill(prompt, width=max_line_length)

        bbox = draw.multiline_textbbox((0, 0), wrapped, font=font, align="center")
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]

        position = (
            max(4, (width - text_width) // 2),
            max(4, (height - text_height) // 2),
        )

        base, accent = palette
        avg = sum(base) / 3
        text_color = (255, 255, 255) if avg < 128 else (20, 20, 20)

        draw.multiline_text(
            position,
            wrapped,
            font=font,
            fill=text_color,
            align="center",
        )


__all__ = ["ImageGenerationError", "LocalImageGenerator"]
