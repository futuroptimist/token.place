import base64
from flask import Flask, request, jsonify
import json
import os
import requests
import time
from encrypt import generate_keys, encrypt_message_with_public_key, decrypt_message_with_private_key, encrypt_longer_message_with_aes, decrypt_aes_encrypted_message
from llama_cpp import Llama
from threading import Thread

# Load the ENVIRONMENT variable from .env or set a default value
ENVIRONMENT = os.getenv('ENVIRONMENT', 'dev')  # Default to 'dev' if not set

# Set the base URL based on the ENVIRONMENT
BASE_URL = 'https://token.place' if ENVIRONMENT == 'prod' else 'http://localhost:5000'

app = Flask(__name__)

URL = 'https://huggingface.co/TheBloke/Llama-2-7B-Chat-GGUF/resolve/main/llama-2-7b-chat.Q4_K_M.gguf'
file_name = os.path.basename(URL)
CHUNK_SIZE_MB = 16  # Chunk size in MB

# Initialize Llama model once (moved model initialization here)
model_path = f"models/{file_name}"
llm = None
if not os.path.exists(model_path):
    print(f"Error: Model file {model_path} does not exist.")
else:
    # Initialize Llama model if it's available
    llm = Llama(
        model_path=model_path,
        n_gpu_layers=-1,
        n_ctx=4096,
        chat_format="llama-2"
    )

    if llm is None:
        print("Error: Failed to initialize Llama model.")

_private_key, _public_key = generate_keys()

_public_key_b64 = base64.b64encode(_public_key).decode('utf-8')

def create_models_directory():
    models_dir = 'models/'
    if not os.path.exists(models_dir):
        os.makedirs(models_dir)
    return models_dir

def download_file_in_chunks(file_path, url, chunk_size_mb):
    chunk_size_bytes = chunk_size_mb * 1024 * 1024  # Convert MB to bytes
    response = requests.get(url, stream=True)

    if response.status_code != 200:
        print(f"Error: Unable to download file, status code {response.status_code}")
        return False

    total_size_in_bytes = int(response.headers.get('content-length', 0))
    if total_size_in_bytes == 0:
        print("Error: Content-Length header is missing or zero.")
        return False

    total_size_in_mb = total_size_in_bytes / (1024 * 1024)
    progress = 0
    start_time = time.time()
    times = []
    bytes_downloaded = []

    try:
        with open(file_path, 'wb') as file:
            for data in response.iter_content(chunk_size=chunk_size_bytes):
                if not data:
                    print("Warning: Received empty data chunk.")
                    continue

                file.write(data)
                file.flush()
                os.fsync(file.fileno())

                elapsed_time = time.time() - start_time
                progress += len(data)
                times.append(elapsed_time)
                bytes_downloaded.append(progress)

                # Keep only the last 10 seconds of data
                times = [t for t in times if elapsed_time - t <= 10]
                bytes_downloaded = bytes_downloaded[-len(times):]

                # Calculate speed and estimated time remaining
                speed = sum(bytes_downloaded) / sum(times) if times else 0
                eta = (total_size_in_bytes - progress) / speed if speed else 0

                downloaded_mb = progress / (1024 * 1024)
                done = int(50 * progress / total_size_in_bytes)
                print(f'\r[{"=" * done}{" " * (50-done)}] {progress * 100 / total_size_in_bytes:.2f}% ({downloaded_mb:.2f}/{total_size_in_mb:.2f} MB) ETA: {eta:.2f}s', end='\r')
    except Exception as e:
        print(f"Error during file download: {e}")
        return False

    if os.path.exists(file_path) and os.path.getsize(file_path) == total_size_in_bytes:
        print(f"\nFile Size Immediately After Download: {os.path.getsize(file_path)} bytes")
        return True
    else:
        print("\nDownload failed or file size does not match.")
        return False

