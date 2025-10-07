import pytest

from api.v1.validation import ValidationError, validate_image_generation_payload


def test_validate_image_generation_payload_accepts_size_string():
    payload = {
        "prompt": "colorful mountains",
        "size": "128x256",
    }

    normalized = validate_image_generation_payload(payload)
    assert normalized["width"] == 128
    assert normalized["height"] == 256
    assert normalized["seed"] is None


@pytest.mark.parametrize(
    "size",
    ["200", "200 by 200", "20x20", "2000x2000"],
)
def test_validate_image_generation_payload_rejects_invalid_size(size):
    with pytest.raises(ValidationError):
        validate_image_generation_payload({"prompt": "test", "size": size})


def test_validate_image_generation_payload_requires_both_dimensions():
    with pytest.raises(ValidationError) as exc:
        validate_image_generation_payload({"prompt": "hi", "width": 256})

    assert "height" in exc.value.message

    with pytest.raises(ValidationError) as exc:
        validate_image_generation_payload({"prompt": "hi", "height": 256})

    assert "width" in exc.value.message


def test_validate_image_generation_payload_rejects_negative_seed():
    with pytest.raises(ValidationError) as exc:
        validate_image_generation_payload({"prompt": "hi", "seed": -1})

    assert exc.value.field == "seed"
