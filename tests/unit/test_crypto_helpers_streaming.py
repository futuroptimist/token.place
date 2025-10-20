"""Unit tests covering CryptoClient streaming decryption flows."""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, Iterable, List, Optional

from unittest.mock import MagicMock, patch

from requests.exceptions import ChunkedEncodingError, RequestException

import encrypt
from utils.crypto_helpers import CryptoClient


def _iter_lines_from_payloads(payloads: Iterable[str | bytes]) -> Iterable[str | bytes]:
    """Return an iterator over provided payloads.

    The streaming client calls ``response.iter_lines`` and expects an iterable. Using a
    helper keeps the individual tests focused on constructing their payloads.
    """

    return iter(payloads)


def _mock_stream_response(chunks: List[str | bytes]) -> MagicMock:
    response = MagicMock()
    response.status_code = 200
    response.headers = {"Content-Type": "text/event-stream"}
    response.iter_lines.return_value = _iter_lines_from_payloads(chunks)
    return response


def _mock_non_stream_response(
    *,
    status: int = 200,
    content_type: str = "application/json",
    json_payload: Optional[Dict] = None,
    text_body: str = "",
) -> MagicMock:
    response = MagicMock()
    response.status_code = status
    response.headers = {"Content-Type": content_type}
    response.iter_lines.return_value = iter(())
    if json_payload is None:
        response.json.side_effect = ValueError("non-json response")
    else:
        response.json.return_value = json_payload
    response.text = text_body
    return response


@patch('utils.crypto_helpers.decrypt_stream_chunk')
@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_decrypts_encrypted_chunks(
    mock_post: MagicMock,
    mock_decrypt_chunk: MagicMock,
) -> None:
    """Encrypted SSE chunks flagged at the top level should be decrypted."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    encrypted_payload: Dict[str, str] = {
        'ciphertext': 'c2VjcmV0',
        'cipherkey': 'a2V5',
        'iv': 'aXY=',
    }

    mock_post.return_value = _mock_stream_response([
        f"data: {json.dumps({'event': 'delta', 'encrypted': True, 'data': encrypted_payload})}\n".encode(),
        b'data: [DONE]\n',
    ])

    decrypted_chunk = {'choices': [{'delta': {'content': 'Hi there!'}}]}

    session_stub = encrypt.StreamSession(aes_key=b'a' * encrypt.AES_KEY_SIZE)

    def _decrypt(
        ciphertext_dict,
        private_key,
        *,
        session=None,
        encrypted_key=None,
        cipher_mode=None,
        associated_data=None,
    ):
        assert session is None
        assert encrypted_key == base64.b64decode(encrypted_payload['cipherkey'])
        assert ciphertext_dict['ciphertext'] == base64.b64decode(encrypted_payload['ciphertext'])
        assert ciphertext_dict['iv'] == base64.b64decode(encrypted_payload['iv'])
        assert cipher_mode is None
        assert associated_data is None
        return json.dumps(decrypted_chunk).encode('utf-8'), session_stub

    mock_decrypt_chunk.side_effect = _decrypt

    chunks = list(client.stream_chat_completion(messages))

    mock_decrypt_chunk.assert_called_once()
    assert chunks == [
        {
            'event': 'delta',
            'data': decrypted_chunk,
        }
    ]


@patch('utils.crypto_helpers.decrypt_stream_chunk')
@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_decrypts_flat_encrypted_payload(
    mock_post: MagicMock,
    mock_decrypt_chunk: MagicMock,
) -> None:
    """Decrypt payloads where the encrypted metadata lives directly under ``data``."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    encrypted_payload: Dict[str, str] = {
        'encrypted': True,
        'ciphertext': 'c2VjcmV0',
        'cipherkey': 'a2V5',
        'iv': 'aXY=',
    }

    mock_post.return_value = _mock_stream_response([
        f"data: {json.dumps({'event': 'delta', 'data': encrypted_payload})}\n".encode(),
        b'data: [DONE]\n',
    ])

    decrypted_chunk = {'choices': [{'delta': {'content': 'Hi there!'}}]}

    session_stub = encrypt.StreamSession(aes_key=b'b' * encrypt.AES_KEY_SIZE)

    def _decrypt(
        ciphertext_dict,
        private_key,
        *,
        session=None,
        encrypted_key=None,
        cipher_mode=None,
        associated_data=None,
    ):
        assert session is None
        assert encrypted_key == base64.b64decode(encrypted_payload['cipherkey'])
        assert ciphertext_dict['ciphertext'] == base64.b64decode(encrypted_payload['ciphertext'])
        assert ciphertext_dict['iv'] == base64.b64decode(encrypted_payload['iv'])
        assert cipher_mode is None
        assert associated_data is None
        return json.dumps(decrypted_chunk).encode('utf-8'), session_stub

    mock_decrypt_chunk.side_effect = _decrypt

    chunks = list(client.stream_chat_completion(messages))

    mock_decrypt_chunk.assert_called_once()
    assert chunks == [
        {
            'event': 'delta',
            'data': decrypted_chunk,
        }
    ]


