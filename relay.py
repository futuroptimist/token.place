from flask import Flask, send_from_directory, request, jsonify
import requests
import os

app = Flask(__name__)

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

# Generic route for serving static files
@app.route('/static/<path:path>')
def serve_static(path):
    return send_from_directory('static', path)

@app.route('/inference', methods=['POST'])
def inference():
    # Get JSON data from the incoming request
    data = request.get_json()

    # Define the URL to which we will forward the request
    url = 'http://localhost:3000/'

    # Forward the POST request to the other service and get the response
    response = requests.post(url, json=data)

    # Return the response received from the other service to the client
    return jsonify(response.json()), response.status_code

 
if __name__ == '__main__':
    app.run(port=5000)  # Flask app runs on port 5000 internally
