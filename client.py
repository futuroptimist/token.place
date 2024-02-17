import requests
import json
import base64
from encrypt import generate_keys, encrypt_message, decrypt_message, public_key_from_pem

# Generate client RSA keys
_private_key, _public_key = generate_keys()

# Convert client's public key to PEM format for transmission
client_public_key_pem = _public_key.save_pkcs1().decode('utf-8')

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
        
        # Send encrypted chat history to the faucet endpoint
        response_faucet = send_request_to_faucet(encrypted_chat_history, server_public_key)
        if response_faucet.status_code != 200:
            print("Failed to send encrypted chat to faucet.")

    # Continue to use the /inference endpoint as before
    data = {
        "message": user_message,
        "chat_history": chat_history
    }

    # Send request to the /inference endpoint
    try:
        response = requests.post(relay_url, json=data)
        if response.status_code == 200:
            response_data = response.json()
            
            # Iterate through each message in the response
            for message in response_data:
                if message["role"] == "assistant":
                    print("AI:", message["content"])
                    break 

        else:
            print(f"Error {response.status_code}: The server encountered an issue.")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
print("Goodbye!")