@patch('utils.crypto_helpers.decrypt_stream_chunk')
@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_decrypts_nested_encrypted_payload(
    mock_post: MagicMock,
    mock_decrypt_chunk: MagicMock,
) -> None:
    """Decrypt payloads nested under an inner ``data`` key."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    encrypted_payload: Dict[str, str] = {
        'ciphertext': 'c2VjcmV0',
        'cipherkey': 'a2V5',
        'iv': 'aXY=',
    }

    nested_encrypted_body = {
        'encrypted': True,
        'data': encrypted_payload,
    }

    mock_post.return_value = _mock_stream_response([
        f"data: {json.dumps({'event': 'delta', 'data': nested_encrypted_body})}\n".encode(),
        b'data: [DONE]\n',
    ])

    decrypted_chunk = {'choices': [{'delta': {'content': 'Hi there!'}}]}

    session_stub = encrypt.StreamSession(aes_key=b'c' * encrypt.AES_KEY_SIZE)

    def _decrypt(
        ciphertext_dict,
        private_key,
        *,
        session=None,
        encrypted_key=None,
        cipher_mode=None,
        associated_data=None,
    ):
        assert session is None
        assert encrypted_key == base64.b64decode(encrypted_payload['cipherkey'])
        assert ciphertext_dict['ciphertext'] == base64.b64decode(encrypted_payload['ciphertext'])
        assert ciphertext_dict['iv'] == base64.b64decode(encrypted_payload['iv'])
        assert cipher_mode is None
        assert associated_data is None
        return json.dumps(decrypted_chunk).encode('utf-8'), session_stub

    mock_decrypt_chunk.side_effect = _decrypt

    chunks = list(client.stream_chat_completion(messages))

    mock_decrypt_chunk.assert_called_once()
    assert chunks == [
        {
            'event': 'delta',
            'data': decrypted_chunk,
        }
    ]


@patch('utils.crypto_helpers.decrypt_stream_chunk')
@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_handles_invalid_encrypted_payload(
    mock_post: MagicMock,
    mock_decrypt_chunk: MagicMock,
) -> None:
    """Invalid encrypted payload shapes should emit an error event."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    mock_post.return_value = _mock_stream_response([
        f"data: {json.dumps({'encrypted': True, 'data': 'oops'})}\n".encode(),
        b'data: [DONE]\n',
    ])

    chunks = list(client.stream_chat_completion(messages))

    mock_decrypt_chunk.assert_not_called()
    assert chunks == [
        {
            'event': 'error',
            'data': {
                'reason': 'invalid_encrypted_chunk',
                'message': 'Received an encrypted streaming chunk without payload data.',
            },
        }
    ]


@patch('utils.crypto_helpers.decrypt_stream_chunk')
@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_rejects_missing_ciphertext(
    mock_post: MagicMock,
    mock_decrypt_chunk: MagicMock,
) -> None:
    """Missing ciphertext metadata should surface a decrypt_failed event."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    malformed_payload = {
        'ciphertext': 123,
        'cipherkey': base64.b64encode(b'key').decode(),
        'iv': base64.b64encode(b'iv').decode(),
    }

    mock_post.return_value = _mock_stream_response([
        f"data: {json.dumps({'encrypted': True, 'data': malformed_payload})}\n".encode(),
        b'data: [DONE]\n',
    ])

    chunks = list(client.stream_chat_completion(messages))

    mock_decrypt_chunk.assert_not_called()
    assert chunks == [
        {
            'event': 'error',
            'data': {
                'reason': 'decrypt_failed',
                'message': 'Unable to decrypt the encrypted streaming update.',
            },
        }
    ]


@patch('utils.crypto_helpers.decrypt_stream_chunk')
@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_rejects_invalid_ciphertext_base64(
    mock_post: MagicMock,
    mock_decrypt_chunk: MagicMock,
) -> None:
    """Ciphertext that fails base64 decoding should abort the decrypt attempt."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    malformed_payload = {
        'ciphertext': 'not-base64!!!',
        'cipherkey': base64.b64encode(b'key').decode(),
        'iv': base64.b64encode(b'iv').decode(),
    }

    mock_post.return_value = _mock_stream_response([
        f"data: {json.dumps({'encrypted': True, 'data': malformed_payload})}\n".encode(),
        b'data: [DONE]\n',
    ])

    chunks = list(client.stream_chat_completion(messages))

    mock_decrypt_chunk.assert_not_called()
    assert chunks == [
        {
            'event': 'error',
            'data': {
                'reason': 'decrypt_failed',
                'message': 'Unable to decrypt the encrypted streaming update.',
            },
        }
    ]


