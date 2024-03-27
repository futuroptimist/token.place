import requests
import json
import base64
import time
import os
import argparse
from encrypt import generate_keys, encrypt, decrypt

# Use an environment variable to determine the environment
environment = os.getenv('ENVIRONMENT', 'dev')  # Default to 'dev' if not set

# Choose the base URL based on the environment
base_url = 'http://token.place' if environment == 'prod' else 'http://localhost'

# TODO: handle prod case, where the port shouldn't be hardcoded (as we can't determine in advance if it's 80 or 443)

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
            response = requests.get(f'{self.base_url}:{self.relay_port}/next_server')
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
            response = requests.post(f'{self.base_url}:{self.relay_port}/faucet', json=data)
            return response
        except requests.exceptions.RequestException as e:
            print(f"Error while sending request to faucet: {str(e)}")
            return None

    def retrieve_response(self, encrypted_cipherkey_b64, timeout=60):
        start_time = time.time()
        while True:
            try:
                response = requests.post(f'{self.base_url}:{self.relay_port}/retrieve', json={"client_public_key": self.public_key_b64})
                if response.status_code == 200:
                    data = response.json()
                    if 'chat_history' in data and 'iv' in data and 'cipherkey' in data:
                        encrypted_chat_history_b64 = data['chat_history']
                        encrypted_chat_history = base64.b64decode(encrypted_chat_history_b64)
                        iv = base64.b64decode(data['iv'])
                        cipherkey = base64.b64decode(data['cipherkey'])
                        print(f"Received cipherkey: {cipherkey}")
                        print(f"Received IV: {iv}")
                        decrypted_chat_history = decrypt({'ciphertext': encrypted_chat_history, 'iv': iv}, cipherkey, self.private_key)
                        
                        if decrypted_chat_history is not None:
                            print(f"Response from AI: {decrypted_chat_history.decode('utf-8')}")
                            return json.loads(decrypted_chat_history.decode('utf-8'))
                        else:
                            print("Decryption failed. Skipping this response.")
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

    def send_message(self, message):
        self.chat_history.append({"role": "user", "content": message})
        server_public_key = self.get_server_public_key()
        
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
                    response = self.retrieve_response(encrypted_cipherkey_b64)
                    if response:
                        self.chat_history = response
                        return response
                    
                    elapsed_time = time.time() - start_time
                    if elapsed_time > timeout:
                        print("Timeout while waiting for response.")
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
            print("Chat history:", response)
        else:
            print("Failed to get response from the server.")

    print("Goodbye!")

if __name__ == "__main__":
    main()