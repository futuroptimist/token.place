"""
Crypto helper utilities for token.place
Provides simplified functions for common encryption/decryption operations

Example Usage:
-------------
# Basic usage
from utils.crypto_helpers import CryptoClient

# Create a client for a relay server
client = CryptoClient('http://localhost:5010')

# Fetch the server's public key
client.fetch_server_public_key()

# Send a chat message and get the response
response = client.send_chat_message("Hello, how are you?")
# handle the response without logging plaintext

# For API usage
client.fetch_server_public_key('/api/v1/public-key')
api_response = client.send_api_request([
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Tell me a joke."}
])
# Avoid printing plaintext responses in production environments
"""

import json
import base64
import requests
import logging
from copy import deepcopy
from typing import Dict, Tuple, Any, List, Optional, Union, Iterator
import time

# Import encryption functions
from encrypt import (
    generate_keys,
    encrypt,
    decrypt,
    decrypt_stream_chunk,
    StreamSession,
)

# Set up module-level logger without configuring global logging
logger = logging.getLogger("crypto_client")
logger.addHandler(logging.NullHandler())

class CryptoClient:
    """
    Helper class for end-to-end encryption operations.
    Simplifies the process of encryption, decryption, and API communication.
    """

    def __init__(self, base_url: str, debug: bool = False):
        """
        Initialize the crypto client

        Args:
            base_url: Base URL for the relay server. Must include http:// or https://.
            debug: Whether to enable debug logging
        """
        base_url = base_url.strip()
        if not base_url or not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        self.base_url = base_url.rstrip('/')  # Remove trailing slash if present
        self.server_public_key = None
        self.server_public_key_b64 = None
        self.client_private_key = None
        self.client_public_key = None
        self.client_public_key_b64 = None
        self.debug = debug

        if debug:
            logger.setLevel(logging.DEBUG)

        logger.info(f"CryptoClient initialized with base URL: {self.base_url}")

        # Generate client keys on initialization
        self._generate_client_keys()

    def _generate_client_keys(self):
        """Generate RSA key pair for the client"""
        logger.debug("Generating client keys...")
        self.client_private_key, self.client_public_key = generate_keys()
        self.client_public_key_b64 = base64.b64encode(self.client_public_key).decode('utf-8')
        logger.debug("Client keys generated successfully")

    def has_server_public_key(self) -> bool:
        """Return True if a server public key has been loaded."""
        return self.server_public_key is not None

    def fetch_server_public_key(self, endpoint: str = "/next_server", timeout: float = 10) -> bool:
        """Fetch the server's public key.

        By default this queries the relay's ``/next_server`` endpoint to
        discover a server and retrieve its public key. When connecting
        directly to a token.place server, pass ``"/api/v1/public-key"`` as the
        ``endpoint`` parameter instead.

        Args:
            endpoint: API endpoint to fetch the public key
            timeout: Maximum time in seconds to wait for a response

        Returns:
            True if successful, False otherwise
        """
        full_url = f"{self.base_url}{endpoint}"
        logger.debug(f"Fetching server public key from: {full_url}")

        try:
            response = requests.get(full_url, timeout=timeout)
            logger.debug(f"Server response status: {response.status_code}")

            if response.status_code != 200:
                logger.error(f"Failed to get server public key: {response.status_code}")
                return False

            data = response.json()

            # Check if there's an error in the response
            if 'error' in data:
                error_msg = data['error'].get('message', 'Unknown error')
                logger.error(f"Server returned error: {error_msg}")
                return False

            key_field = "server_public_key" if "server_public_key" in data else "public_key"

            if key_field not in data:
                logger.error(f"No public key found in response, available fields: {list(data.keys())}")
                return False

            self.server_public_key_b64 = data[key_field]
            self.server_public_key = base64.b64decode(self.server_public_key_b64)
            logger.info(f"Successfully fetched server public key")
            return True
        except Exception as e:
            logger.error(
                "Exception while fetching server public key: %s",
                e.__class__.__name__,
                exc_info=self.debug,
            )
            return False

    def encrypt_message(self, message: Union[Dict, List, str, bytes]) -> Dict[str, str]:
        """
        Encrypt a message for the server.

        Args:
            message: Message to encrypt (string, bytes, list, or dict). Must not be ``None``.

        Returns:
            Dictionary with base64-encoded ciphertext, cipherkey, and iv.

        Raises:
            ValueError: If ``message`` is ``None`` or the server key is missing.
            TypeError: If ``message`` is not a ``dict``, ``list``, ``str``, or ``bytes``.
        """
        if self.server_public_key is None:
            raise ValueError("Server public key not available. Call fetch_server_public_key() first.")
        if message is None:
            raise ValueError("message cannot be None")
        if not isinstance(message, (dict, list, str, bytes)):
            raise TypeError(f"Unsupported message type: {type(message).__name__}")

        # Convert to JSON if it's a dict or list
        if isinstance(message, (dict, list)):
            plaintext = json.dumps(message).encode('utf-8')
        elif isinstance(message, bytes):
            plaintext = message
        else:
            plaintext = message.encode('utf-8')

        logger.debug("Encrypting message of length %d bytes", len(plaintext))

        # Encrypt the message
        encrypted_dict, cipherkey, iv = encrypt(plaintext, self.server_public_key)

        # Convert to Base64 for transmission
        return {
            'ciphertext': base64.b64encode(encrypted_dict['ciphertext']).decode('utf-8'),
            'cipherkey': base64.b64encode(cipherkey).decode('utf-8'),
            'iv': base64.b64encode(iv).decode('utf-8')
        }

    def decrypt_message(self, encrypted_data: Dict[str, str]) -> Any:
        """
        Decrypt a message from the server

        Args:
            encrypted_data: Dictionary with base64-encoded encrypted fields

        Returns:
            Decrypted data (parsed from JSON if possible)
            or an empty string when the decrypted content is empty
        """
        if self.client_private_key is None:
            raise ValueError("Client private key not available")
        logger.debug("Decrypting message...")

        required_fields = ("ciphertext", "cipherkey", "iv")
        if not all(field in encrypted_data for field in required_fields):
            logger.error("Missing required encrypted fields: %s", required_fields)
            return None

        try:
            encrypted_response = {
                'ciphertext': base64.b64decode(encrypted_data['ciphertext']),
                'iv': base64.b64decode(encrypted_data['iv'])
            }
            encrypted_key = base64.b64decode(encrypted_data['cipherkey'])
        except Exception:
            logger.error("Failed to decode encrypted fields")
            return None

        # Decrypt the data
        decrypted_bytes = decrypt(encrypted_response, encrypted_key, self.client_private_key)

        if decrypted_bytes is None:
            logger.error("Decryption failed, got None")
            return None

        logger.debug(f"Successfully decrypted {len(decrypted_bytes)} bytes")

        # Try to parse as JSON
        try:
            return json.loads(decrypted_bytes.decode('utf-8'))
        except json.JSONDecodeError:
            # Return as string if not valid JSON
            logger.debug("Could not parse as JSON, returning as string")
            return decrypted_bytes.decode('utf-8')

    def send_encrypted_message(self, endpoint: str, payload: Dict, timeout: float = 10) -> Optional[Dict]:
        """
        Send an encrypted message to an endpoint

        Args:
            endpoint: API endpoint to send to
            payload: Data to include in the request
            timeout: Maximum time in seconds to wait for a response

        Returns:
            Server response as dictionary or None if failed
            or if the response body is not valid JSON
        """
        full_url = f"{self.base_url}{endpoint}"
        logger.debug(f"Sending encrypted message to: {full_url}")
        logger.debug(f"Payload keys: {list(payload.keys())}")

        try:
            response = requests.post(full_url, json=payload, timeout=timeout)
            logger.debug(f"Server response status: {response.status_code}")

            if response.status_code != 200:
                logger.error(
                    f"Server returned error status: {response.status_code}"
                )
                logger.debug(
                    "Response content length: %d", len(response.text)
                )
                return None

            try:
                return response.json()
            except ValueError:
                logger.error("Server returned non-JSON response")
                return None
        except Exception as e:
            logger.error(
                "Exception while sending encrypted message: %s",
                e.__class__.__name__,
                exc_info=self.debug,
            )
            return None

    def send_chat_message(self, message: Union[str, List[Dict]], max_retries: int = 5) -> Optional[List[Dict]]:
        """
        Send a chat message through the relay server

        Args:
            message: Message content or chat history to send (must be non-empty)
            max_retries: Maximum number of retry attempts for retrieving the response

        Returns:
            Decrypted server response or None if failed
        """
        # Validate and prepare the chat history
        if isinstance(message, str):
            if not message.strip():
                logger.error("Message cannot be empty")
                return None
            chat_history = [{"role": "user", "content": message}]
        else:
            if not message:
                logger.error("Chat history cannot be empty")
                return None
            chat_history = message

        # Ensure we have the server's public key
        if not self.server_public_key and not self.fetch_server_public_key():
            logger.error("Failed to get server public key")
            return None

        logger.debug("Sending chat message with %d entries", len(chat_history))

        # Encrypt the chat history
        try:
            encrypted_data = self.encrypt_message(chat_history)
        except Exception as e:
            logger.error(
                "Failed to encrypt message: %s",
                e.__class__.__name__,
                exc_info=self.debug,
            )
            return None

        # Prepare the payload
        payload = {
            'client_public_key': self.client_public_key_b64,
            'server_public_key': self.server_public_key_b64,
            'chat_history': encrypted_data['ciphertext'],
            'cipherkey': encrypted_data['cipherkey'],
            'iv': encrypted_data['iv']
        }

        # Send to the faucet endpoint
        response = self.send_encrypted_message('/faucet', payload)
        if not response:
            logger.error("Failed to send message to faucet")
            return None

        if not response.get('success', False) and 'message' not in response:
            logger.error("Unexpected response from faucet")
            return None

        logger.debug("Message sent successfully, waiting for processing")

        # Wait for processing
        time.sleep(3)

        # Retrieve the response
        return self.retrieve_chat_response(max_retries)

    def retrieve_chat_response(self, max_retries: int = 5, retry_delay: int = 2) -> Optional[List[Dict]]:
        """
        Retrieve and decrypt a chat response from the server

        Args:
            max_retries: Maximum number of retry attempts
            retry_delay: Delay between retries in seconds

        Returns:
            Decrypted chat history or None if failed
        """
        payload = {
            'client_public_key': self.client_public_key_b64
        }

        logger.debug(f"Attempting to retrieve response, max retries: {max_retries}")

        for i in range(max_retries):
            logger.debug(f"Retrieve attempt {i+1}/{max_retries}")
            response = self.send_encrypted_message('/retrieve', payload)

            if not response:
                logger.error("Failed to retrieve response")
                time.sleep(retry_delay)
                continue

            logger.debug(f"Received response keys: {list(response.keys())}")

            # Check for error messages
            if 'error' in response:
                # Handle case where error is a string
                if isinstance(response['error'], str):
                    error_msg = response['error']
                # Handle case where error is an object
                elif isinstance(response['error'], dict) and 'message' in response['error']:
                    error_msg = response['error']['message']
                else:
                    error_msg = str(response['error'])

                logger.debug(f"Server returned error: {error_msg}")

                # If the error indicates no response available yet, wait and retry
                if "No response available" in error_msg:
                    logger.debug("No response available yet, retrying...")
                    time.sleep(retry_delay)
                    continue
                else:
                    logger.error(f"Server error: {error_msg}")
                    return None

            if "chat_history" in response and "cipherkey" in response and "iv" in response:
                logger.debug("Got encrypted response, attempting to decrypt")
                # Decrypt the response
                try:
                    decrypted_data = self.decrypt_message({
                        'ciphertext': response['chat_history'],
                        'cipherkey': response['cipherkey'],
                        'iv': response['iv']
                    })

                    # Validate the response structure
                    if isinstance(decrypted_data, list) and len(decrypted_data) > 0:
                        for msg in decrypted_data:
                            if not isinstance(msg, dict) or 'role' not in msg or 'content' not in msg:
                                logger.warning("Invalid message format in response")
                                return None
                        return decrypted_data
                    else:
                        logger.warning("Unexpected response format type: %s", type(decrypted_data).__name__)
                        return None
                except Exception as e:
                    logger.error(
                        "Failed to decrypt response: %s",
                        e.__class__.__name__,
                        exc_info=self.debug,
                    )
                    return None
            else:
                logger.debug("Response missing expected fields: %s", list(response.keys()))

            # Wait before retrying
            time.sleep(retry_delay)

        logger.error(f"Failed to retrieve chat response after {max_retries} attempts")
        return None

    def send_api_request(self, messages: List[Dict], model: str = 'llama-3-8b-instruct') -> Optional[Dict]:
        """
        Send an encrypted API request to the chat completions endpoint

        Args:
            messages: List of message dictionaries
            model: Model name to use

        Returns:
            Decrypted API response or None if failed
        """
        # Ensure we have the server's API public key
        if not self.server_public_key:
            if not self.fetch_server_public_key('/api/v1/public-key'):
                logger.error("Failed to get API public key")
                return None

        # Encrypt the messages
        try:
            encrypted_data = self.encrypt_message(messages)
        except Exception as e:
            logger.error(
                "Failed to encrypt API request: %s",
                e.__class__.__name__,
                exc_info=self.debug,
            )
            return None

        # Prepare the payload
        payload = {
            'model': model,
            'encrypted': True,
            'client_public_key': self.client_public_key_b64,
            'messages': {
                'ciphertext': encrypted_data['ciphertext'],
                'cipherkey': encrypted_data['cipherkey'],
                'iv': encrypted_data['iv']
            }
        }

        # Send the request
        response = self.send_encrypted_message('/api/v1/chat/completions', payload)

        if not response:
            logger.error("Failed to get API response")
            return None

        logger.debug(f"API response keys: {list(response.keys())}")

        # Handle different response formats

        # New format: response has 'data' key with encrypted content
        if 'data' in response and isinstance(response['data'], dict) and 'encrypted' in response['data']:
            try:
                encrypted_content = response['data']
                decrypted_content = self.decrypt_message({
                    'ciphertext': encrypted_content['ciphertext'],
                    'cipherkey': encrypted_content['cipherkey'],
                    'iv': encrypted_content['iv']
                })
                return decrypted_content
            except Exception as e:
                logger.error(
                    "Failed to decrypt API response (data format): %s",
                    e.__class__.__name__,
                    exc_info=self.debug,
                )
                return None

        # Original format: response has 'encrypted_content' key
        elif 'encrypted' in response and response['encrypted'] and 'encrypted_content' in response:
            try:
                encrypted_content = response['encrypted_content']
                decrypted_content = self.decrypt_message({
                    'ciphertext': encrypted_content['ciphertext'],
                    'cipherkey': encrypted_content['cipherkey'],
                    'iv': encrypted_content['iv']
                })
                return decrypted_content
            except Exception as e:
                logger.error(
                    "Failed to decrypt API response (encrypted_content format): %s",
                    e.__class__.__name__,
                    exc_info=self.debug,
                )
                return None

        # Avoid logging full response to prevent leaking ciphertext or other data
        logger.error(
            "Invalid API response format: keys=%s",
            list(response.keys()) if isinstance(response, dict) else type(response).__name__,
        )
        return None

    def stream_chat_completion(
        self,
        messages: List[Dict[str, Any]],
        *,
        model: str = 'llama-3-8b-instruct',
        endpoint: str = '/api/v1/chat/completions',
        timeout: float = 30,
        max_retries: int = 1,
        retry_delay: float = 0.5,
    ) -> Iterator[Dict[str, Any]]:
        """Yield Server-Sent Event chunks from a streaming chat completion request.

        The helper issues a plaintext OpenAI-compatible request with ``stream=True`` and
        parses ``text/event-stream`` responses into dictionaries. When the server falls
        back to a standard JSON payload (e.g. because encryption was requested), the
        iterator yields a single ``{"event": "response", "data": ...}`` entry.

        Args:
            messages: Ordered chat history to send to the model.
            model: Model identifier to include with the request.
            endpoint: API endpoint relative to ``self.base_url``.
            timeout: Timeout in seconds applied to the initial request.

        Returns:
            An iterator that yields dictionaries describing each streaming update.
        """

        if not isinstance(messages, list) or not messages:
            raise ValueError("messages must be a non-empty list of chat entries")
        if any(not isinstance(entry, dict) for entry in messages):
            raise TypeError("each message must be a dictionary")

        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if retry_delay < 0:
            raise ValueError("retry_delay must be non-negative")

        payload = {
            'model': model,
            'messages': messages,
            'stream': True,
        }
        full_url = f"{self.base_url}{endpoint}"
        logger.debug("Starting streaming chat completion at %s", full_url)

        def _format_error_message(
            reason: str,
            *,
            fallback: Optional[str] = None,
            status_code: Optional[int] = None,
        ) -> str:
            if reason == 'invalid_encrypted_chunk':
                return 'Received an encrypted streaming chunk without payload data.'
            if reason == 'decrypt_failed':
                return 'Unable to decrypt the encrypted streaming update.'
            if reason == 'empty_response':
                return 'Streaming response completed without returning any data.'
            if reason == 'bad_status':
                if fallback == 'request_failed':
                    return 'Streaming request failed and fallback request could not complete.'
                if status_code is not None:
                    return f'Streaming request returned an unexpected status ({status_code}).'
                return 'Streaming request returned an unexpected status.'
            if reason == 'request_failed':
                return 'Unable to establish the streaming connection.'
            if reason == 'connection_lost':
                return 'Streaming connection was interrupted.'
            return 'Streaming request encountered an unexpected error.'

        def _build_error_data(reason: str, **extras: Any) -> Dict[str, Any]:
            payload: Dict[str, Any] = {'reason': reason, **extras}
            payload['message'] = _format_error_message(
                reason,
                fallback=payload.get('fallback'),
                status_code=payload.get('status_code'),
            )
            return payload

        def _fallback_message(reason: str, *, status_code: Optional[int] = None) -> str:
            if reason == 'bad_status':
                if status_code is not None:
                    return (
                        'Streaming endpoint returned '
                        f'status {status_code}; requesting full response instead.'
                    )
                return 'Streaming endpoint returned an unexpected status; requesting full response.'
            if reason == 'request_failed':
                return 'Streaming channel unavailable; retrying without streaming.'
            if reason == 'connection_lost':
                return 'Streaming connection dropped; requesting full response instead.'
            return 'Switching to a standard response due to streaming issues.'

        def _iter_chunks() -> Iterator[Dict[str, Any]]:
            attempt = 0
            reconnect_count = 0
            cached_events: List[Dict[str, Any]] = []
            decrypt_sessions: Dict[str, StreamSession] = {}

            def _decode_base64(value: str, field: str) -> Optional[bytes]:
                try:
                    return base64.b64decode(value)
                except (TypeError, ValueError):
                    logger.error("Failed to decode %s for encrypted stream chunk", field)
                    return None

            def _decode_associated_data(value: Any) -> Optional[bytes]:
                if value is None:
                    return None
                if not isinstance(value, str):
                    logger.error("associated_data must be a base64 string when provided")
                    return None
                return _decode_base64(value, "associated_data")

            def _decrypt_stream_payload(
                encrypted_payload: Dict[str, Any],
                *,
                session_id: Optional[str],
            ) -> Optional[Any]:
                ciphertext_b64 = encrypted_payload.get('ciphertext')
                iv_b64 = encrypted_payload.get('iv')
                if not isinstance(ciphertext_b64, str) or not isinstance(iv_b64, str):
                    logger.error(
                        "Encrypted streaming chunk missing ciphertext or iv",
                    )
                    return None

                ciphertext_bytes = _decode_base64(ciphertext_b64, "ciphertext")
                iv_bytes = _decode_base64(iv_b64, "iv")
                if ciphertext_bytes is None or iv_bytes is None:
                    return None

                ciphertext_dict: Dict[str, bytes] = {
                    'ciphertext': ciphertext_bytes,
                    'iv': iv_bytes,
                }

                tag_b64 = encrypted_payload.get('tag')
                if isinstance(tag_b64, str):
                    tag_bytes = _decode_base64(tag_b64, "tag")
                    if tag_bytes is None:
                        return None
                    ciphertext_dict['tag'] = tag_bytes

                mode_value = encrypted_payload.get('mode')
                if isinstance(mode_value, str):
                    ciphertext_dict['mode'] = mode_value
                else:
                    mode_value = None

                associated_data = _decode_associated_data(
                    encrypted_payload.get('associated_data'),
                )
                if encrypted_payload.get('associated_data') is not None and associated_data is None:
                    return None

                encrypted_key_bytes: Optional[bytes] = None
                cipherkey_b64 = encrypted_payload.get('cipherkey')
                if cipherkey_b64 is not None:
                    if not isinstance(cipherkey_b64, str):
                        logger.error(
                            "Encrypted streaming chunk has non-string cipherkey",
                        )
                        return None
                    encrypted_key_bytes = _decode_base64(cipherkey_b64, "cipherkey")
                    if encrypted_key_bytes is None:
                        return None

                session_obj: Optional[StreamSession] = None
                if session_id:
                    session_obj = decrypt_sessions.get(session_id)

                if session_obj is None and encrypted_key_bytes is None:
                    logger.error(
                        "Encrypted streaming chunk missing cipherkey for new session",
                    )
                    return None

                try:
                    plaintext_bytes, new_session = decrypt_stream_chunk(
                        ciphertext_dict,
                        self.client_private_key,
                        session=session_obj,
                        encrypted_key=encrypted_key_bytes,
                        cipher_mode=mode_value,
                        associated_data=associated_data,
                    )
                except Exception:
                    logger.error(
                        "Exception while decrypting streaming chunk",
                        exc_info=self.debug,
                    )
                    return None

                if session_id:
                    decrypt_sessions[session_id] = new_session

                try:
                    plaintext_text = plaintext_bytes.decode('utf-8')
                except UnicodeDecodeError:
                    logger.error("Failed to decode decrypted streaming chunk as UTF-8")
                    return None
                try:
                    return json.loads(plaintext_text)
                except json.JSONDecodeError:
                    logger.debug(
                        "Could not parse decrypted streaming chunk as JSON",
                    )
                    return plaintext_text

            def _record_cached_event(event: str, data: Any) -> None:
                try:
                    cached_events.append({'event': event, 'data': deepcopy(data)})
                except TypeError:  # pragma: no cover - defensive for non-copyable payloads
                    cached_events.append({'event': event, 'data': data})

            def _collect_text(value: Any, parts: List[str]) -> None:
                if isinstance(value, str):
                    parts.append(value)
                    return
                if isinstance(value, dict):
                    content = value.get('content')
                    if isinstance(content, str):
                        parts.append(content)
                    choices = value.get('choices')
                    if isinstance(choices, list):
                        for choice in choices:
                            if isinstance(choice, dict):
                                _collect_text(choice.get('delta'), parts)
                                _collect_text(choice.get('message'), parts)
                    delta = value.get('delta')
                    if isinstance(delta, dict):
                        _collect_text(delta, parts)
                    data_field = value.get('data')
                    if isinstance(data_field, (dict, list, str)):
                        _collect_text(data_field, parts)
                elif isinstance(value, list):
                    for item in value:
                        _collect_text(item, parts)

            def _drain_partial_events() -> List[Dict[str, Any]]:
                if not cached_events:
                    return []

                cached_copy = deepcopy(cached_events)
                text_parts: List[str] = []
                for entry in cached_copy:
                    _collect_text(entry.get('data'), text_parts)

                cached_events.clear()

                return [
                    {
                        'event': 'partial_response',
                        'data': {
                            'chunks': cached_copy,
                            'text': ''.join(text_parts),
                        },
                    }
                ]

            def _emit_non_stream_response(response: Any) -> Iterator[Dict[str, Any]]:
                try:
                    data = response.json()
                except ValueError:
                    body_text = getattr(response, 'text', '')
                    if body_text:
                        yield {'event': 'text', 'data': body_text}
                    else:
                        yield {
                            'event': 'error',
                            'data': _build_error_data('empty_response'),
                        }
                    return
                yield {'event': 'response', 'data': data}

            def _fallback_to_non_stream(
                reason: str,
                *,
                status_code: Optional[int] = None,
            ) -> Iterator[Dict[str, Any]]:
                fallback_payload = dict(payload)
                fallback_payload['stream'] = False
                for partial in _drain_partial_events():
                    yield partial
                try:
                    fallback_response = requests.post(
                        full_url,
                        json=fallback_payload,
                        timeout=timeout,
                        stream=False,
                    )
                except requests.RequestException as fallback_exc:
                    logger.error(
                        "Fallback request failed: %s",
                        fallback_exc.__class__.__name__,
                        exc_info=self.debug,
                    )
                    yield {
                        'event': 'error',
                        'data': _build_error_data(
                            reason,
                            fallback='request_failed',
                            status_code=status_code,
                        ),
                    }
                    decrypt_sessions.clear()
                    return
                yield {
                    'event': 'fallback',
                    'data': {
                        'reason': reason,
                        'message': _fallback_message(reason, status_code=status_code),
                    },
                }
                decrypt_sessions.clear()
                yield from _emit_non_stream_response(fallback_response)

            while True:
                try:
                    response = requests.post(
                        full_url,
                        json=payload,
                        timeout=timeout,
                        stream=True,
                    )
                except requests.RequestException as exc:  # pragma: no cover - defensive
                    logger.error(
                        "Streaming request failed: %s",
                        exc.__class__.__name__,
                        exc_info=self.debug,
                    )
                    if attempt >= max_retries:
                        yield from _fallback_to_non_stream('request_failed')
                        return
                    attempt += 1
                    reconnect_count += 1
                    yield {
                        'event': 'reconnect',
                        'data': {'attempt': reconnect_count, 'reason': 'request_failed'},
                    }
                    if retry_delay > 0:
                        time.sleep(retry_delay)
                    continue

                content_type = response.headers.get('Content-Type', '')
                is_event_stream = 'text/event-stream' in content_type.lower()
                should_retry = False
                retry_reason = 'connection_lost'

                try:
                    if response.status_code != 200:
                        logger.error(
                            "Streaming request returned unexpected status: %s",
                            response.status_code,
                        )
                        yield from _fallback_to_non_stream(
                            'bad_status', status_code=response.status_code
                        )
                        return

                    if not is_event_stream:
                        logger.debug(
                            "Non-streaming response received with content-type %s",
                            content_type,
                        )
                        decrypt_sessions.clear()
                        yield from _emit_non_stream_response(response)
                        return

                    for raw_line in response.iter_lines(decode_unicode=True):
                        if raw_line is None:
                            continue
                        if isinstance(raw_line, bytes):
                            try:
                                line = raw_line.decode('utf-8').strip()
                            except UnicodeDecodeError:
                                logger.debug("Dropping undecodable chunk: %s", raw_line)
                                continue
                        else:
                            line = raw_line.strip()
                        if not line or not line.startswith('data:'):
                            continue
                        data_str = line[5:].strip()
                        if not data_str:
                            continue
                        if data_str == '[DONE]':
                            logger.debug("Received stream terminator")
                            break
                        try:
                            chunk = json.loads(data_str)
                            if isinstance(chunk, dict):
                                event_name = chunk.get('event', 'chunk')

                                encrypted_body = None
                                session_id: Optional[str] = None
                                if isinstance(chunk.get('stream_session_id'), str):
                                    session_id = chunk['stream_session_id']
                                if chunk.get('encrypted') is True:
                                    encrypted_body = chunk.get('data')
                                else:
                                    data_field = chunk.get('data')
                                    if isinstance(data_field, dict) and data_field.get('encrypted') is True:
                                        # Encrypted payload may live directly under data or inside a nested
                                        # `data` key depending on the server schema. Support both shapes.
                                        encrypted_body = data_field.get('data', data_field)
                                        event_name = chunk.get('event', event_name)
                                        if isinstance(data_field.get('stream_session_id'), str):
                                            session_id = data_field['stream_session_id']
                                if (
                                    session_id is None
                                    and isinstance(chunk.get('data'), dict)
                                    and isinstance(chunk['data'].get('stream_session_id'), str)
                                ):
                                    session_id = chunk['data']['stream_session_id']

                                if encrypted_body is not None:
                                    if not isinstance(encrypted_body, dict):
                                        logger.error("Encrypted streaming chunk missing payload")
                                        yield {
                                            'event': 'error',
                                            'data': _build_error_data('invalid_encrypted_chunk'),
                                        }
                                        continue

                                    decrypted_payload = _decrypt_stream_payload(
                                        encrypted_body,
                                        session_id=session_id,
                                    )
                                    if decrypted_payload is None:
                                        yield {
                                            'event': 'error',
                                            'data': _build_error_data('decrypt_failed'),
                                        }
                                        continue

                                    event_payload = {
                                        'event': event_name,
                                        'data': decrypted_payload,
                                    }
                                    _record_cached_event(event_payload['event'], event_payload['data'])
                                    yield event_payload
                                    continue

                            event_payload = {'event': 'chunk', 'data': chunk}
                            _record_cached_event(event_payload['event'], event_payload['data'])
                            yield event_payload
                        except json.JSONDecodeError:
                            logger.debug("Ignoring malformed streaming chunk: %s", data_str)
                            event_payload = {'event': 'text', 'data': data_str}
                            _record_cached_event(event_payload['event'], event_payload['data'])
                            yield event_payload
                    decrypt_sessions.clear()
                    return
                except requests.RequestException as exc:  # pragma: no cover - defensive
                    logger.warning(
                        "Streaming interrupted: %s",
                        exc.__class__.__name__,
                        exc_info=self.debug,
                    )
                    if attempt >= max_retries:
                        yield from _fallback_to_non_stream(retry_reason)
                        return
                    should_retry = True
                    retry_reason = 'connection_lost'
                finally:
                    response.close()

                if should_retry:
                    attempt += 1
                    reconnect_count += 1
                    yield {
                        'event': 'reconnect',
                        'data': {'attempt': reconnect_count, 'reason': retry_reason},
                    }
                    if retry_delay > 0:
                        time.sleep(retry_delay)
                    continue

                decrypt_sessions.clear()
                return  # pragma: no cover - loop exits after successful attempt

        return _iter_chunks()