@patch('utils.crypto_helpers.decrypt_stream_chunk')
@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_rejects_non_string_associated_data(
    mock_post: MagicMock,
    mock_decrypt_chunk: MagicMock,
) -> None:
    """Associated data must be a base64 string when provided."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    malformed_payload = {
        'ciphertext': base64.b64encode(b'chunk').decode(),
        'cipherkey': base64.b64encode(b'key').decode(),
        'iv': base64.b64encode(b'iv').decode(),
        'associated_data': {'bad': 'type'},
    }

    mock_post.return_value = _mock_stream_response([
        f"data: {json.dumps({'encrypted': True, 'data': malformed_payload})}\n".encode(),
        b'data: [DONE]\n',
    ])

    chunks = list(client.stream_chat_completion(messages))

    mock_decrypt_chunk.assert_not_called()
    assert chunks == [
        {
            'event': 'error',
            'data': {
                'reason': 'decrypt_failed',
                'message': 'Unable to decrypt the encrypted streaming update.',
            },
        }
    ]


@patch('utils.crypto_helpers.decrypt_stream_chunk')
@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_rejects_invalid_associated_data_base64(
    mock_post: MagicMock,
    mock_decrypt_chunk: MagicMock,
) -> None:
    """Invalid associated data should be treated as a decryption failure."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    malformed_payload = {
        'ciphertext': base64.b64encode(b'chunk').decode(),
        'cipherkey': base64.b64encode(b'key').decode(),
        'iv': base64.b64encode(b'iv').decode(),
        'associated_data': '!!!not-base64!!!',
    }

    mock_post.return_value = _mock_stream_response([
        f"data: {json.dumps({'encrypted': True, 'data': malformed_payload})}\n".encode(),
        b'data: [DONE]\n',
    ])

    chunks = list(client.stream_chat_completion(messages))

    mock_decrypt_chunk.assert_not_called()
    assert chunks == [
        {
            'event': 'error',
            'data': {
                'reason': 'decrypt_failed',
                'message': 'Unable to decrypt the encrypted streaming update.',
            },
        }
    ]


@patch('utils.crypto_helpers.decrypt_stream_chunk')
@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_rejects_invalid_tag_base64(
    mock_post: MagicMock,
    mock_decrypt_chunk: MagicMock,
) -> None:
    """Tags must decode from base64 to be accepted."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    malformed_payload = {
        'ciphertext': base64.b64encode(b'chunk').decode(),
        'cipherkey': base64.b64encode(b'key').decode(),
        'iv': base64.b64encode(b'iv').decode(),
        'tag': '!!!not-base64!!!',
    }

    mock_post.return_value = _mock_stream_response([
        f"data: {json.dumps({'encrypted': True, 'data': malformed_payload})}\n".encode(),
        b'data: [DONE]\n',
    ])

    chunks = list(client.stream_chat_completion(messages))

    mock_decrypt_chunk.assert_not_called()
    assert chunks == [
        {
            'event': 'error',
            'data': {
                'reason': 'decrypt_failed',
                'message': 'Unable to decrypt the encrypted streaming update.',
            },
        }
    ]


@patch('utils.crypto_helpers.decrypt_stream_chunk')
@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_rejects_non_string_cipherkey(
    mock_post: MagicMock,
    mock_decrypt_chunk: MagicMock,
) -> None:
    """Cipherkeys must be strings to decode successfully."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    malformed_payload = {
        'ciphertext': base64.b64encode(b'chunk').decode(),
        'cipherkey': {'bad': 'type'},
        'iv': base64.b64encode(b'iv').decode(),
    }

    mock_post.return_value = _mock_stream_response([
        f"data: {json.dumps({'encrypted': True, 'data': malformed_payload})}\n".encode(),
        b'data: [DONE]\n',
    ])

    chunks = list(client.stream_chat_completion(messages))

    mock_decrypt_chunk.assert_not_called()
    assert chunks == [
        {
            'event': 'error',
            'data': {
                'reason': 'decrypt_failed',
                'message': 'Unable to decrypt the encrypted streaming update.',
            },
        }
    ]


@patch('utils.crypto_helpers.decrypt_stream_chunk')
@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_rejects_invalid_cipherkey_base64(
    mock_post: MagicMock,
    mock_decrypt_chunk: MagicMock,
) -> None:
    """Cipherkeys that fail base64 decoding should abort the stream decrypt."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    malformed_payload = {
        'ciphertext': base64.b64encode(b'chunk').decode(),
        'cipherkey': '!!!not-base64!!!',
        'iv': base64.b64encode(b'iv').decode(),
    }

    mock_post.return_value = _mock_stream_response([
        f"data: {json.dumps({'encrypted': True, 'data': malformed_payload})}\n".encode(),
        b'data: [DONE]\n',
    ])

    chunks = list(client.stream_chat_completion(messages))

    mock_decrypt_chunk.assert_not_called()
    assert chunks == [
        {
            'event': 'error',
            'data': {
                'reason': 'decrypt_failed',
                'message': 'Unable to decrypt the encrypted streaming update.',
            },
        }
    ]


@patch('utils.crypto_helpers.decrypt_stream_chunk')
@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_requires_cipherkey_for_new_session(
    mock_post: MagicMock,
    mock_decrypt_chunk: MagicMock,
) -> None:
    """New encrypted sessions must include a cipherkey to initialize state."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    encrypted_payload = {
        'ciphertext': base64.b64encode(b'chunk').decode(),
        'iv': base64.b64encode(b'iv').decode(),
    }

    mock_post.return_value = _mock_stream_response([
        f"data: {json.dumps({'encrypted': True, 'stream_session_id': 'sess', 'data': encrypted_payload})}\n".encode(),
        b'data: [DONE]\n',
    ])

    chunks = list(client.stream_chat_completion(messages))

    mock_decrypt_chunk.assert_not_called()
    assert chunks == [
        {
            'event': 'error',
            'data': {
                'reason': 'decrypt_failed',
                'message': 'Unable to decrypt the encrypted streaming update.',
            },
        }
    ]


