import os
import requests
import time
from flask import Flask, request, jsonify

# Import Llama
from llama_cpp import Llama

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
        n_ctx=2048,
        chat_format="llama-2"
    )

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

def llama_cpp_get_response(chat_history, message):
    try:
        # Append the new user message to the received chat history
        chat_history.append({"role": "user", "content": message})

        response = llm.create_chat_completion(messages=chat_history)

        # Append the model's response to the chat history
        chat_history.append({"role": "assistant", "content": response})

        return chat_history  # Return the updated chat history
    except Exception as e:
        print(f"Error during chat completion: {e}")
        return [{"role": "user", "content": message}, {"role": "assistant", "content": "Error during chat completion."}]

# Handle POST requests
@app.route('/', methods=['POST'])
def process_message():
    if request.method == 'POST':
        # Parse the JSON data from the request body
        data = request.json
        message = data.get('message')
        chat_history = data.get('chat_history', [])

        # Log the chat history and message for debugging
        print(f"Received message: {message}")
        print(f"Received chat history: {chat_history}")

        updated_chat_history = llama_cpp_get_response(chat_history, message)

        # Log the updated chat history for debugging
        print(f"Updated chat history: {updated_chat_history}")

        # Return the updated chat history as JSON
        return jsonify(updated_chat_history)
    else:
        return 'Invalid request'

if __name__ == '__main__':
    models_dir = create_models_directory()
    download_file_if_not_exists(models_dir, URL)
    app.run(host='0.0.0.0', port=3000) # Flask app runs on port 3000 internally