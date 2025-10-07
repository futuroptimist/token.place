import base64

import pytest

from utils.vision.image_generator import ImageGenerationError, LocalImageGenerator


def test_local_image_generator_requires_prompt():
    generator = LocalImageGenerator()

    with pytest.raises(ImageGenerationError):
        generator.generate("  ")


def test_local_image_generator_rejects_non_positive_dimensions():
    generator = LocalImageGenerator()

    with pytest.raises(ImageGenerationError):
        generator.generate("hello", width=-1)


def test_local_image_generator_uses_seed_for_determinism():
    generator = LocalImageGenerator()

    first = generator.generate("deterministic", seed=123, width=64, height=64)
    second = generator.generate("deterministic", seed=123, width=64, height=64)

    assert first == second
    binary = base64.b64decode(first, validate=True)
    assert binary.startswith(b"\x89PNG")
