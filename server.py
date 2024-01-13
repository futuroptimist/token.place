import os
import requests
import time
from flask import Flask, request

app = Flask(__name__)

URL = 'https://huggingface.co/TheBloke/Llama-2-7B-Chat-GGUF/resolve/main/llama-2-7b-chat.Q4_K_M.gguf'
CHUNK_SIZE_MB = 16  # Chunk size in MB

def create_models_directory():
    models_dir = 'models/'
    if not os.path.exists(models_dir):
        os.makedirs(models_dir)
    return models_dir

def download_file_in_chunks(file_path, url, chunk_size_mb):
    chunk_size_bytes = chunk_size_mb * 1024 * 1024  # Convert MB to bytes
    response = requests.get(url, stream=True)
    total_size_in_bytes = int(response.headers.get('content-length', 0))
    total_size_in_mb = total_size_in_bytes / (1024 * 1024)
    progress = 0
    start_time = time.time()
    times = []
    bytes_downloaded = []

    with open(file_path, 'wb') as file:
        for data in response.iter_content(chunk_size=chunk_size_bytes):
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

def download_file_if_not_exists(models_dir, url):
    file_name = os.path.basename(url)
    file_path = os.path.join(models_dir, file_name)
    if not os.path.exists(file_path):
        print(f"Downloading {file_name}...")
        download_file_in_chunks(file_path, url, CHUNK_SIZE_MB)
        print(f"\nDownload completed!")

@app.route('/', methods=['GET'])
def echo_message():
    if request.method == 'GET' and 'message' in request.args:
        message = request.args.get('message')
        return message
    else:
        return 'Invalid request'

if __name__ == '__main__':
    models_dir = create_models_directory()
    download_file_if_not_exists(models_dir, URL)
    app.run(port=3000)
