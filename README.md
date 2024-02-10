# token.place
p2p generative AI marketplace

# vision
There are tons of personal computers and homelabs out there with lots of compute that remain idle. This project aims to create a marketplace of people with spare compute and people with needs for compute. Note that this is not a financial marketplace -- this is intended to be a public good. If it takes off is anyone's guess, but I'll donate whatever compute I can in the meantime once this is up and running.

## roadmap

- [x] hello world: it literally just echoes your message param back to you
- [x] find an initial model to support (llama 2 7b chat gguf)
- [x] download model programmatically on device
- [x] load the model and successfully run it
- [x] do inference over HTTP
- [x] multi-step dialogue
- [ ] set up production server (raspberry pi cluster lol)
- [ ] relay.py, which passes plaintext requests from client to a server (relay chooses one) and the response back to the client.
- [ ] end-to-end encrypt communication between server and client with public key cryptography (server generates public/private key pair on init and gives public key to relay, which passes it on to the server [but does not reveal server's IP address])
- [ ] bandwidth improvements
- [ ] allow participation from other server.pys
- [ ] allow users to specify which model they want from a growing list

## usage

create a virtual environment:

```sh
$ python -m venv env
```

activate the virtual environment:

### windows

```sh
.\env\Scripts\activate
```

If this command doesn't work (e.g. `Activate.ps1 cannot be loaded because running scripts is disabled on this system`), you may have to run the following command in an Administrator PowerShell session:

```sh
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

### unix/macos

```sh
source env/bin/activate
```

install requirements.txt:

```
pip install -r requirements.txt
```

You can also use Docker:

Build the Docker image (e.g. named `tokenplace` using the `Dockerfile` and run:

```sh
$ docker run -p 80:80 tokenplace
```

Alternatively, you can run:

```sh
$ docker-compose up --build
```

start the script:

NOTE: When first launched, or if the model file isn't present (currently only [Llama 7B Chat GGUF by TheBloke](https://huggingface.co/TheBloke/Llama-2-7B-Chat-GGUF)), the script will download the model (approximately 4GB) and will save it in the `models/` directory in your project directory under the same filename. This will be gated by user interaction in the future to prevent large file downloads without the user's consent. Eventually you'll basically browse models and choose one from a list.

run the server locally (NOT READY FOR PRODUCTION!!!):

```sh
python server.py
```

Navigate to http://localhost:3000/?message=YOUR_PROMPT_HERE.

Llama will reply once the model is loaded (usually takes around 20 seconds for me).

run the client script, which demonstrates server usage:

```sh
python client.py
```

The client script will start, and you can type your messages and press Enter to send them to the server. The server will reply with responses generated by the AI model.