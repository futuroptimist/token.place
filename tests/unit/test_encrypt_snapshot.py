from __future__ import annotations

import base64
import json
from pathlib import Path
import secrets

import pytest

from encrypt import decrypt, encrypt

SNAPSHOT_PATH = Path(__file__).with_name("snapshots") / "encrypt_default_payload.json"

AES_KEY_BYTES = bytes(range(1, 33))
IV_BYTES = bytes(range(101, 117))

STATIC_PRIVATE_KEY_PEM = b"""-----BEGIN PRIVATE KEY-----
MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQCcUEoMC9NkR34i
VLn0UpVUWngNPbywvWIfBn3h0nnHmV/4qrhbFyXwI4RDeundCx4zYyQB1nVW9ZtS
ayicW7HulUtAPIT4A1CQ13grzjTNZX4KCFPPfiPErn2lvpyJ9K2jsnxbLsSqFSu7
tguGepnHP8C4cE+urRFRLt/7Jwm18Dq+P0iynQFim+DA4/3sTL6cIuJHkL/chrM5
luUdDtEJt9mo/56dwzBFsf8i+tevPr+95y4iurrR8W3Jkhuzt0BKl+5Ivpq0gAG7
oKJKVfL7oSGUR2YBdoI4+VAxiNzi7Zk2s5ROO6HdjDyEwZ7OxN9Nhl3autsZlGWS
l8psh1fRAgMBAAECggEAAcjD+eWnOyMoWMhm8Y6BMW46pd3CUx+Qkc2pqpzdEpEd
i/h0CvojKl3Fazkycub5XzSlke3M0qftwk2UsClbf/G/me6bfIT1vPc5z9x662NH
1M+gzv5NiO6OYr9yCLMYEuDq5gqxsY+ZltaTDdGOlRe5s+74Qd/Q2yUEMDSA8PGf
Gb6Xxl6ura/TlgnueA68KPYbmDMiPSMa/cSIupcLSSCOlo/yuq8gwVd1Zk1GsYGG
mnKfDbQUIPCXuE9Hb1ZpG98ydF/Z0Qjw0P/nyCjoTLIDY4rCMix00OHdSH1bwVO3
L1R236EOtPO3ZjRcJHpEcovN9/W7qJIS+zGnHydgYQKBgQDbOVlUfg0tc9GgJFwU
d58DoEZWBQXAgl8ldmiX2+Yv3rLU5/YGkDMw1mG14BQtb4PB96BYnDBRn6QU2hzg
QIY62YeZGivvlqlDHA9HVlab/jl2bIDOgWytRkzHlGPdhmC78PuGfPamH1qkJHYT
96PFKdao/crVtKowBTwNhc/fOQKBgQC2iTtg3uzksnYF/g5AoEo38lvny2YoQHGs
HbyKnWn8aDUZZIeZjDSyTPIekQ+ibZwbF3m5xLq4b9+piRyvtrWS7Gy9p5ymRFK5
wUXylIdJJNkVXoc/8PDtWWYBaovYucn4RGWVkiFZ/roqL6L09ScXkDQ2Gs8nDBRu
K4mJDHulWQKBgQCg4hWMzHUfRkAxJn3tB1zLbHQx7L2r6gGpnJxl1hu4Rdc4KIOF
jY4D6VEMCMbGEXDAiNpELVvIiz//jZJjgPcBeWLqGSrlScwuVAlicRpeoNPK9RYS
dykqgM0YKu6fRF75joEI0eyxPZFnpNqDDNpDd9DxdE/HRi8fzrejtPA5AQKBgDCx
vUhjT9jWjsuccZrl58ay/beBODhmsKxUpnZn9d0iw3+JpO7eSzSBeFmVIxGWof1M
LunSxGjtV0I31JI/cILIVV3mt9BXC6aIG6vR2aE2lj6wH+57zRnULnUUQkhHj8yO
GndjK0iBvpIAcT1dpNcRXgxM7JQjEdQuAxuvW9DJAoGATM6hkh7GzLIwjq47vZT1
HRCiNaQjLNyl6dxEGzxZfGMjndGdHJbz9KyrlVelm5WON9kuoXZx26s4PwEH6gWJ
vPztSAbCcmH6Zm4WqLyqK34lxIbZmmhUcbFdAKRUEIGMCyh1dKkOU16DYEeqv2Ka
r02VRk9KPo0LKXrNWmYi0t8=
-----END PRIVATE KEY-----"""

