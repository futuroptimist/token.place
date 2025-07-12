import pytest
from encrypt import pkcs7_pad, pkcs7_unpad

@pytest.mark.parametrize("data", [b"test", b"" , b"1234567890abcdef"*2])
def test_pad_unpad_roundtrip(data):
    padded = pkcs7_pad(data, 16)
    assert len(padded) % 16 == 0
    result = pkcs7_unpad(padded, 16)
    assert result == data


def test_unpad_invalid_padding():
    # invalid sequence: last byte value 3 but previous bytes not all 3
    padded = b"invalidpadding" + b"\x01\x02\x03"
    with pytest.raises(ValueError):
        pkcs7_unpad(padded, 16)


def test_unpad_padding_too_long():
    """Raise error when padding length byte exceeds block size."""
    padded = b"abc" + b"\x11" * 17  # block size 16, padding byte 17 (>16)
    with pytest.raises(ValueError):
        pkcs7_unpad(padded, 16)
