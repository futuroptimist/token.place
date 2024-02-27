import requests
import json
import base64
import time
import os
from encrypt import generate_keys, encrypt_message, decrypt_message, public_key_from_pem

# Generate client RSA keys
_private_key, _public_key = generate_keys()

# Convert client's public key to PEM format for transmission
client_public_key_pem = _public_key.save_pkcs1().decode('utf-8')

# Use an environment variable to determine the environment
environment = os.getenv('ENVIRONMENT', 'dev')  # Default to 'dev' if not set

# Choose the base URL based on the environment
base_url = 'http://token.place' if environment == 'prod' else 'http://localhost:5000'

def get_server_public_key():
    """Fetch the server's public key from the relay."""
    response = requests.get('http://localhost:5000/next_server')
    if response.status_code == 200:
        data = response.json()
        return data['server_public_key']
    else:
        print("Could not fetch server's public key. Please try again later.")
        return None

def encrypt_chat_history(chat_history, server_public_key_pem):
    """Encrypt the chat history using the server's public key."""
    # Load the server's public key from the PEM string
    server_public_key = public_key_from_pem(server_public_key_pem)
    
    # Serialize chat history to a JSON string and encode to bytes
    message_bytes = json.dumps(chat_history).encode('utf-8')
    
    # Encrypt the chat history using the server's public key
    encrypted_message = encrypt_message(message_bytes, server_public_key)
    
    # Return the encrypted chat history
    return encrypted_message

def send_request_to_faucet(encrypted_chat_history, server_public_key):
    """Send the encrypted chat history to the faucet endpoint."""
    # Encode the encrypted chat history with base64 to safely convert it to a string
    encoded_chat_history = base64.b64encode(encrypted_chat_history).decode('utf-8')
    
    data = {
        "client_public_key": client_public_key_pem,
        "server_public_key": server_public_key,
        "chat_history": encoded_chat_history  # Use Base64-encoded string
    }
    response = requests.post('http://localhost:5000/faucet', json=data)
    return response

def retrieve_response():
    """Poll the relay for a response until it's available."""
    while True:
        response = requests.post('http://localhost:5000/retrieve', json={"client_public_key": client_public_key_pem})
        if response.status_code == 200:
            data = response.json()
            encrypted_chat_history = base64.b64decode(data['chat_history'])
            decrypted_chat_history = decrypt_message(encrypted_chat_history, _private_key)
            print("Response from AI:", decrypted_chat_history)
            break
        else:
            print("Waiting for response...")
            time.sleep(2)  # Wait a bit before trying again

relay_url = 'http://localhost:5000/inference'
chat_history = []

print("Welcome to the Chat Client!")
print("Type your messages and press Enter to send. Type 'exit' to quit.")

while True:
    user_message = input("You: ")
    if user_message.lower() == 'exit':
        break

    chat_history.append({"role": "user", "content": user_message})

    # Fetch server public key
    server_public_key = get_server_public_key()
    if server_public_key:
        # Encrypt chat history with server's public key
        encrypted_chat_history = encrypt_chat_history(chat_history, server_public_key)
        
        # Send encrypted chat history to the faucet endpoint and wait for a response
        response_faucet = send_request_to_faucet(encrypted_chat_history, server_public_key)
        if response_faucet.status_code == 200:
            print("Request sent successfully, waiting for response...")
            retrieve_response()
        else:
            print("Failed to send encrypted chat to faucet.")

print("Goodbye!")
