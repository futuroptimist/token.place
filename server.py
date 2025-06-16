import base64
from flask import Flask, request, jsonify
import json
import os
import requests
import time
from encrypt import encrypt, decrypt, generate_keys
from llama_cpp import Llama
from threading import Thread, Lock
import argparse
from unittest.mock import MagicMock # Import MagicMock
import logging
from config import get_config

# Get configuration instance
config = get_config()

# Configure logging based on environment
if not config.is_production:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    logger = logging.getLogger('server')
else:
    # In production, set up a null handler to suppress all logs
    logging.basicConfig(handlers=[logging.NullHandler()])
    logger = logging.getLogger('server')

def log_info(message):
    """Log info only in non-production environments"""
    if not config.is_production:
        logger.info(message)

def log_warning(message):
    """Log warnings only in non-production environments"""
    if not config.is_production:
        logger.warning(message)

def log_error(message, exc_info=False):
    """Log errors only in non-production environments"""
    if not config.is_production:
        logger.error(message, exc_info=exc_info)

# Llama model configuration
file_name = config.get('model.filename', 'llama-3-8b-instruct.Q4_K_M.gguf')
URL = config.get('model.url', 'https://huggingface.co/TheBloke/Llama-3-8B-Instruct-GGUF/resolve/main/llama-3-8b-instruct.Q4_K_M.gguf')
CHUNK_SIZE_MB = config.get('model.download_chunk_size_mb', 10)
model_path = os.path.join(config.get('paths.models_dir'), file_name)

# Initialize Flask app
app = Flask(__name__)

# Initialize variables
llm = None  # Will be initialized on first use
llm_lock = Lock()  # For thread safety
_private_key = None  # Will be set during initialization
_public_key = None
_public_key_b64 = None

# Check for USE_MOCK_LLM - keep backward compatibility but use new config system
USE_MOCK_LLM = config.get('model.use_mock', False) or os.getenv('USE_MOCK_LLM') == '1'

def create_models_directory():
    models_dir = config.get('paths.models_dir')
    if not os.path.exists(models_dir):
        os.makedirs(models_dir)
    return models_dir

def download_file_in_chunks(file_path, url, chunk_size_mb):
    chunk_size_bytes = chunk_size_mb * 1024 * 1024  # Convert MB to bytes
    response = requests.get(url, stream=True)

    if response.status_code != 200:
        log_error(f"Error: Unable to download file, status code {response.status_code}")
        return False

    total_size_in_bytes = int(response.headers.get('content-length', 0))
    if total_size_in_bytes == 0:
        log_error("Error: Content-Length header is missing or zero.")
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
                    log_warning("Warning: Received empty data chunk.")
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
                if not config.is_production:
                    print(f'\r[{"=" * done}{" " * (50-done)}] {progress * 100 / total_size_in_bytes:.2f}% ({downloaded_mb:.2f}/{total_size_in_mb:.2f} MB) ETA: {eta:.2f}s', end='\r')
    except Exception as e:
        log_error(f"Error during file download: {e}")
        return False

    if os.path.exists(file_path) and os.path.getsize(file_path) == total_size_in_bytes:
        log_info(f"File Size Immediately After Download: {os.path.getsize(file_path)} bytes")
        return True
    else:
        log_error("Download failed or file size does not match.")
        return False

def download_file_if_not_exists(models_dir, url):
    file_path = os.path.join(models_dir, file_name)
    if not os.path.exists(file_path):
        log_info(f"Downloading {file_name}...")
        if download_file_in_chunks(file_path, url, CHUNK_SIZE_MB):
            log_info(f"Download completed!")
        else:
            log_error(f"Download failed or file is empty.")
    else:
        log_info(f"File {file_name} already exists.")

