import unittest
from unittest.mock import patch, MagicMock
import base64
import json
from client import ChatClient, encrypt
from encrypt import generate_keys

class TestChatClient(unittest.TestCase):

    def setUp(self):
        self.mock_server_public_key = base64.b64encode(b'MockServerPublicKey').decode('utf-8')
        self.base_url = 'http://localhost'
        self.relay_port = 5000
        self.chat_client = ChatClient(self.base_url, self.relay_port)

    @patch('client.requests.get')
    def test_get_server_public_key_success(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'server_public_key': self.mock_server_public_key}
        mock_get.return_value = mock_response

        server_public_key = self.chat_client.get_server_public_key()
        self.assertIsNotNone(server_public_key)
        self.assertEqual(server_public_key, base64.b64decode(self.mock_server_public_key))

    @patch('client.requests.get')
    def test_get_server_public_key_failure(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_get.return_value = mock_response

        server_public_key = self.chat_client.get_server_public_key()
        self.assertIsNone(server_public_key)

    @patch('client.requests.post')
    def test_send_request_to_faucet(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        encrypted_chat_history_b64 = base64.b64encode(b'encrypted_chat_history').decode('utf-8')
        server_public_key_b64 = base64.b64encode(b'MockServerPublicKey').decode('utf-8')
        encrypted_cipherkey_b64 = base64.b64encode(b'encrypted_cipherkey').decode('utf-8')
        iv_b64 = base64.b64encode(b'MockIV').decode('utf-8')  # Mock IV for the test.
        response = self.chat_client.send_request_to_faucet(encrypted_chat_history_b64, iv_b64, server_public_key_b64, encrypted_cipherkey_b64)
        self.assertEqual(response.status_code, 200)

    def test_encrypt_with_server_public_key_bytes(self):
        # Generate a valid public key in PEM format
        _, server_public_key = generate_keys()

        # Mock the chat history
        chat_history = [{"role": "user", "content": "Hello"}]

        # Call the encrypt function with the generated server's public key and chat history
        ciphertext_dict, cipherkey, iv = encrypt(json.dumps(chat_history).encode('utf-8'), server_public_key)

        # Assert that the ciphertext_dict, cipherkey, and iv are not None
        self.assertIsNotNone(ciphertext_dict)
        self.assertIsNotNone(cipherkey)
        self.assertIsNotNone(iv)

if __name__ == '__main__':
    unittest.main()