@patch('utils.crypto_helpers.decrypt_stream_chunk')
@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_handles_decrypt_failures(
    mock_post: MagicMock,
    mock_decrypt_chunk: MagicMock,
) -> None:
    """Decryption failures should surface an error event for callers."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    encrypted_payload: Dict[str, str] = {
        'ciphertext': 'c2VjcmV0',
        'cipherkey': 'a2V5',
        'iv': 'aXY=',
    }

    mock_post.return_value = _mock_stream_response([
        f"data: {json.dumps({'encrypted': True, 'data': encrypted_payload})}\n".encode(),
        b'data: [DONE]\n',
    ])

    mock_decrypt_chunk.side_effect = RuntimeError("decrypt failure")

    chunks = list(client.stream_chat_completion(messages))

    mock_decrypt_chunk.assert_called_once()
    assert chunks == [
        {
            'event': 'error',
            'data': {
                'reason': 'decrypt_failed',
                'message': 'Unable to decrypt the encrypted streaming update.',
            },
        }
    ]


@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_extracts_session_id_from_plain_chunk(
    mock_post: MagicMock,
) -> None:
    """Plain chunks should still surface session identifiers for telemetry."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    plain_chunk = {
        'event': 'chunk',
        'data': {
            'stream_session_id': 'plain-session',
            'delta': {'content': 'hello'},
        },
    }

    mock_post.return_value = _mock_stream_response([
        f"data: {json.dumps(plain_chunk)}\n",
        b'data: [DONE]\n',
    ])

    chunks = list(client.stream_chat_completion(messages))

    assert chunks == [
        {
            'event': 'chunk',
            'data': plain_chunk,
        }
    ]


@patch('utils.crypto_helpers.decrypt_stream_chunk')
@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_extracts_nested_session_id(
    mock_post: MagicMock,
    mock_decrypt_chunk: MagicMock,
) -> None:
    """Nested encrypted payloads should expose their stream session ids."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]
    session_id = 'nested-session'

    first_payload = {
        'ciphertext': base64.b64encode(b'first').decode(),
        'cipherkey': base64.b64encode(b'cipher-key').decode(),
        'iv': base64.b64encode(b'iv-1').decode(),
    }
    second_payload = {
        'ciphertext': base64.b64encode(b'second').decode(),
        'iv': base64.b64encode(b'iv-2').decode(),
    }

    stream_chunks = [
        f"data: {json.dumps({'event': 'chunk', 'data': {'encrypted': True, 'stream_session_id': session_id, 'data': first_payload}})}\n",
        f"data: {json.dumps({'event': 'chunk', 'data': {'encrypted': True, 'stream_session_id': session_id, 'data': second_payload}})}\n",
        b'data: [DONE]\n',
    ]

    mock_post.return_value = _mock_stream_response(stream_chunks)

    session_marker = encrypt.StreamSession(aes_key=b's' * encrypt.AES_KEY_SIZE)

    def decrypt_side_effect(
        ciphertext_dict,
        *_args,
        session=None,
        encrypted_key=None,
        cipher_mode=None,
        associated_data=None,
    ):
        if session is None:
            assert encrypted_key == base64.b64decode(first_payload['cipherkey'])
            return json.dumps({'delta': 'first'}).encode(), session_marker
        assert session is session_marker
        assert encrypted_key is None
        return json.dumps({'delta': 'second'}).encode(), session_marker

    mock_decrypt_chunk.side_effect = decrypt_side_effect

    chunks = list(client.stream_chat_completion(messages))

    assert mock_decrypt_chunk.call_count == 2
    assert chunks == [
        {
            'event': 'chunk',
            'data': {'delta': 'first'},
        },
        {
            'event': 'chunk',
            'data': {'delta': 'second'},
        },
    ]


@patch('utils.crypto_helpers.decrypt_stream_chunk')
@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_decrypts_with_mode_tag_and_aad(
    mock_post: MagicMock,
    mock_decrypt_chunk: MagicMock,
) -> None:
    """The decrypt helper should pass through mode, tag, and associated data."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]
    session_id = 'mode-session'
    aad_bytes = b'context'

    encrypted_payload = {
        'ciphertext': base64.b64encode(b'plain-text').decode(),
        'cipherkey': base64.b64encode(b'cipher-key').decode(),
        'iv': base64.b64encode(b'iv-3').decode(),
        'mode': 'gcm',
        'tag': base64.b64encode(b'tag-bytes').decode(),
        'associated_data': base64.b64encode(aad_bytes).decode(),
    }

    mock_post.return_value = _mock_stream_response([
        f"data: {json.dumps({'event': 'delta', 'stream_session_id': session_id, 'encrypted': True, 'data': encrypted_payload})}\n".encode(),
        b'data: [DONE]\n',
    ])

    session_marker = encrypt.StreamSession(aes_key=b'm' * encrypt.AES_KEY_SIZE)

    def decrypt_side_effect(
        ciphertext_dict,
        *_args,
        session=None,
        encrypted_key=None,
        cipher_mode=None,
        associated_data=None,
    ):
        assert session is None
        assert encrypted_key == base64.b64decode(encrypted_payload['cipherkey'])
        assert ciphertext_dict['tag'] == base64.b64decode(encrypted_payload['tag'])
        assert ciphertext_dict['mode'] == 'gcm'
        assert cipher_mode == 'gcm'
        assert associated_data == aad_bytes
        return b'plain-text', session_marker

    mock_decrypt_chunk.side_effect = decrypt_side_effect

    chunks = list(client.stream_chat_completion(messages))

    mock_decrypt_chunk.assert_called_once()
    assert chunks == [
        {
            'event': 'delta',
            'data': 'plain-text',
        }
    ]

