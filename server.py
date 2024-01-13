import os
import requests
from flask import Flask, request

app = Flask(__name__)

def create_models_directory():
    models_dir = 'models/'
    if not os.path.exists(models_dir):
        os.makedirs(models_dir)
    return models_dir

def download_file_if_not_exists(models_dir, file_name, url):
    file_path = os.path.join(models_dir, file_name)
    if not os.path.exists(file_path):
        response = requests.get(url)
        with open(file_path, 'wb') as file:
            file.write(response.content)

@app.route('/', methods=['GET'])
def echo_message():
    if request.method == 'GET' and 'message' in request.args:
        message = request.args.get('message')
        return message
    else:
        return 'Invalid request'

if __name__ == '__main__':
    models_dir = create_models_directory()
    download_file_if_not_exists(models_dir, 'file.zip', 'https://example.com/file.zip')
    app.run(port=3000)
