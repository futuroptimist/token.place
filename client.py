import requests
import json
import base64
import time
import os
from encrypt import generate_keys, encrypt, decrypt

# Generate client RSA keys
_private_key, _public_key = generate_keys()

# Convert client's public key to PEM format for transmission
_public_key_b64 = base64.b64encode(_public_key).decode('utf-8')

# Use an environment variable to determine the environment
environment = os.getenv('ENVIRONMENT', 'dev')  # Default to 'dev' if not set

# Choose the base URL based on the environment
base_url = 'http://token.place' if environment == 'prod' else 'http://localhost:5000'

class ChatClient:
    def __init__(self, base_url, relay_port):
        self.base_url = base_url
        self.relay_port = relay_port
        self.private_key, self.public_key = generate_keys()
        self.public_key_b64 = base64.b64encode(self.public_key).decode('utf-8')
        self.chat_history = []

    def send_message(self, message):
        self.chat_history.append({"role": "user", "content": message})
        server_public_key = self.get_server_public_key()
        
        if server_public_key:
            ciphertext_dict, cipherkey = encrypt(json.dumps(self.chat_history).encode('utf-8'), server_public_key)
            encrypted_chat_history_b64 = base64.b64encode(ciphertext_dict['ciphertext']).decode('utf-8')
            iv_b64 = base64.b64encode(ciphertext_dict['iv']).decode('utf-8')
            encrypted_cipherkey_b64 = base64.b64encode(cipherkey).decode('utf-8')

            response_faucet = self.send_request_to_faucet(
                encrypted_chat_history_b64,
                iv_b64,
                base64.b64encode(server_public_key).decode('utf-8'),
                encrypted_cipherkey_b64
            )
            if response_faucet and response_faucet.status_code == 200:
                response = self.retrieve_response(encrypted_cipherkey_b64)
                if response:
                    self.chat_history = response
                    return response

        return None

def get_server_public_key():
    """Fetch the server's public key from the relay."""
    try:
        response = requests.get(f'{base_url}/next_server')
        if response.status_code == 200:
            data = response.json()
            server_public_key_b64 = data['server_public_key']
            return base64.b64decode(server_public_key_b64)
        else:
            print(f"Could not fetch server's public key. Status code: {response.status_code}")
            return None
    except requests.exceptions.RequestException as e:
        print(f"Error while fetching server's public key: {str(e)}")
        return None

def send_request_to_faucet(encrypted_chat_history_b64, iv_b64, server_public_key_b64, encrypted_cipherkey_b64):
    """Send the encrypted chat history and IV to the faucet endpoint."""
    try:
        data = {
            "client_public_key": _public_key_b64,
            "server_public_key": server_public_key_b64,
            "chat_history": encrypted_chat_history_b64,
            "cipherkey": encrypted_cipherkey_b64,
            "iv": iv_b64,
        }
        response = requests.post(f'{base_url}/faucet', json=data)
        return response
    except requests.exceptions.RequestException as e:
        print(f"Error while sending request to faucet: {str(e)}")
        return None

def retrieve_response(encrypted_cipherkey_b64, timeout=60):
    start_time = time.time()
    while True:
        try:
            response = requests.post(f'{base_url}/retrieve', json={"client_public_key": _public_key_b64, "cipherkey": encrypted_cipherkey_b64})
            if response.status_code == 200:
                data = response.json()
                if 'chat_history' in data and 'iv' in data:
                    encrypted_chat_history_b64 = data['chat_history']
                    encrypted_chat_history = base64.b64decode(encrypted_chat_history_b64)
                    iv = base64.b64decode(data['iv'])
                    cipherkey = base64.b64decode(encrypted_cipherkey_b64)
                    decrypted_chat_history = decrypt({'ciphertext': encrypted_chat_history, 'iv': iv}, cipherkey, _private_key)
                    print(f"Response from AI: {decrypted_chat_history.decode('utf-8')}")
                    return json.loads(decrypted_chat_history.decode('utf-8'))
                else:
                    print("Response data is incomplete, waiting for complete response...")
            else:
                print(f"Unexpected status code from /retrieve endpoint: {response.status_code}")
        except requests.exceptions.RequestException as e:
            print(f"Error while retrieving response: {str(e)}")

        elapsed_time = time.time() - start_time
        if elapsed_time > timeout:
            print("Timeout while waiting for response.")
            return None

        time.sleep(2)  # Wait for a short interval before trying again

relay_url = 'http://localhost:5000/inference'
chat_history = []

def main():
    print("Welcome to the Chat Client!")
    print("Type your messages and press Enter to send. Type 'exit' to quit.")
    chat_history = []

    while True:
        user_message = input("You: ")
        if user_message.lower() == 'exit':
            break

        chat_history.append({"role": "user", "content": user_message})

        server_public_key = get_server_public_key()
        if server_public_key:
            ciphertext_dict, cipherkey = encrypt(json.dumps(chat_history).encode('utf-8'), server_public_key)
            encrypted_chat_history_b64 = base64.b64encode(ciphertext_dict['ciphertext']).decode('utf-8')
            iv_b64 = base64.b64encode(ciphertext_dict['iv']).decode('utf-8')
            encrypted_cipherkey_b64 = base64.b64encode(cipherkey).decode('utf-8')

            # Then, you correctly include iv_b64 in the data sent to the faucet
            response_faucet = send_request_to_faucet(
                encrypted_chat_history_b64,
                iv_b64,  # Add this argument
                base64.b64encode(server_public_key).decode('utf-8'),
                encrypted_cipherkey_b64
            )
            if response_faucet and response_faucet.status_code == 200:
                print("Request sent successfully, waiting for response...")
                response = retrieve_response(encrypted_cipherkey_b64)
                if response:
                    chat_history.append({"role": "assistant", "content": response})
            else:
                print("Failed to send encrypted chat to faucet.")

    print("Goodbye!")

if __name__ == "__main__":
    main()