def poll_relay(base_url, relay_port):
    """
    Continuously poll the relay for new chat messages and process them.

    Args:
        base_url (str): The base URL of the relay server.
        relay_port (int): The port number of the relay server.
    """
    while True:
        try:
            log_info(f"[Server Poll] Pinging relay {base_url}:{relay_port}/sink with key {_public_key_b64[:10]}...")
            response = requests.post(f'{base_url}:{relay_port}/sink', json={'server_public_key': _public_key_b64})
            
            if response.status_code == 200:
                data = response.json()
                log_info("[Server Poll] Received data from relay")
                
                if 'client_public_key' in data and 'chat_history' in data and 'cipherkey' in data and 'iv' in data and data['iv']:
                    log_info("[Server Poll] Processing client request...")
                    encrypted_chat_history_b64 = data['chat_history']
                    client_pub_key_b64 = data['client_public_key']
                    
                    try:
                        iv = base64.b64decode(data['iv'])
                        encrypted_chat_history_dict = {'ciphertext': base64.b64decode(encrypted_chat_history_b64), 'iv': iv}
                        cipherkey = base64.b64decode(data['cipherkey'])
                        
                        log_info("[Server Poll] Decrypting request...")
                        decrypted_chat_history = decrypt(encrypted_chat_history_dict, cipherkey, _private_key)
                        
                        if decrypted_chat_history is None:
                            log_info("[Server Poll] Decryption failed. Skipping.")
                            continue
                            
                        log_info("[Server Poll] Decrypted request received")
                        chat_history_obj = json.loads(decrypted_chat_history)
                        
                        log_info("[Server Poll] Getting response from LLM...")
                        response_history = llama_cpp_get_response(chat_history_obj) # This uses get_llm_instance()
                        log_info("[Server Poll] LLM response generated")

                        log_info("[Server Poll] Encrypting response for client...")
                        client_pub_key = base64.b64decode(client_pub_key_b64)
                        encrypted_response, encrypted_cipherkey, iv_resp = encrypt(json.dumps(response_history).encode('utf-8'), client_pub_key)
                        encrypted_response_b64 = base64.b64encode(encrypted_response['ciphertext']).decode('utf-8')
                        iv_resp_b64 = base64.b64encode(iv_resp).decode('utf-8')
                        encrypted_cipherkey_b64 = base64.b64encode(encrypted_cipherkey).decode('utf-8')

                        source_payload = {
                            'client_public_key': client_pub_key_b64,
                            'chat_history': encrypted_response_b64,
                            'cipherkey': encrypted_cipherkey_b64,
                            'iv': iv_resp_b64
                        }
                        log_info(f"[Server Poll] Posting response to {base_url}:{relay_port}/source. Payload keys: {list(source_payload.keys())}")
                        
                        source_response = requests.post(f'{base_url}:{relay_port}/source', json=source_payload)
                        log_info(f"[Server Poll] Response sent to /source. Status: {source_response.status_code}, Text: {source_response.text}")

                    except Exception as e:
                        log_error(f"[Server Poll] Exception during request processing: {e}", exc_info=True) # Add traceback
                        continue
                else:
                    log_info("[Server Poll] No client request data in sink response.")
                
                sleep_duration = data.get('next_ping_in_x_seconds', 10)
                log_info(f"[Server Poll] Sleeping for {sleep_duration} seconds...")
                time.sleep(sleep_duration)
            else:
                log_error(f"[Server Poll] Error from relay /sink: {response.status_code} {response.text}")
                time.sleep(10)
        except Exception as e:
            log_error(f"[Server Poll] Exception during polling loop: {e}", exc_info=True) # Add traceback
            time.sleep(10)

def get_llm_instance():
    """Gets the Llama instance, initializing it if necessary (thread-safe), or returns a mock if USE_MOCK_LLM is set."""
    global llm
    # Check if mocking is enabled via configuration
    if USE_MOCK_LLM:
        log_info("Using Mock LLM instance based on USE_MOCK_LLM configuration.")
        mock_llama_instance = MagicMock()
        mock_response = {
            'choices': [
                {
                    'message': {
                        'role': 'assistant',
                        # Make the mock response more specific for easier debugging
                        'content': 'Mock Response: The capital of France is Paris.'
                    }
                }
            ]
        }
        mock_llama_instance.create_chat_completion.return_value = mock_response
        return mock_llama_instance

    # Quick check without lock (for real LLM)
    if llm is None:
        # Acquire lock only if we might need to initialize
        with llm_lock:
            # Double-check after acquiring lock
            if llm is None:
                if not os.path.exists(model_path):
                    log_error(f"Error: Model file {model_path} does not exist. LLM not initialized.")
                else:
                    try:
                        log_info(f"Initializing Llama model from {model_path}...")
                        llm = Llama(
                            model_path=model_path,
                            n_gpu_layers=-1,
                            n_ctx=config.get('model.context_size', 8192),
                            chat_format=config.get('model.chat_format', 'llama-3')
                        )
                        log_info("Llama model initialized successfully.")
                    except Exception as e:
                        log_error(f"Error initializing Llama model: {e}")
                        # llm remains None
    return llm

