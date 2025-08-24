import requests
import json
import base64
import time
import os
import argparse
import logging
from encrypt import generate_keys, encrypt, decrypt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# Use an environment variable to determine the environment
environment = os.getenv('ENVIRONMENT', 'dev')  # Default to 'dev' if not set

# Choose the base domain based on the environment
base_url = "http://token.place" if environment == "prod" else "http://localhost"

# --- Configuration ---
# Use the correct base URL for your running relay/API
# You can override this with the API_BASE_URL environment variable.
# If running locally with default port 5070:
API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:5070/api/v1")
# Or use "http://localhost:5070" if targeting relay endpoints directly

REQUEST_TIMEOUT = 10  # seconds

CLIENT_KEYS_DIR = "client_keys"
CLIENT_PRIVATE_KEY_FILE = os.path.join(CLIENT_KEYS_DIR, "client_private.pem")
CLIENT_PUBLIC_KEY_FILE = os.path.join(CLIENT_KEYS_DIR, "client_public.pem")

# --- Key Management ---

def load_or_generate_client_keys():
    """Loads client keys if they exist, otherwise generates and saves them."""
    os.makedirs(CLIENT_KEYS_DIR, exist_ok=True)
    if os.path.exists(CLIENT_PRIVATE_KEY_FILE) and os.path.exists(CLIENT_PUBLIC_KEY_FILE):
        logger.info("Loading existing client keys...")
        with open(CLIENT_PRIVATE_KEY_FILE, "rb") as f:
            private_key = serialization.load_pem_private_key(
                f.read(),
                password=None, # Assuming no password for simplicity
                backend=default_backend()
            )
        with open(CLIENT_PUBLIC_KEY_FILE, "rb") as f:
            public_key_pem = f.read()
    else:
        logger.info("Generating new client keys...")
        private_key, public_key_pem = generate_keys()
        # Save keys
        with open(CLIENT_PRIVATE_KEY_FILE, "wb") as f:
            f.write(
                private_key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption() # No password
                )
            )
        with open(CLIENT_PUBLIC_KEY_FILE, "wb") as f:
            f.write(public_key_pem)
        logger.info("New keys saved in %s/", CLIENT_KEYS_DIR)

    return private_key, public_key_pem

# --- API Interaction ---

def get_server_public_key():
    """Gets the public key from the API server."""
    try:
        response = requests.get(f"{API_BASE_URL}/public-key", timeout=REQUEST_TIMEOUT)
        response.raise_for_status() # Raise an exception for bad status codes
        data = response.json()
        return data.get('public_key')
    except requests.exceptions.RequestException as e:
        logger.warning("Error getting server public key: %s", e.__class__.__name__)
        return None

