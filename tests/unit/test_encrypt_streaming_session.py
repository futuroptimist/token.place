import collections
import os

import pytest

import encrypt


def _generate_keys():
    private_key, public_key = encrypt.generate_keys()
    return private_key, public_key


def test_encrypt_stream_chunk_reuses_session_key():
    private_key, public_key = _generate_keys()
    first_chunk = b"hello"
    second_chunk = b"world"

    payload1, encrypted_key1, session1 = encrypt.encrypt_stream_chunk(first_chunk, public_key)

    assert encrypted_key1 is not None
    plaintext1, session_for_decrypt = encrypt.decrypt_stream_chunk(
        payload1,
        private_key,
        encrypted_key=encrypted_key1,
    )
    assert plaintext1 == first_chunk
    assert session_for_decrypt is not None

    payload2, encrypted_key2, session2 = encrypt.encrypt_stream_chunk(
        second_chunk,
        public_key,
        session=session1,
    )

    assert encrypted_key2 is None
    assert session2 is session1 or session2.aes_key == session1.aes_key

    plaintext2, session_after_second = encrypt.decrypt_stream_chunk(
        payload2,
        private_key,
        session=session_for_decrypt,
    )
    assert plaintext2 == second_chunk
    assert session_after_second.aes_key == session_for_decrypt.aes_key


def test_encrypt_stream_chunk_retries_on_iv_collision(monkeypatch):
    """Encryption should retry when IV generation produces a duplicate."""

    private_key, public_key = _generate_keys()

    iv_sequence = collections.deque(
        [
            b"\x01" * encrypt.IV_SIZE,
            b"\x01" * encrypt.IV_SIZE,
            b"\x02" * encrypt.IV_SIZE,
            b"\x02" * encrypt.IV_SIZE,
        ]
    )

    def fake_token_bytes(length: int) -> bytes:
        if length == encrypt.IV_SIZE:
            return iv_sequence.popleft()
        return b"\x99" * length

    monkeypatch.setattr(encrypt.secrets, "token_bytes", fake_token_bytes)

    first_payload, encrypted_key, session = encrypt.encrypt_stream_chunk(b"first", public_key)
    assert encrypted_key is not None

    second_payload, _, session = encrypt.encrypt_stream_chunk(
        b"second",
        public_key,
        session=session,
    )

    assert first_payload["iv"] != second_payload["iv"], "IVs must differ between streaming chunks"

    _, decrypt_session = encrypt.decrypt_stream_chunk(
        first_payload,
        private_key,
        encrypted_key=encrypted_key,
    )

    _, _ = encrypt.decrypt_stream_chunk(
        second_payload,
        private_key,
        session=decrypt_session,
    )


@pytest.mark.parametrize("mode", ["CBC", "GCM"])
def test_encrypt_stream_chunk_requires_cipherkey_for_first_chunk(mode):
    private_key, public_key = _generate_keys()

    payload, encrypted_key, session = encrypt.encrypt_stream_chunk(b"chunk", public_key, cipher_mode=mode)
    assert encrypted_key is not None

    if mode == "GCM":
        with pytest.raises(ValueError):
            encrypt.decrypt_stream_chunk(payload, private_key)

    plaintext, session_after = encrypt.decrypt_stream_chunk(
        payload,
        private_key,
        encrypted_key=encrypted_key,
        cipher_mode=mode,
    )
    assert plaintext == b"chunk"
    assert session_after.aes_key == session.aes_key


def test_stream_session_normalises_inputs():
    session = encrypt.StreamSession(
        aes_key=bytearray(os.urandom(encrypt.AES_KEY_SIZE)),
        cipher_mode="gcm",
        associated_data=bytearray(b"meta"),
    )

    assert isinstance(session.aes_key, bytes)
    assert session.cipher_mode == "GCM"
    assert session.associated_data == b"meta"


def test_encrypt_helpers_validate_bytes_like_inputs():
    key = os.urandom(encrypt.AES_KEY_SIZE)

    with pytest.raises(TypeError, match="plaintext must be bytes-like"):
        encrypt._encrypt_with_key(  # type: ignore[arg-type]
            "not-bytes", key, cipher_mode="CBC"
        )

    with pytest.raises(TypeError, match="associated_data must be bytes-like"):
        encrypt._encrypt_with_key(
            b"data", key, cipher_mode="GCM", associated_data="bad"  # type: ignore[arg-type]
        )


def test_encrypt_helpers_handle_gcm_round_trip():
    key = os.urandom(encrypt.AES_KEY_SIZE)
    payload = b"payload"
    ad = b"context"

    ciphertext_dict = encrypt._encrypt_with_key(
        payload,
        key,
        cipher_mode="GCM",
        associated_data=ad,
    )

    assert ciphertext_dict["mode"] == "GCM"

    plaintext = encrypt._decrypt_with_key(
        ciphertext_dict,
        key,
        cipher_mode="GCM",
        associated_data=ad,
    )
    assert plaintext == payload


def test_decrypt_with_key_rejects_invalid_inputs():
    key = os.urandom(encrypt.AES_KEY_SIZE)

    with pytest.raises(ValueError, match="Missing required field: iv"):
        encrypt._decrypt_with_key({"ciphertext": b""}, key, cipher_mode="CBC")

    with pytest.raises(TypeError, match="ciphertext must be bytes-like"):
        encrypt._decrypt_with_key(
            {"ciphertext": "abc", "iv": b"0" * encrypt.IV_SIZE},  # type: ignore[arg-type]
            key,
            cipher_mode="CBC",
        )


def test_stream_session_validations_require_consistency():
    private_key, public_key = _generate_keys()

    payload1, encrypted_key, session1 = encrypt.encrypt_stream_chunk(
        b"hello",
        public_key,
        cipher_mode="GCM",
        associated_data=b"ad",
    )
    plaintext1, session_for_decrypt = encrypt.decrypt_stream_chunk(
        payload1,
        private_key,
        encrypted_key=encrypted_key,
        cipher_mode="GCM",
        associated_data=b"ad",
    )
    assert plaintext1 == b"hello"

    payload2, _, session2 = encrypt.encrypt_stream_chunk(
        b"world",
        public_key,
        session=session1,
        cipher_mode=session1.cipher_mode,
    )
    assert session2 is session1

    with pytest.raises(ValueError, match="associated_data mismatch"):
        encrypt.decrypt_stream_chunk(
            payload2,
            private_key,
            session=session_for_decrypt,
            associated_data=b"other",
        )

    with pytest.raises(ValueError, match="encrypted_key should be omitted"):
        encrypt.decrypt_stream_chunk(
            payload2,
            private_key,
            session=session_for_decrypt,
            encrypted_key=b"extra",
        )


def test_encrypt_stream_chunk_rejects_conflicting_modes():
    _, public_key = _generate_keys()
    session = encrypt.StreamSession(aes_key=os.urandom(encrypt.AES_KEY_SIZE), cipher_mode="CBC")

    with pytest.raises(ValueError, match="cipher_mode mismatch"):
        encrypt.encrypt_stream_chunk(
            b"data",
            public_key,
            session=session,
            cipher_mode="GCM",
        )


def test_mode_normalisation_and_validation():
    assert encrypt._normalise_mode(None) == "CBC"
    assert encrypt._normalise_mode("gcm") == "GCM"

    with pytest.raises(ValueError, match="Unsupported cipher_mode: fake"):
        encrypt._normalise_mode("fake")
