# token.place
p2p generative AI marketplace

# vision
There are tons of personal computers and homelabs out there with lots of compute that remain idle. This project aims to create a marketplace of people with spare compute and people with needs for compute. Note that this is not a financial marketplace -- this is intended to be a public good. If it takes off is anyone's guess, but I'll donate whatever compute I can in the meantime once this is up and running.

## roadmap

- [x] hello world: it literally just echoes your message param back to you
- [x] find an initial model to support (llama 2 7b chat gguf)
- [x] download model programmatically on device
- [ ] load the model and successfully run it
- [ ] do inference over HTTP (including creation of a client.py)

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

`pip install -r requirements.txt`

start the script:

```sh
python server.py
```

NOTE: When first launched, or if the model file isn't present (currently only [Llama 7B Chat GGUF by TheBloke](https://huggingface.co/TheBloke/Llama-2-7B-Chat-GGUF)), the script will download the model (approximately 4GB) and will save it in the `models/` directory in your project directory under the same filename. This will be gated by user interaction in the future to prevent large file downloads without the user's consent. Eventually you'll basically browse models and choose one from a list.

toy example of simple HTTP server (DON'T USE IN PRODUCTION!!):

1. navigate to http://localhost:3000/?message=YOUR_MESSAGE_HERE

2. edit the message param and visit the page to see your message update