@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_flags_tampered_encrypted_chunks(mock_post: MagicMock) -> None:
    """Tampered encrypted chunks should emit a decrypt_failed error event."""

    client = CryptoClient('https://stream.test')
    assert client.client_public_key is not None
    messages = [{"role": "user", "content": "Hello"}]

    plaintext_chunk = {'choices': [{'delta': {'content': 'Hi there!'}}]}
    ciphertext_dict, cipherkey, iv = encrypt.encrypt(
        json.dumps(plaintext_chunk).encode('utf-8'),
        client.client_public_key,
    )

    tampered_ciphertext = bytearray(ciphertext_dict['ciphertext'])
    tampered_ciphertext[0] ^= 0x01

    tampered_payload: Dict[str, str] = {
        'ciphertext': base64.b64encode(bytes(tampered_ciphertext)).decode('utf-8'),
        'cipherkey': base64.b64encode(cipherkey).decode('utf-8'),
        'iv': base64.b64encode(iv).decode('utf-8'),
    }

    mock_post.return_value = _mock_stream_response([
        f"data: {json.dumps({'event': 'delta', 'encrypted': True, 'data': tampered_payload})}\n".encode(),
        b'data: [DONE]\n',
    ])

    chunks = list(client.stream_chat_completion(messages))

    assert chunks == [
        {
            'event': 'error',
            'data': {
                'reason': 'decrypt_failed',
                'message': 'Unable to decrypt the encrypted streaming update.',
            },
        }
    ]


@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_surfaces_http_status_errors(mock_post: MagicMock) -> None:
    """HTTP failures should fall back to a non-streaming request."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    failing_response = _mock_stream_response([])
    failing_response.status_code = 502

    fallback_payload: Dict[str, Dict[str, str]] = {
        'choices': [{'message': {'role': 'assistant', 'content': 'fallback'}}],
    }

    mock_post.side_effect = [
        failing_response,
        _mock_non_stream_response(json_payload=fallback_payload),
    ]

    chunks = list(client.stream_chat_completion(messages))

    assert mock_post.call_count == 2
    first_kwargs = mock_post.call_args_list[0].kwargs
    second_kwargs = mock_post.call_args_list[1].kwargs
    assert first_kwargs['stream'] is True
    assert second_kwargs['stream'] is False
    assert second_kwargs['json']['stream'] is False
    assert chunks == [
        {
            'event': 'fallback',
            'data': {
                'reason': 'bad_status',
                'message': 'Streaming endpoint returned status 502; requesting full response instead.',
            },
        },
        {
            'event': 'response',
            'data': fallback_payload,
        },
    ]


@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_reports_fallback_request_failure(
    mock_post: MagicMock,
) -> None:
    """Failed fallback requests should yield an error event with context."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    failing_response = _mock_stream_response([])
    failing_response.status_code = 503

    mock_post.side_effect = [
        failing_response,
        RequestException("fallback failed"),
    ]

    chunks = list(client.stream_chat_completion(messages))

    assert mock_post.call_count == 2
    assert chunks == [
        {
            'event': 'error',
            'data': {
                'reason': 'bad_status',
                'fallback': 'request_failed',
                'status_code': 503,
                'message': 'Streaming request failed and fallback request could not complete.',
            },
        }
    ]


@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_includes_user_facing_error_messages(
    mock_post: MagicMock,
) -> None:
    """Error events should expose a user-friendly message for UI consumption."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    failing_response = _mock_stream_response([])
    failing_response.status_code = 503

    mock_post.side_effect = [
        failing_response,
        RequestException("fallback failed"),
    ]

    chunks = list(client.stream_chat_completion(messages))

    assert chunks == [
        {
            'event': 'error',
            'data': {
                'reason': 'bad_status',
                'fallback': 'request_failed',
                'status_code': 503,
                'message': 'Streaming request failed and fallback request could not complete.',
            },
        }
    ]


def _extract_format_error(helper: Any) -> Any:
    closure = helper.__closure__
    assert closure is not None
    for cell in closure:
        candidate = cell.cell_contents
        if callable(candidate) and getattr(candidate, '__name__', '') == '_format_error_message':
            return candidate
    raise AssertionError('Expected _format_error_message in closure')


def test_stream_chat_completion_error_message_helpers_cover_all_reasons() -> None:
    """Validate that helper formatting covers every error branch."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    generator = client.stream_chat_completion(messages)

    try:
        frame = generator.gi_frame
        assert frame is not None

        format_error = _extract_format_error(frame.f_locals['_build_error_data'])
        fallback_message = frame.f_locals['_fallback_message']

        assert format_error('bad_status') == 'Streaming request returned an unexpected status.'
        assert format_error('bad_status', status_code=418) == (
            'Streaming request returned an unexpected status (418).'
        )
        assert format_error('request_failed') == 'Unable to establish the streaming connection.'
        assert format_error('connection_lost') == 'Streaming connection was interrupted.'
        assert format_error('mystery') == 'Streaming request encountered an unexpected error.'

        assert fallback_message('bad_status') == (
            'Streaming endpoint returned an unexpected status; requesting full response.'
        )
        assert fallback_message('connection_lost') == (
            'Streaming connection dropped; requesting full response instead.'
        )
        assert fallback_message('unknown') == (
            'Switching to a standard response due to streaming issues.'
        )
    finally:
        generator.close()


