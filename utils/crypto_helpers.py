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
from typing import Dict, Tuple, Any, List, Optional, Union, Iterator
import time

# Import encryption functions
from encrypt import generate_keys, encrypt, decrypt

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

        payload = {
            'model': model,
            'messages': messages,
            'stream': True,
        }
        full_url = f"{self.base_url}{endpoint}"
        logger.debug("Starting streaming chat completion at %s", full_url)

        try:
            response = requests.post(
                full_url,
                json=payload,
                timeout=timeout,
                stream=True,
            )
        except requests.RequestException as exc:  # pragma: no cover - defensive
            logger.error(
                "Streaming request failed: %s", exc.__class__.__name__, exc_info=self.debug
            )
            return iter(())

        content_type = response.headers.get('Content-Type', '')
        is_event_stream = 'text/event-stream' in content_type.lower()

        def _iter_chunks() -> Iterator[Dict[str, Any]]:
            try:
                if response.status_code != 200:
                    logger.error(
                        "Streaming request returned unexpected status: %s",
                        response.status_code,
                    )
                    yield {
                        'event': 'error',
                        'data': {'status': response.status_code},
                    }
                    return

                if not is_event_stream:
                    logger.debug(
                        "Non-streaming response received with content-type %s",
                        content_type,
                    )
                    try:
                        data = response.json()
                        yield {'event': 'response', 'data': data}
                    except ValueError:
                        body_text = getattr(response, 'text', '')
                        if body_text:
                            yield {'event': 'text', 'data': body_text}
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
                        yield {'event': 'chunk', 'data': chunk}
                    except json.JSONDecodeError:
                        logger.debug("Ignoring malformed streaming chunk: %s", data_str)
                        yield {'event': 'text', 'data': data_str}
            finally:
                response.close()

        return _iter_chunks()
