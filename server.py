from flask import Flask, request

app = Flask(__name__)

@app.route('/', methods=['GET'])
def echo_message():
    if request.method == 'GET' and 'message' in request.args:
        message = request.args.get('message')
        return message
    else:
        return 'Invalid request'

if __name__ == '__main__':
    app.run(port=3000)