@patch('utils.crypto_helpers.decrypt_stream_chunk')
@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_handles_unicode_decode_failures(
    mock_post: MagicMock,
    mock_decrypt_chunk: MagicMock,
) -> None:
    """Undecodable byte chunks should be skipped without disrupting the stream."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    undecodable = b"\xff\xfe\xfd"

    valid_payload: Dict[str, str] = {
        'ciphertext': 'c2VjcmV0',
        'cipherkey': 'a2V5',
        'iv': 'aXY=',
    }

    mock_post.return_value = _mock_stream_response([
        undecodable,
        f"data: {json.dumps({'event': 'delta', 'encrypted': True, 'data': valid_payload})}\n".encode(),
        b'data: [DONE]\n',
    ])

    decrypted_chunk = {'choices': [{'delta': {'content': 'Hi there!'}}]}

    session_stub = encrypt.StreamSession(aes_key=b'd' * encrypt.AES_KEY_SIZE)

    def _decrypt(
        ciphertext_dict,
        private_key,
        *,
        session=None,
        encrypted_key=None,
        cipher_mode=None,
        associated_data=None,
    ):
        assert encrypted_key == base64.b64decode(valid_payload['cipherkey'])
        assert ciphertext_dict['ciphertext'] == base64.b64decode(valid_payload['ciphertext'])
        assert ciphertext_dict['iv'] == base64.b64decode(valid_payload['iv'])
        return json.dumps(decrypted_chunk).encode('utf-8'), session_stub

    mock_decrypt_chunk.side_effect = _decrypt

    chunks = list(client.stream_chat_completion(messages))

    mock_decrypt_chunk.assert_called_once()
    assert chunks == [
        {
            'event': 'delta',
            'data': decrypted_chunk,
        }
    ]


@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_handles_json_fallback(mock_post: MagicMock) -> None:
    """Non-SSE JSON responses should be surfaced via a response event."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    payload = {'choices': [{'message': {'role': 'assistant', 'content': 'Hi'}}]}
    mock_post.return_value = _mock_non_stream_response(json_payload=payload)

    chunks = list(client.stream_chat_completion(messages))

    assert chunks == [
        {
            'event': 'response',
            'data': payload,
        }
    ]


@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_handles_plain_text_fallback(mock_post: MagicMock) -> None:
    """Plain-text non-SSE responses should be yielded as text events."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    mock_post.return_value = _mock_non_stream_response(
        content_type='text/plain',
        text_body='temporarily unavailable',
    )

    chunks = list(client.stream_chat_completion(messages))

    assert chunks == [
        {
            'event': 'text',
            'data': 'temporarily unavailable',
        }
    ]


@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_handles_empty_non_stream_payload(
    mock_post: MagicMock,
) -> None:
    """Non-SSE responses without JSON or text should emit an error event."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    mock_post.return_value = _mock_non_stream_response()

    chunks = list(client.stream_chat_completion(messages))

    assert chunks == [
        {
            'event': 'error',
            'data': {
                'reason': 'empty_response',
                'message': 'Streaming response completed without returning any data.',
            },
        }
    ]


@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_handles_text_chunks(mock_post: MagicMock) -> None:
    """Chunks missing JSON should be forwarded as text events."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    mock_post.return_value = _mock_stream_response([
        "data: not-json\n",
        b'data: [DONE]\n',
    ])

    chunks = list(client.stream_chat_completion(messages))

    assert chunks == [
        {
            'event': 'text',
            'data': 'not-json',
        }
    ]


@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_skips_non_data_lines(mock_post: MagicMock) -> None:
    """The iterator should ignore non-data lines and yield raw chunks when unencrypted."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    mock_post.return_value = _mock_stream_response([
        None,
        'event: keep-alive\n',
        'data:\n',
        'data: {"event": "chunk", "data": {"delta": {"content": "still encrypted?"}}}\n',
        'data: {"event": "chunk", "data": {"delta": {"content": "raw"}}}\n',
        b'data: [DONE]\n',
    ])

    with patch.object(client, 'decrypt_message') as mock_decrypt:
        chunks = list(client.stream_chat_completion(messages))

    mock_decrypt.assert_not_called()
    assert chunks == [
        {
            'event': 'chunk',
            'data': {'event': 'chunk', 'data': {'delta': {'content': 'still encrypted?'}}},
        },
        {
            'event': 'chunk',
            'data': {'event': 'chunk', 'data': {'delta': {'content': 'raw'}}},
        },
    ]


