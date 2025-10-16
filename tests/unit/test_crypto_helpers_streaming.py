"""Unit tests covering CryptoClient streaming decryption flows."""

from __future__ import annotations

import json
from typing import Dict, Iterable, List

from unittest.mock import MagicMock, patch

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


@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_decrypts_encrypted_chunks(mock_post: MagicMock) -> None:
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

    with patch.object(client, 'decrypt_message', return_value=decrypted_chunk) as mock_decrypt:
        chunks = list(client.stream_chat_completion(messages))

    mock_decrypt.assert_called_once_with(encrypted_payload)
    assert chunks == [
        {
            'event': 'delta',
            'data': decrypted_chunk,
        }
    ]


@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_decrypts_flat_encrypted_payload(mock_post: MagicMock) -> None:
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

    with patch.object(client, 'decrypt_message', return_value=decrypted_chunk) as mock_decrypt:
        chunks = list(client.stream_chat_completion(messages))

    mock_decrypt.assert_called_once_with(encrypted_payload)
    assert chunks == [
        {
            'event': 'delta',
            'data': decrypted_chunk,
        }
    ]


@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_decrypts_nested_encrypted_payload(mock_post: MagicMock) -> None:
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

    with patch.object(client, 'decrypt_message', return_value=decrypted_chunk) as mock_decrypt:
        chunks = list(client.stream_chat_completion(messages))

    mock_decrypt.assert_called_once_with(encrypted_payload)
    assert chunks == [
        {
            'event': 'delta',
            'data': decrypted_chunk,
        }
    ]


@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_handles_invalid_encrypted_payload(mock_post: MagicMock) -> None:
    """Invalid encrypted payload shapes should emit an error event."""

    client = CryptoClient('https://stream.test')
    messages = [{"role": "user", "content": "Hello"}]

    mock_post.return_value = _mock_stream_response([
        f"data: {json.dumps({'encrypted': True, 'data': 'oops'})}\n".encode(),
        b'data: [DONE]\n',
    ])

    with patch.object(client, 'decrypt_message') as mock_decrypt:
        chunks = list(client.stream_chat_completion(messages))

    mock_decrypt.assert_not_called()
    assert chunks == [
        {
            'event': 'error',
            'data': {'reason': 'invalid_encrypted_chunk'},
        }
    ]


@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_handles_decrypt_failures(mock_post: MagicMock) -> None:
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

    with patch.object(client, 'decrypt_message', return_value=None) as mock_decrypt:
        chunks = list(client.stream_chat_completion(messages))

    mock_decrypt.assert_called_once_with(encrypted_payload)
    assert chunks == [
        {
            'event': 'error',
            'data': {'reason': 'decrypt_failed'},
        }
    ]


@patch('utils.crypto_helpers.requests.post')
def test_stream_chat_completion_handles_unicode_decode_failures(mock_post: MagicMock) -> None:
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

    with patch.object(client, 'decrypt_message', return_value=decrypted_chunk) as mock_decrypt:
        chunks = list(client.stream_chat_completion(messages))

    mock_decrypt.assert_called_once_with(valid_payload)
    assert chunks == [
        {
            'event': 'delta',
            'data': decrypted_chunk,
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
