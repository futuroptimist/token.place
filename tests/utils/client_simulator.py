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

    def __init__(self, base_url: str = "http://localhost:5000"):
        """
        Initialize the client simulator with API endpoint.
        
        Args:
            base_url: Base URL for the API endpoint
        """
        self.base_url = base_url
        self.session = requests.Session()
        
        # Generate client keys
        self.private_key, self.public_key = generate_keys()
        self.server_public_key = None
        
    def fetch_server_public_key(self) -> bytes:
        """
        Fetch the server's public key.
        
        Returns:
            The server's public key as bytes
        """
        response = self.session.get(f"{self.base_url}/api/v1/public-key")
        response.raise_for_status()

        data = response.json()
        key_b64 = data.get("server_public_key") or data.get("public_key")
        if key_b64 is None:
            raise ValueError("Public key not found in response")

        self.server_public_key = base64.b64decode(key_b64)
        return self.server_public_key
        
    def encrypt_message(self, message: Union[str, Dict], server_key: Optional[bytes] = None) -> Dict:
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
        
        # Convert message to JSON string if it's a dict or list
        if isinstance(message, (dict, list)):
            message_str = json.dumps(message)
        else:
            message_str = str(message)
            
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
            f"{self.base_url}/api/v1/chat/completions",
            json=payload
        )
        response.raise_for_status()

        # Return parsed JSON
        return response.json()
    
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
        response_data = self.send_request(encrypted_data, model)

        # Handle different response formats
        if isinstance(response_data, dict):
            if "data" in response_data:
                return self.decrypt_response(response_data["data"])
            elif "choices" in response_data and response_data["choices"]:
                return self.decrypt_response(response_data["choices"][0]["message"])

        raise ValueError("Unexpected response format")