@patch('utils.crypto_helpers.time.sleep', autospec=True)
@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_reconnects_after_connection_drop(
    mock_post: MagicMock,
    mock_sleep: MagicMock,
) -> None:
    """Connection drops should trigger a retry and surface a reconnect event."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    partial_chunk = json.dumps(
        {
            'event': 'chunk',
            'data': {'delta': {'content': 'partial'}},
        }
    ).encode()

    first_response = MagicMock()
    first_response.status_code = 200
    first_response.headers = {"Content-Type": "text/event-stream"}

    def failing_iter_lines(*_, **__):
        yield b'data: ' + partial_chunk + b'\n'
        raise ChunkedEncodingError("connection lost")

    first_response.iter_lines.side_effect = failing_iter_lines

    second_response = _mock_stream_response([
        "data: {\"choices\": [{\"delta\": {\"content\": \"final\"}}]}\n",
        b'data: [DONE]\n',
    ])

    mock_post.side_effect = [first_response, second_response]

    chunks = list(client.stream_chat_completion(messages, retry_delay=0))

    assert mock_post.call_count == 2
    mock_sleep.assert_not_called()
    assert chunks == [
        {
            'event': 'chunk',
            'data': {'event': 'chunk', 'data': {'delta': {'content': 'partial'}}},
        },
        {
            'event': 'reconnect',
            'data': {'attempt': 1, 'reason': 'connection_lost'},
        },
        {
            'event': 'chunk',
            'data': {'choices': [{'delta': {'content': 'final'}}]},
        },
    ]


@patch('utils.crypto_helpers.decrypt_stream_chunk')
@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_reconnects_with_stream_session(
    mock_post: MagicMock,
    mock_decrypt_stream: MagicMock,
) -> None:
    """Reconnections should reuse the established encrypted stream session."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]
    session_id = 'session-123'

    first_payload = {
        'ciphertext': base64.b64encode(b'chunk-1').decode('ascii'),
        'iv': base64.b64encode(b'iv-1').decode('ascii'),
        'cipherkey': base64.b64encode(b'first-key').decode('ascii'),
    }
    second_payload = {
        'ciphertext': base64.b64encode(b'chunk-2').decode('ascii'),
        'iv': base64.b64encode(b'iv-2').decode('ascii'),
    }

    first_chunk = json.dumps(
        {
            'event': 'chunk',
            'encrypted': True,
            'stream_session_id': session_id,
            'data': first_payload,
        }
    ).encode()
    second_chunk = json.dumps(
        {
            'event': 'chunk',
            'encrypted': True,
            'stream_session_id': session_id,
            'data': second_payload,
        }
    ).encode()

    first_response = MagicMock()
    first_response.status_code = 200
    first_response.headers = {"Content-Type": "text/event-stream"}

    def first_iter_lines(*_: Any, **__: Any):
        yield b'data: ' + first_chunk + b'\n'
        raise ChunkedEncodingError("connection lost")

    first_response.iter_lines.side_effect = first_iter_lines

    second_response = _mock_stream_response([
        b'data: ' + second_chunk + b'\n',
        b'data: [DONE]\n',
    ])

    session_marker = MagicMock(name='StreamSession')

    def decrypt_side_effect(
        ciphertext_dict,
        private_key,
        *,
        session=None,
        encrypted_key=None,
        cipher_mode=None,
        associated_data=None,
    ):
        if session is None:
            assert encrypted_key == base64.b64decode(first_payload['cipherkey'])
            assert ciphertext_dict['ciphertext'] == base64.b64decode(first_payload['ciphertext'])
            assert ciphertext_dict['iv'] == base64.b64decode(first_payload['iv'])
            assert cipher_mode is None
            assert associated_data is None
            return (
                b'{"choices": [{"delta": {"content": "partial"}}]}',
                session_marker,
            )

        assert session is session_marker
        assert encrypted_key is None
        assert ciphertext_dict['ciphertext'] == base64.b64decode(second_payload['ciphertext'])
        assert ciphertext_dict['iv'] == base64.b64decode(second_payload['iv'])
        assert cipher_mode is None
        assert associated_data is None
        return (
            b'{"choices": [{"delta": {"content": "final"}}]}',
            session_marker,
        )

    mock_decrypt_stream.side_effect = decrypt_side_effect
    mock_post.side_effect = [first_response, second_response]

    chunks = list(client.stream_chat_completion(messages, retry_delay=0))

    assert mock_decrypt_stream.call_count == 2
    assert chunks == [
        {
            'event': 'chunk',
            'data': {'choices': [{'delta': {'content': 'partial'}}]},
        },
        {
            'event': 'reconnect',
            'data': {'attempt': 1, 'reason': 'connection_lost'},
        },
        {
            'event': 'chunk',
            'data': {'choices': [{'delta': {'content': 'final'}}]},
        },
    ]


@patch('utils.crypto_helpers.time.sleep', autospec=True)
@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_waits_before_retry(
    mock_post: MagicMock,
    mock_sleep: MagicMock,
) -> None:
    """Positive retry delays should pause between attempts."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    partial_chunk = json.dumps(
        {
            'event': 'chunk',
            'data': {'delta': {'content': 'partial'}},
        }
    ).encode()

    first_response = MagicMock()
    first_response.status_code = 200
    first_response.headers = {"Content-Type": "text/event-stream"}

    def failing_iter_lines(*_, **__):
        yield b'data: ' + partial_chunk + b'\n'
        raise ChunkedEncodingError("connection lost")

    first_response.iter_lines.side_effect = failing_iter_lines

    second_response = _mock_stream_response([
        "data: {\"choices\": [{\"delta\": {\"content\": \"final\"}}]}\n",
        b'data: [DONE]\n',
    ])

    mock_post.side_effect = [first_response, second_response]

    chunks = list(client.stream_chat_completion(messages, retry_delay=1.25))

    mock_sleep.assert_called_once_with(1.25)
    assert chunks[1] == {
        'event': 'reconnect',
        'data': {'attempt': 1, 'reason': 'connection_lost'},
    }


@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_falls_back_after_request_failure(
    mock_post: MagicMock,
) -> None:
    """Request failures should fall back to a JSON response when retries are exhausted."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    fallback_payload: Dict[str, Dict[str, str]] = {
        'choices': [{'message': {'role': 'assistant', 'content': 'fallback'}}],
    }

    mock_post.side_effect = [
        RequestException("boom"),
        _mock_non_stream_response(json_payload=fallback_payload),
    ]

    chunks = list(
        client.stream_chat_completion(messages, max_retries=0, retry_delay=0),
    )

    assert mock_post.call_count == 2
    first_kwargs = mock_post.call_args_list[0].kwargs
    second_kwargs = mock_post.call_args_list[1].kwargs
    assert first_kwargs['stream'] is True
    assert second_kwargs['stream'] is False
    assert second_kwargs['json']['stream'] is False
    assert chunks == [
        {
            'event': 'fallback',
            'data': {
                'reason': 'request_failed',
                'message': 'Streaming channel unavailable; retrying without streaming.',
            },
        },
        {
            'event': 'response',
            'data': fallback_payload,
        },
    ]


