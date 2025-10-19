"""Unit tests covering CryptoClient streaming decryption flows."""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

from unittest.mock import MagicMock, patch

from requests.exceptions import ChunkedEncodingError, RequestException

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
            'data': {
                'reason': 'invalid_encrypted_chunk',
                'message': 'Received an encrypted streaming chunk without payload data.',
            },
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