def call_chat_completions_encrypted(server_pub_key_b64, client_priv_key, client_pub_key_pem):
    """Calls the encrypted chat completions endpoint."""

    # 1. Prepare message data
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What is the capital of France? Write a short poem about it."}
    ]

    # 2. Decode server public key
    try:
        server_public_key_bytes = base64.b64decode(server_pub_key_b64)
    except Exception as e:
        logger.warning("Error decoding server public key: %s", e.__class__.__name__)
        return None

    # 3. Encrypt message using encrypt.py functions
    logger.debug("Encrypting request...")
    try:
        # Ensure message is JSON bytes
        message_bytes = json.dumps(messages).encode('utf-8')
        # Encrypt using server's public key
        ciphertext_dict, cipherkey, iv = encrypt(message_bytes, server_public_key_bytes)
    except Exception as e:
        logger.warning("Error during request encryption: %s", e.__class__.__name__)
        return None

    # 4. Prepare payload
    client_pub_key_b64 = base64.b64encode(client_pub_key_pem).decode('utf-8')
    payload = {
        "model": "llama-3-8b-instruct", # Use a model the server knows
        "encrypted": True,
        "client_public_key": client_pub_key_b64,
        "messages": {
            "ciphertext": base64.b64encode(ciphertext_dict['ciphertext']).decode('utf-8'),
            "cipherkey": base64.b64encode(cipherkey).decode('utf-8'),
            "iv": base64.b64encode(iv).decode('utf-8')
        }
    }

    # 5. Send request
    logger.debug("Sending request to API...")
    try:
        response = requests.post(
            f"{API_BASE_URL}/chat/completions", json=payload, timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        encrypted_response_data = response.json()
    except requests.exceptions.RequestException as e:
        logger.warning("API request failed: %s", e)
        if hasattr(e, 'response') and e.response is not None:
             try:
                 err_status = e.response.status_code
             except Exception:
                 err_status = "unknown"
             logger.warning("Error status: %s", err_status)
        return None

    # 6. Decrypt response
    logger.debug("Decrypting response...")
    try:
        if not encrypted_response_data.get('encrypted'):
            logger.warning("Response was not encrypted as expected.")
            # Avoid logging potentially sensitive response contents
            return None

        enc_data = encrypted_response_data['data']
        ciphertext_resp = base64.b64decode(enc_data['ciphertext'])
        cipherkey_resp = base64.b64decode(enc_data['cipherkey'])
        iv_resp = base64.b64decode(enc_data['iv'])

        # Prepare dict for decrypt function
        ciphertext_resp_dict = {'ciphertext': ciphertext_resp, 'iv': iv_resp}

        # Decrypt using client's private key
        decrypted_bytes = decrypt(ciphertext_resp_dict, cipherkey_resp, client_priv_key)

        if decrypted_bytes is None:
            logger.warning("Failed to decrypt response.")
            return None

        # Decode and parse JSON
        decrypted_response = json.loads(decrypted_bytes.decode('utf-8'))
        return decrypted_response

    except Exception as e:
        logger.warning("Error during response decryption or parsing: %s", e.__class__.__name__)
        return None

class ChatClient:
    def __init__(self, base_url, relay_port=5000):
        self.base_url = base_url
        self.relay_port = relay_port
        self.private_key, self.public_key = generate_keys()
        self.public_key_b64 = base64.b64encode(self.public_key).decode('utf-8')
        self.chat_history = []

    def get_server_public_key(self):
        """Fetch the server's public key from the relay."""
        try:
            response = requests.get(
                f'{self.base_url}:{self.relay_port}/next_server', timeout=REQUEST_TIMEOUT
            )
            if response.status_code == 200:
                data = response.json()
                server_public_key_b64 = data['server_public_key']
                return base64.b64decode(server_public_key_b64)
            else:
                logger.warning(
                    "Could not fetch server's public key. Status code: %s",
                    response.status_code,
                )
                return None
        except requests.exceptions.RequestException as e:
            logger.warning(
                "Error while fetching server's public key: %s",
                e.__class__.__name__,
            )
            return None

    def send_request_to_faucet(self, encrypted_chat_history_b64, iv_b64, server_public_key_b64, encrypted_cipherkey_b64):
        """Send the encrypted chat history and IV to the faucet endpoint."""
        try:
            data = {
                "client_public_key": self.public_key_b64,
                "server_public_key": server_public_key_b64,
                "chat_history": encrypted_chat_history_b64,
                "cipherkey": encrypted_cipherkey_b64,
                "iv": iv_b64,
            }
            response = requests.post(
                f'{self.base_url}:{self.relay_port}/faucet', json=data, timeout=REQUEST_TIMEOUT
            )
            return response
        except requests.exceptions.RequestException as e:
            logger.warning(
                "Error while sending request to faucet: %s",
                e.__class__.__name__,
            )
            return None

    def retrieve_response(self, timeout=60):
        start_time = time.time()
        while True:
            try:
                response = requests.post(
                    f'{self.base_url}:{self.relay_port}/retrieve',
                    json={"client_public_key": self.public_key_b64},
                    timeout=REQUEST_TIMEOUT,
                )
                if response.status_code == 200:
                    data = response.json()
                    if 'chat_history' in data and 'iv' in data and 'cipherkey' in data:
                        encrypted_chat_history_b64 = data['chat_history']
                        encrypted_chat_history = base64.b64decode(encrypted_chat_history_b64)
                        iv = base64.b64decode(data['iv'])
                        cipherkey = base64.b64decode(data['cipherkey'])
                        decrypted_chat_history = decrypt({'ciphertext': encrypted_chat_history, 'iv': iv}, cipherkey, self.private_key)

                        if decrypted_chat_history is not None:
                            logger.debug(
                                "Received decrypted AI response (%d bytes)",
                                len(decrypted_chat_history),
                            )
                            return json.loads(decrypted_chat_history.decode('utf-8'))
                        else:
                            logger.debug("Decryption failed. Skipping this response.")
                    else:
                        logger.debug(
                            "Response data is incomplete, waiting for complete response..."
                        )
                else:
                    logger.warning(
                        "Unexpected status code from /retrieve endpoint: %s",
                        response.status_code,
                    )
            except requests.exceptions.RequestException as e:
                logger.warning(
                    "Error while retrieving response: %s",
                    e.__class__.__name__,
                )

            elapsed_time = time.time() - start_time
            if elapsed_time > timeout:
                logger.warning("Timeout while waiting for response.")
                return None

            time.sleep(2)  # Wait for a short interval before trying again

    def send_message(self, message):
        self.chat_history.append({"role": "user", "content": message})

        server_public_key = self.get_server_public_key()

        logger.debug("Retrieved server public key (%d bytes)", len(server_public_key))

        if server_public_key:
            ciphertext_dict, cipherkey, iv = encrypt(json.dumps(self.chat_history).encode('utf-8'), server_public_key)
            encrypted_chat_history_b64 = base64.b64encode(ciphertext_dict['ciphertext']).decode('utf-8')
            iv_b64 = base64.b64encode(iv).decode('utf-8')
            encrypted_cipherkey_b64 = base64.b64encode(cipherkey).decode('utf-8')

            response_faucet = self.send_request_to_faucet(
                encrypted_chat_history_b64,
                iv_b64,
                base64.b64encode(server_public_key).decode('utf-8'),
                encrypted_cipherkey_b64
            )
            if response_faucet and response_faucet.status_code == 200:
                start_time = time.time()
                timeout = 60  # Adjust the timeout as needed
                while True:
                    response = self.retrieve_response()
                    if response:
                        self.chat_history = response
                        return response

                    elapsed_time = time.time() - start_time
                    if elapsed_time > timeout:
                        logger.warning("Timeout while waiting for response.")
                        break

                    time.sleep(2)  # Adjust the polling interval as needed

        return None

def main():
    print("Welcome to the Chat Client!")
    print("Type your messages and press Enter to send. Type 'exit' to quit.")

    parser = argparse.ArgumentParser()
    parser.add_argument('--relay_port', type=int, default=5000, help='Port number for the relay')
    args = parser.parse_args()

    chat_client = ChatClient(base_url, args.relay_port)

    while True:
        user_message = input("You: ")
        if user_message.lower() == 'exit':
            break

        response = chat_client.send_message(user_message)
        if response:
            if isinstance(response, list) and response:
                last_msg = response[-1]
                content = (
                    last_msg.get("content")
                    if isinstance(last_msg, dict)
                    else str(last_msg)
                )
                print(f"Assistant: {content}")
            else:
                print("Assistant:", response)
        else:
            print("Failed to get response from the server.")

    print("Goodbye!")

if __name__ == "__main__":  # pragma: no cover
    main()
