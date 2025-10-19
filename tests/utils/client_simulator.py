import json
import base64
import requests
from typing import Dict, Any, Optional, List, Union

from encrypt import generate_keys

class ClientSimulator:
    """
    Simulate a client for end-to-end testing, handling key generation,
    encryption, API requests, and decryption.
    """

    def __init__(self, base_url: str = "http://localhost:5000", api_prefix: str = "/api/v1"):
        """
        Initialize the client simulator with API endpoint.

        Args:
            base_url: Base URL for the API endpoint
            api_prefix: API prefix path (e.g., "/api/v1" or "/v1")
        """
        self.base_url = base_url.rstrip('/')
        normalized_prefix = api_prefix if api_prefix.startswith('/') else f"/{api_prefix}"
        self.api_base = f"{self.base_url}{normalized_prefix.rstrip('/')}"
        self.session = requests.Session()

        # Generate client keys
        self.private_key, self.public_key = generate_keys()
        self.server_public_key = None

    def _url(self, path: str) -> str:
        """Build a fully-qualified API URL for the configured prefix."""
        suffix = path if path.startswith('/') else f"/{path}"
        return f"{self.api_base}{suffix}"

    def fetch_server_public_key(self) -> bytes:
        """
        Fetch the server's public key.

        Returns:
            The server's public key as bytes
        """
        response = self.session.get(self._url("/public-key"))
        response.raise_for_status()

        data = response.json()
        key_b64 = data.get("server_public_key") or data.get("public_key")
        if key_b64 is None:
            raise ValueError("Public key not found in response")

        self.server_public_key = base64.b64decode(key_b64)
        return self.server_public_key

    def encrypt_message(self, message: Union[str, Dict, List[Dict]], server_key: Optional[bytes] = None) -> Dict:
        """
        Encrypt a message to send to the server.

        Args:
            message: Message content to encrypt (string or dict)
            server_key: Server public key (if None, uses stored key)

        Returns:
            Dict with encrypted message components
        """
        from encrypt import encrypt

        # Get the server key if not provided
        if server_key is None:
            if self.server_public_key is None:
                self.fetch_server_public_key()
            server_key = self.server_public_key

        # Normalize the message into the structure expected by the API and JSON encode it
        normalized_message: Union[str, Dict, List[Dict[str, Any]]]

        if isinstance(message, str):
            normalized_message = [{"role": "user", "content": message}]
        elif isinstance(message, dict) and {"role", "content"}.issubset(message.keys()):
            normalized_message = [message]  # Single chat turn provided directly
        else:
            normalized_message = message

        message_str = json.dumps(normalized_message)

        # Encrypt the message
        ciphertext_dict, cipherkey, iv = encrypt(message_str.encode('utf-8'), server_key)

        # Return encrypted data structure
        return {
            "ciphertext": base64.b64encode(ciphertext_dict['ciphertext']).decode('utf-8'),
            "cipherkey": base64.b64encode(cipherkey).decode('utf-8'),
            "iv": base64.b64encode(iv).decode('utf-8')
        }

    def decrypt_response(self, response_data: Dict) -> str:
        """
        Decrypt a response from the server.

        Args:
            response_data: Encrypted response data

        Returns:
            Decrypted response content as string
        """
        from encrypt import decrypt

        # Extract encrypted components
        ciphertext = base64.b64decode(response_data['ciphertext'])
        cipherkey = base64.b64decode(response_data['cipherkey'])
        iv = base64.b64decode(response_data['iv'])

        # Create ciphertext dict as expected by decrypt function
        ciphertext_dict = {
            'ciphertext': ciphertext,
            'iv': iv
        }

        # Decrypt the response
        decrypted_bytes = decrypt(ciphertext_dict, cipherkey, self.private_key)
        return decrypted_bytes.decode('utf-8')

    def send_request(self, encrypted_data: Dict, model: str = "llama-3-8b-instruct") -> Dict:
        """
        Send an encrypted request to the server.

        Args:
            encrypted_data: Encrypted message data
            model: Model to use for inference

        Returns:
            Encrypted response from the server
        """
        # Create the full request payload
        payload = {
            "model": model,
            "encrypted": True,
            "client_public_key": base64.b64encode(self.public_key).decode('utf-8'),
            "messages": encrypted_data
        }

        # Send the request
        response = self.session.post(
            self._url("/chat/completions"),
            json=payload
        )
        response.raise_for_status()

        response_json = response.json()

        if not isinstance(response_json, dict):
            raise ValueError("Unexpected response format: expected JSON object")

        # Handle API responses that include an encrypted payload wrapper
        if response_json.get("encrypted") and isinstance(response_json.get("data"), dict):
            payload = response_json["data"]
        else:
            choices = response_json.get("choices") if isinstance(response_json.get("choices"), list) else []
            payload = choices[0]["message"] if choices else None

        if not isinstance(payload, dict):
            raise ValueError("Encrypted response payload missing or malformed")

        required_fields = {"ciphertext", "cipherkey", "iv"}
        if not required_fields.issubset(payload.keys()):
            raise ValueError("Encrypted response payload missing required fields")

        return payload

    def send_message(self, message: Union[str, Dict, List[Dict]], model: str = "llama-3-8b-instruct") -> str:
        """
        High-level method to send a message and get a decrypted response.

        Args:
            message: Message to send (string, dict, or list of message dicts)
            model: Model to use for inference

        Returns:
            Decrypted response content
        """
        # Format the message as needed for the API
        if isinstance(message, str):
            formatted_message = [{"role": "user", "content": message}]
        elif isinstance(message, dict) and "role" in message and "content" in message:
            formatted_message = [message]
        elif isinstance(message, list) and all("role" in m and "content" in m for m in message):
            formatted_message = message
        else:
            raise ValueError("Message must be a string, a role/content dict, or a list of role/content dicts")

        # Ensure we have the server's public key
        if self.server_public_key is None:
            self.fetch_server_public_key()

        # Encrypt the message
        encrypted_data = self.encrypt_message(formatted_message)

        # Send the request
        encrypted_payload = self.send_request(encrypted_data, model)
        return self.decrypt_response(encrypted_payload)

    def stream_chat_completion(
        self,
        messages: Union[str, Dict, List[Dict[str, Any]]],
        model: str = "llama-3-8b-instruct",
        *,
        timeout: float = 15.0,
    ) -> Dict[str, Any]:
        """Request a streaming chat completion and return the parsed SSE payloads."""

        if isinstance(messages, str):
            formatted_messages: List[Dict[str, Any]] = [{"role": "user", "content": messages}]
        elif isinstance(messages, dict) and {"role", "content"}.issubset(messages):
            formatted_messages = [messages]
        elif (
            isinstance(messages, list)
            and all(isinstance(item, dict) and {"role", "content"}.issubset(item) for item in messages)
        ):
            formatted_messages = messages  # type: ignore[assignment]
        else:
            raise ValueError(
                "Messages must be a string, a role/content dict, or a list of role/content dicts",
            )

        payload = {
            "model": model,
            "messages": formatted_messages,
            "stream": True,
        }

        events: List[Dict[str, Any]] = []
        role: Optional[str] = None
        content_segments: List[str] = []
        finish_reason: Optional[str] = None

        with self.session.post(
            self._url("/chat/completions"),
            json=payload,
            stream=True,
            timeout=timeout,
        ) as response:
            response.raise_for_status()

            content_type = response.headers.get("Content-Type", "")
            if "text/event-stream" not in content_type:
                raise ValueError(
                    f"Expected text/event-stream response, received Content-Type: {content_type!r}",
                )

            for line in response.iter_lines(decode_unicode=True):
                if not line:
                    continue
                if not line.startswith("data: "):
                    continue

                data = line[len("data: "):]
                if data == "[DONE]":
                    break

                event = json.loads(data)
                events.append(event)

                choices = event.get("choices")
                if not choices:
                    continue

                delta = choices[0].get("delta", {})
                if "role" in delta:
                    role = delta["role"]
                if "content" in delta:
                    content_segments.append(delta["content"])

                finish = choices[0].get("finish_reason")
                if finish:
                    finish_reason = finish

        if finish_reason is None:
            raise ValueError("Streaming response ended without a finish_reason chunk")

        return {
            "role": role,
            "content": "".join(content_segments),
            "finish_reason": finish_reason,
            "events": events,
        }
