"""
Tests focusing on system recovery from failures in encryption and decryption processes.

These tests verify that the system can:
1. Handle invalid encryption data properly
2. Recover from decryption errors 
3. Return appropriate error messages for corrupted data
"""

import pytest
import base64
import os
import json
import requests
import time
from unittest.mock import patch, Mock
from typing import Dict, Any

from tests.utils.client_simulator import ClientSimulator
from tests.test_e2e_conversation_flow import start_server, start_relay
from encrypt import generate_keys, encrypt, decrypt

# Check if we should run with the mock LLM to avoid downloading large models
USE_MOCK_LLM = os.environ.get('USE_MOCK_LLM', '1') == '1'  # Default to mock mode for tests

class TestFailureAndRecovery:
    """Tests for system behavior during failure scenarios and recovery."""
    
    @pytest.mark.failure
    def test_decryption_with_invalid_key(self):
        """Test that decryption with an invalid key fails gracefully."""
        # Generate two different key pairs
        valid_private_key, valid_public_key = generate_keys()
        _, invalid_public_key = generate_keys()  # Different key pair
        
        # Test data
        plaintext = "Test message for encryption"
        
        # Encrypt with valid key
        ciphertext_dict, cipherkey, iv = encrypt(plaintext.encode(), valid_public_key)
        
        # Try to decrypt with a different key
        # This should fail because cipherkey was encrypted with valid_public_key
        # and can only be decrypted with valid_private_key
        invalid_private_key_encoded = b"""-----BEGIN PRIVATE KEY-----
MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQCvvaFOEUBzQYJY
Xg24UyQDYJHoN7P4Y1aXVxUqbMMDgbkOjraNjFmxI9yvCVVoPg5mCrL/zD/vOKAz
KPz2ucqzLiNk8OiCJKo/TQ6XBnCSKJZZlQzGVV3oF5FPrBqKVPt/oY2GvFFYKd5P
dE4LYaJQxsNzbjUOlP2c3q/+JR5sIFQP9VRpkno61I9ZMHcE0g7UxnEgJBRe1jzC
c6lX3PEy2QtPmGBt9CPdvwc/oHod8qGcXPMWbcfqZoZBLn6ycO/b1InkQSH+e+YI
J1vv1bvQJ3KPnQ/cHjSLg/IPX6aMGneQ3PUZKb1MJ7kQwJnZqKbvR4xwBNh44Ctz
CbBdXLIzAgMBAAECggEASgSDJ8BYzlF06/sJLGbdivJFjxUQDSoRofJ3U9CpRMtJ
b8IUhG4vlFaGc7/e9Vm76oo7Yu3ApFHmQfVeN1LJLcQOOoLn6U7uhgrdr9Etyc5Q
5sOaKrdJ3zqvq3c8MsQlGY+xF5lILJvW4N/lqVVQNjZBxJoCSbFe2L7JfxXTM4Ex
yYk/JzFkw5QW2F7kO8RQxZsK36vlD1l8hB3C0g2+aXDDoOh8duvAQcmhGzqEZm79
fRD0CNr3hHRm+xRoLOKZ0R3GVU3ZPRLhyRZbIGh+y6DxC0iUQz/w/Cf0NGpwEuX9
i2KiGQxLSXzLIr8mTQNtbGD5Xf2QOazDQA+UXx8BUQKBgQDexVvB1TTf8vcO1DDm
x7uRU8TsxcuY6GtFwfGJs4ponWI8xHzyBUO59uFFgWF3WDcJeD+fK2Yd5Oi67CRO
ueJfLXNAJNp19s/U3BL8g+JHyFCiFHxeD/yDiYHGbNUGjQQu+NQt3qA5vhq+mALB
P/vE5RKE5r2ebBDrBxgYu+WnmQKBgQDKehDDhhYi5J9WHqzfkqd9kOZ+UyfA4DIM
qCuXAOSzXoJ44gXNMGK3vTDk3WnrK4cAgZN21K5I3dE4Z/vnxYTvtIhI44KfC0Fu
GTyHJXn9yYmAcXJ6A9UyFcJ3RPQ61U9NoS6GEMnk9K6kTnJIZUQr6kzn25ygQ6ZN
gaBBGk2oCwKBgEWsXllD8PFqoiGNqqGMHYYEuCqDLnBS9YukG1TiXQp+XJUxeTs3
5PD2m/0eJQU4kL0qh0D7Bs3GHpn+ZEDzr2IY8WEUcJ/+1VbE4AfdF840y3y4dDIj
HE9RZDG6s59WoLF6gXlLEqE1mGVdGG/jtDXYYPzXRiwyd9qHOy8ZUkc5AoGAb6Fv
Oe1HH/V4+dHbHCQnb8kbIUSFNJcHEYLPePaZZGXo24nryEgUekIVQHk0Z9LrSVcS
P2Lg6BvONMI+kV27LZH5k3g0M6P1CyHB5J2RVVO+fULGK8MnjKoRULBVZeJOWRZJ
KzsFPpGqmDSK2o8J4z/zKw4lnJEPvNwdGnHU6o0CgYEAiV4YqQ3YYWKGVYOASvQg
ehjVJew6PVzuYh2JSCgqLMPGCrKXF9qJTG+wB+qPybMq+T7N1Z8K9CFZCa4XcHIt
o5hPkNQcBJU/lUZPvKF+WUXiDyCdz1lN1vptAc8O9Vyi6+ECQo2lQcSv7QvzdFq9
gSxOSXP9KLvVWBJeBcHg3to=
-----END PRIVATE KEY-----"""
        
        try:
            # This should fail because we're trying to decrypt with the wrong key
            decrypt_result = decrypt(ciphertext_dict, cipherkey, invalid_private_key_encoded)
            # If we get here, the test failed
            assert False, "Decryption should have failed with an invalid key"
        except Exception as e:
            # Expecting an exception
            assert "Error decrypting message" in str(e) or "Decryption failed" in str(e) or "Padding" in str(e)
    
    @pytest.mark.failure
    def test_server_recovery_after_encryption_error(self):
        """Test that the server recovers after receiving an invalid encryption payload."""
        # Start the server and relay
        with start_server(use_mock_llm=USE_MOCK_LLM), start_relay():
            # Create a client
            client = ClientSimulator(base_url="http://localhost:5000")
            
            # Ensure we have the server's public key
            client.fetch_server_public_key()
            
            # Create an invalid encrypted payload
            corrupted_payload = {
                "model": "llama-3-8b-instruct",
                "encrypted": True,
                "client_public_key": client.public_key.decode('utf-8'),
                "messages": {
                    "ciphertext": "invalid_data",
                    "cipherkey": base64.b64encode(b"invalid").decode('utf-8'),
                    "iv": base64.b64encode(os.urandom(16)).decode('utf-8')
                }
            }
            
            # Send the corrupted request
            response = requests.post(
                f"{client.base_url}/api/v1/chat/completions",
                json=corrupted_payload
            )
            
            # Verify the server returns an appropriate error
            assert response.status_code == 400 or response.status_code == 500, \
                f"Expected error status code, got {response.status_code}"
            
            # Now send a valid request to verify server is still functional
            valid_message = "Are you still working?"
            response_text = client.send_message(valid_message)
            
            # Verify we got a meaningful response
            assert response_text, "Server did not respond after error"
            assert len(response_text) > 10, "Response suspiciously short"
    
    @pytest.mark.failure
    def test_relay_connection_interruption(self):
        """Test system behavior when relay connection is interrupted."""
        # Start the server and relay
        with start_server(use_mock_llm=USE_MOCK_LLM), start_relay():
            # Create a client
            client = ClientSimulator(base_url="http://localhost:5000")
            
            # Send a test message to verify everything works initially
            initial_response = client.send_message("Hello, are you there?")
            assert initial_response, "Initial communication failed"
            
            # Now simulate a relay failure for one request
            original_post = client.session.post
            
            def failing_post(*args, **kwargs):
                """Simulate a connection failure for one request."""
                client.session.post = original_post  # Restore for future requests
                raise requests.exceptions.ConnectionError("Simulated connection failure")
            
            client.session.post = failing_post
            
            # Try a request that should fail
            with pytest.raises(requests.exceptions.ConnectionError):
                client.send_message("This should fail")
            
            # Now try again, which should succeed because we restored the original post
            recovery_response = client.send_message("Are you back?")
            assert recovery_response, "Failed to recover after connection interruption"
    
    @pytest.mark.failure
    def test_server_handles_malformed_json(self):
        """Test that the server handles malformed JSON requests gracefully."""
        # Start the server and relay
        with start_server(use_mock_llm=USE_MOCK_LLM), start_relay():
            # Send a malformed JSON request directly to the relay
            malformed_data = b"{ this is not valid JSON }"
            response = requests.post(
                "http://localhost:5000/api/v1/chat/completions",
                data=malformed_data,
                headers={"Content-Type": "application/json"}
            )
            
            # Verify we get an appropriate error
            assert response.status_code == 400, "Expected 400 Bad Request for malformed JSON"
            
            # Verify the server is still functional after the error
            client = ClientSimulator(base_url="http://localhost:5000")
            valid_response = client.send_message("Is the server still operational?")
            assert valid_response, "Server should still be operational after malformed request"
    
    @pytest.mark.failure
    def test_empty_message_handling(self):
        """Test that the system handles empty messages appropriately."""
        # Start the server and relay
        with start_server(use_mock_llm=USE_MOCK_LLM), start_relay():
            client = ClientSimulator(base_url="http://localhost:5000")
            
            # Try sending an empty message
            try:
                response = client.send_message("")
                # If it doesn't raise an exception, ensure we got a reasonable response
                assert response, "Empty response received for empty message"
            except (ValueError, requests.exceptions.HTTPError) as e:
                # Alternatively, it's acceptable for the system to reject empty messages
                assert "empty" in str(e).lower() or "invalid" in str(e).lower() or \
                       "400" in str(e) or "bad request" in str(e).lower(), \
                       f"Unexpected error for empty message: {e}"
    
    @pytest.mark.failure
    def test_missing_public_key_handling(self):
        """Test system behavior when client public key is missing."""
        # Start the server and relay
        with start_server(use_mock_llm=USE_MOCK_LLM), start_relay():
            client = ClientSimulator(base_url="http://localhost:5000")
            
            # Generate a message but don't include the client's public key
            message = {"role": "user", "content": "Test message"}
            encrypted_data = client.encrypt_message({"messages": [message]})
            
            # Create a payload without the client_public_key
            incomplete_payload = {
                "model": "llama-3-8b-instruct",
                "encrypted": True,
                # client_public_key is intentionally omitted
                "messages": encrypted_data
            }
            
            # Send the request and expect an error
            response = requests.post(
                f"{client.base_url}/api/v1/chat/completions",
                json=incomplete_payload
            )
            
            # Verify the error response
            assert response.status_code == 400, "Expected 400 Bad Request for missing public key"
            assert "public key" in response.text.lower(), "Error should mention missing public key"
            
            # Verify the server still works with a proper request
            valid_response = client.send_message("Is the server still working?")
            assert valid_response, "Server should still function after invalid request" 
