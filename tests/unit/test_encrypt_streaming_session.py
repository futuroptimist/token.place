import base64

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