def llama_cpp_get_response(chat_history):
    """Process chat history with the Llama model and generate a response."""
    log_info("[LLM] Requesting LLM instance...")
    model_instance = get_llm_instance() # Use the helper function
    if model_instance is None:
        log_info("[LLM] LLM not available. Returning error message.")
        # Return an error message in the chat history
        chat_history.append({"role": "assistant", "content": "Error: Model is not available or failed to load."})
        return chat_history

    try:
        log_info("[LLM] Calling create_chat_completion")
        # Use the obtained model_instance
        response = model_instance.create_chat_completion(messages=chat_history)
        log_info("[LLM] Received response from model")
        if response and 'choices' in response and response['choices']:
            assistant_message = response['choices'][0]['message']
            log_info("[LLM] Assistant message generated")
            chat_history.append(assistant_message)
            # Return the modified chat_history (already done by append)
        else:
             log_info("[LLM] No valid response/choices from model.")
             chat_history.append({"role": "assistant", "content": "Error: No response from model."}) # Add error if no choice
    except Exception as e:
        log_error(f"[LLM] Error during chat completion: {e}", exc_info=True) # Add traceback
        # Optionally append an error message here too
        chat_history.append({"role": "assistant", "content": f"Error during generation: {e}"})
    return chat_history # Return chat_history in all cases

@app.route('/', methods=['POST'])
def process_message():
    """Process incoming chat messages and return model responses."""
    if request.method == 'POST':
        data = request.get_json()
        # Validate if 'chat_history' key exists in the request data
        if 'chat_history' not in data:
            return jsonify({'error': 'Invalid request format'}), 400
        chat_history = data.get('chat_history', [
            {
                "role": "system",
                "content": "You are Llama 3 8B, a helpful assistant created by"
                "Meta. If anyone asks you what your name is, just say Llama!🦙 "
                "Make sure you use whitespace appropriately so that your "
                "responses aren't just a big blob of text, especially on smaller"
                "device widths."
            }
        ])
        updated_chat_history = llama_cpp_get_response(chat_history)
        return jsonify(updated_chat_history)
    else:
        return jsonify({'error': 'Method not allowed'}), 405
    
@app.errorhandler(405)
def method_not_allowed(_):
    """Custom error handler for Method Not Allowed (405) errors."""
    return jsonify({'error': 'Method not allowed'}), 405

def main():
    """Main function to start the server."""
    global _private_key, _public_key, _public_key_b64
    
    parser = argparse.ArgumentParser(description='Start the server with specific ports')
    parser.add_argument('--server_port', type=int, default=config.get('server.port'), help='Port for the server')
    parser.add_argument('--relay_port', type=int, default=config.get('relay.port'), help='Port for the relay')
    parser.add_argument('--use_mock_llm', action='store_true', help='Use mock LLM implementation')
    args = parser.parse_args()

    SERVER_PORT = args.server_port
    RELAY_PORT = args.relay_port
    if args.use_mock_llm:
        os.environ['USE_MOCK_LLM'] = '1'
        global USE_MOCK_LLM
        USE_MOCK_LLM = True
    BASE_URL = config.get('server.base_url', 'http://localhost')
    SERVER_HOST = config.get('server.host', '0.0.0.0')
    
    # Generate RSA keys
    log_info("Generating RSA keys...")
    _private_key, _public_key = generate_keys()
    _public_key_b64 = base64.b64encode(_public_key).decode('utf-8')
    log_info("RSA keys generated successfully!")
    
    # Create models directory and download model if needed
    models_dir = create_models_directory()
    download_file_if_not_exists(models_dir, URL)
    
    # Start the polling thread to interact with the relay
    log_info(f"Starting polling thread for relay at {BASE_URL}:{RELAY_PORT}...")
    poll_thread = Thread(target=poll_relay, args=(BASE_URL, RELAY_PORT), daemon=True)
    poll_thread.start()
    
    # Start the Flask app
    log_info(f"Starting Flask app on {SERVER_HOST}:{SERVER_PORT}...")
    app.run(host=SERVER_HOST, port=SERVER_PORT, debug=config.get('server.debug', False), use_reloader=False)

if __name__ == "__main__":
    main()