STATIC_PUBLIC_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAnFBKDAvTZEd+IlS59FKV
VFp4DT28sL1iHwZ94dJ5x5lf+Kq4Wxcl8COEQ3rp3QseM2MkAdZ1VvWbUmsonFux
7pVLQDyE+ANQkNd4K840zWV+CghTz34jxK59pb6cifSto7J8Wy7EqhUru7YLhnqZ
xz/AuHBPrq0RUS7f+ycJtfA6vj9Isp0BYpvgwOP97Ey+nCLiR5C/3IazOZblHQ7R
CbfZqP+encMwRbH/IvrXrz6/vecuIrq60fFtyZIbs7dASpfuSL6atIABu6CiSlXy
+6EhlEdmAXaCOPlQMYjc4u2ZNrOUTjuh3Yw8hMGezsTfTYZd2rrbGZRlkpfKbIdX
0QIDAQAB
-----END PUBLIC KEY-----"""


@pytest.fixture()
def deterministic_token_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch secrets.token_bytes to return deterministic values for AES key and IV."""

    sequence = [AES_KEY_BYTES, IV_BYTES]
    calls = iter(sequence)

    def fake_token_bytes(n: int) -> bytes:
        try:
            value = next(calls)
        except StopIteration as exc:  # pragma: no cover - unexpected extra call
            raise AssertionError("token_bytes called more times than expected") from exc
        assert len(value) == n, f"Expected {n} bytes but got {len(value)}"
        return value

    monkeypatch.setattr(secrets, "token_bytes", fake_token_bytes)


def render_snapshot_payload() -> dict[str, str]:
    """Render the encryption output as Base64 strings for stable snapshot comparison."""
    plaintext_payload = {
        "message": "Snapshot test message",
        "metadata": {
            "lang": "en",
            "tags": ["snapshot", "encryption"],
            "length": 3,
        },
    }
    plaintext_bytes = json.dumps(plaintext_payload, sort_keys=True).encode("utf-8")

    ciphertext_dict, encrypted_key, iv = encrypt(
        plaintext_bytes, STATIC_PUBLIC_KEY_PEM, use_pkcs1v15=True
    )

    decrypted = decrypt(ciphertext_dict, encrypted_key, STATIC_PRIVATE_KEY_PEM)
    assert decrypted == plaintext_bytes

    encrypted_key_b64 = base64.b64encode(encrypted_key).decode("ascii")
    return {
        "ciphertext_b64": base64.b64encode(ciphertext_dict["ciphertext"]).decode("ascii"),
        "ciphertext_len": len(ciphertext_dict["ciphertext"]),
        "cipher_iv_b64": base64.b64encode(ciphertext_dict["iv"]).decode("ascii"),
        "return_iv_b64": base64.b64encode(iv).decode("ascii"),
        "encrypted_key_len": len(encrypted_key_b64),
        "encrypted_key_bytes": len(encrypted_key),
        "aes_key_b64": base64.b64encode(AES_KEY_BYTES).decode("ascii"),
    }


def test_encrypt_output_matches_snapshot(deterministic_token_bytes: None) -> None:
    """Ensure encrypt.encrypt produces stable structure for a canonical payload."""
    assert deterministic_token_bytes is None
    snapshot_payload = render_snapshot_payload()

    if not SNAPSHOT_PATH.exists():
        pytest.fail(
            "Snapshot missing for encrypt.encrypt output. "
            "Run the snapshot update helper once the format is validated."
        )

    expected_payload = json.loads(SNAPSHOT_PATH.read_text())
    assert snapshot_payload == expected_payload
