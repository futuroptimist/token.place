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

def get_server_public_key():
    """Fetch the server's public key from the relay."""
    response = requests.get(f'{base_url}/next_server')
    if response.status_code == 200:
        data = response.json()
        server_public_key_b64 = data['server_public_key']
        return base64.b64decode(server_public_key_b64)
    else:
        print("Could not fetch server's public key. Please try again later.")
        return None

def encrypt_chat_history(chat_history, server_public_key):
    """Encrypt the chat history using the server's public key for longer messages."""
    message_bytes = json.dumps(chat_history).encode('utf-8')
    encrypted_aes_key, iv, encrypted_message = encrypt_longer_message_with_aes(message_bytes, server_public_key)
    return encrypted_aes_key, iv, encrypted_message

def send_request_to_faucet(encrypted_chat_history_b64, server_public_key_b64, encrypted_cipherkey_b64):
    """Send the encrypted chat history to the faucet endpoint."""
    data = {
        "client_public_key": _public_key_b64,
        "server_public_key": server_public_key_b64,
        "chat_history": encrypted_chat_history_b64,
        "cipherkey": encrypted_cipherkey_b64
    }
    response = requests.post(f'{base_url}/faucet', json=data)
    return response

def retrieve_response(encrypted_cipherkey_b64):
    while True:
        response = requests.post(f'{base_url}/retrieve', json={"client_public_key": _public_key_b64})
        if response.status_code == 200:
            data = response.json()
            if 'chat_history' in data:
                encrypted_chat_history_b64 = data['chat_history']
                encrypted_chat_history = base64.b64decode(encrypted_chat_history_b64)
                cipherkey = base64.b64decode(encrypted_cipherkey_b64)
                decrypted_chat_history = decrypt({'ciphertext': encrypted_chat_history}, cipherkey, _private_key)
                print("Response from AI:", decrypted_chat_history.decode('utf-8'))
                break
            else:
                print("Response data is incomplete, waiting for complete response...")
                time.sleep(2)
        else:
            print("Waiting for response...")
            time.sleep(2)

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
            ciphertext, cipherkey = encrypt(json.dumps(chat_history).encode('utf-8'), server_public_key)
            encrypted_chat_history_b64 = base64.b64encode(ciphertext['ciphertext']).decode('utf-8')
            encrypted_cipherkey_b64 = base64.b64encode(cipherkey).decode('utf-8')
            response_faucet = send_request_to_faucet(encrypted_chat_history_b64, base64.b64encode(server_public_key).decode('utf-8'), encrypted_cipherkey_b64)
            if response_faucet.status_code == 200:
                print("Request sent successfully, waiting for response...")
                retrieve_response(encrypted_cipherkey_b64)
            else:
                print("Failed to send encrypted chat to faucet.")

    print("Goodbye!")

if __name__ == "__main__":
    main()