def download_file_if_not_exists(models_dir, url):
    file_path = os.path.join(models_dir, file_name)
    if not os.path.exists(file_path):
        print(f"Downloading {file_name}...")
        if download_file_in_chunks(file_path, url, CHUNK_SIZE_MB):
            print(f"Download completed!")
        else:
            print(f"Download failed or file is empty.")
    else:
        print(f"File {file_name} already exists.")

def poll_relay():
    while True:
        try:
            response = requests.post(f'{BASE_URL}/sink', json={
                'server_public_key': _public_key_b64,
            })
            if response.status_code == 200:
                data = response.json()
                if 'client_public_key' in data and 'chat_history' in data:
                    encrypted_chat_history_b64 = data['chat_history']

                    print("Encoded chat history length:", len(encrypted_chat_history_b64))
                    # Adjust the padding if necessary
                    if len(encrypted_chat_history_b64) % 4 != 0:
                        encrypted_chat_history_b64 += '=' * (4 - len(encrypted_chat_history_b64) % 4)

                    try:
                        encrypted_chat_history = base64.b64decode(encrypted_chat_history_b64)
                    except Exception as e:
                        print(f"Exception decoding base64 data: {e}")
                        continue

                    try:
                        encrypted_chat_history = base64.b64decode(encrypted_chat_history_b64)
                    except Exception as e:
                        print(f"Exception decoding base64 data: {e}")
                        continue

                    decrypted_chat_history = decrypt_message_with_private_key(encrypted_chat_history, _private_key)
                    chat_history_obj = json.loads(decrypted_chat_history)
                    response_history = llama_cpp_get_response(chat_history_obj)
                    client_pub_key_b64 = data['client_public_key']
                    encrypted_aes_key, iv, encrypted_response = encrypt_longer_message_with_aes(json.dumps(response_history).encode('utf-8'), base64.b64decode(client_pub_key_b64))
                    encrypted_response_b64 = base64.b64encode(encrypted_response).decode('utf-8')
                    encrypted_aes_key_b64 = base64.b64encode(encrypted_aes_key).decode('utf-8')
                    iv_b64 = base64.b64encode(iv).decode('utf-8')

                    requests.post(f'{BASE_URL}/source', json={
                        'client_public_key': client_pub_key_b64,
                        'encrypted_aes_key': encrypted_aes_key_b64,
                        'iv': iv_b64,
                        'chat_history': encrypted_response_b64,
                    })
                    print("Response sent.")
                time.sleep(data.get('next_ping_in_x_seconds', 10))
            else:
                print("Error from relay:", response.status_code, response.text)
                time.sleep(10)
        except Exception as e:
            print(f"Exception during polling: {e}")
            time.sleep(10)

def llama_cpp_get_response(chat_history):
    """Process chat history with the Llama model and generate a response."""
    # This is a placeholder for your actual Llama model integration
    # Replace with your actual model invocation logic
    try:
        response = llm.create_chat_completion(messages=chat_history)
        if response and 'choices' in response and response['choices']:
            assistant_message = response['choices'][0]['message']
            chat_history.append(assistant_message)
    except Exception as e:
        print(f"Error during chat completion: {e}")
    return chat_history

@app.route('/', methods=['POST'])
def process_message():
    """Process incoming chat messages and return model responses."""
    if request.method == 'POST':
        data = request.get_json()
        # Validate if 'chat_history' key exists in the request data
        if 'chat_history' not in data:
            return jsonify({'error': 'Invalid request format'}), 400
        chat_history = data.get('chat_history', [])
        updated_chat_history = llama_cpp_get_response(chat_history)
        return jsonify(updated_chat_history)
    else:
        return jsonify({'error': 'Method not allowed'}), 405
    
@app.errorhandler(405)
def method_not_allowed(error):
    """Custom error handler for Method Not Allowed (405) errors."""
    return jsonify({'error': 'Method not allowed'}), 405

if __name__ == '__main__':
    models_dir = create_models_directory()
    download_file_if_not_exists(models_dir, URL)
    Thread(target=poll_relay, daemon=True).start()
    app.run(host='0.0.0.0', port=3000)