@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_emits_partial_response_before_fallback(
    mock_post: MagicMock,
) -> None:
    """Partial SSE payloads should surface before the fallback response."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    partial_chunk = json.dumps(
        {
            'event': 'chunk',
            'data': {'delta': {'content': 'partial'}},
        }
    ).encode()

    first_response = MagicMock()
    first_response.status_code = 200
    first_response.headers = {"Content-Type": "text/event-stream"}

    def failing_iter_lines(*_: Any, **__: Any):
        yield b'data: ' + partial_chunk + b'\n'
        raise ChunkedEncodingError("connection lost")

    first_response.iter_lines.side_effect = failing_iter_lines

    fallback_payload: Dict[str, Dict[str, str]] = {
        'choices': [{'message': {'role': 'assistant', 'content': 'recovered'}}],
    }

    mock_post.side_effect = [
        first_response,
        _mock_non_stream_response(json_payload=fallback_payload),
    ]

    stream = client.stream_chat_completion(messages, max_retries=0, retry_delay=0)

    first_chunk = next(stream)
    assert first_chunk == {
        'event': 'chunk',
        'data': {'event': 'chunk', 'data': {'delta': {'content': 'partial'}}},
    }
    assert mock_post.call_count == 1

    second_chunk = next(stream)
    assert second_chunk == {
        'event': 'partial_response',
        'data': {
            'chunks': [
                {
                    'event': 'chunk',
                    'data': {'event': 'chunk', 'data': {'delta': {'content': 'partial'}}},
                }
            ],
            'text': 'partial',
        },
    }
    assert mock_post.call_count == 1

    remaining_chunks = list(stream)
    assert remaining_chunks == [
        {
            'event': 'fallback',
            'data': {
                'reason': 'connection_lost',
                'message': 'Streaming connection dropped; requesting full response instead.',
            },
        },
        {
            'event': 'response',
            'data': fallback_payload,
        },
    ]


@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_partial_response_collects_nested_text(
    mock_post: MagicMock,
) -> None:
    """Cached partial chunks should merge nested text fields before fallback."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    streaming_payloads = [
        {
            'event': 'chunk',
            'data': {
                'content': 'A',
                'choices': [
                    {
                        'delta': {'content': 'B'},
                        'message': {'content': 'C'},
                    }
                ],
            },
        },
        {
            'event': 'chunk',
            'data': {
                'delta': {'content': 'D'},
                'data': {'content': 'E'},
            },
        },
        {
            'event': 'chunk',
            'data': {'data': ['F', {'content': 'G'}]},
        },
        {
            'event': 'chunk',
            'data': {'data': 'H'},
        },
    ]

    first_response = MagicMock()
    first_response.status_code = 200
    first_response.headers = {"Content-Type": "text/event-stream"}

    def failing_iter_lines(*_: Any, **__: Any):
        for payload in streaming_payloads:
            yield f"data: {json.dumps(payload)}\n"
        yield 'data: not-json\n'
        raise ChunkedEncodingError("connection lost")

    first_response.iter_lines.side_effect = failing_iter_lines

    fallback_payload: Dict[str, Dict[str, str]] = {
        'choices': [{'message': {'role': 'assistant', 'content': 'recovered'}}],
    }

    mock_post.side_effect = [
        first_response,
        _mock_non_stream_response(json_payload=fallback_payload),
    ]

    stream = client.stream_chat_completion(messages, max_retries=0, retry_delay=0)

    emitted_events = [next(stream) for _ in range(len(streaming_payloads) + 1)]
    assert [event['event'] for event in emitted_events] == [
        'chunk',
        'chunk',
        'chunk',
        'chunk',
        'text',
    ]
    assert mock_post.call_count == 1

    partial_event = next(stream)
    assert partial_event == {
        'event': 'partial_response',
        'data': {
            'chunks': emitted_events,
            'text': 'ABCDEFGHnot-json',
        },
    }
    assert mock_post.call_count == 1

    remaining_chunks = list(stream)
    assert remaining_chunks == [
        {
            'event': 'fallback',
            'data': {
                'reason': 'connection_lost',
                'message': 'Streaming connection dropped; requesting full response instead.',
            },
        },
        {
            'event': 'response',
            'data': fallback_payload,
        },
    ]

    assert mock_post.call_count == 2
