# token.place
p2p generative AI platform

# vision
There are tons of personal computers and homelabs out there with lots of compute that remain idle. This project aims to create a marketplace of people with spare compute and people with needs for compute. Note that this is not a financial marketplace -- this is intended to be a public good. If it takes off is anyone's guess, but I'll donate whatever compute I can in the meantime once this is up and running.

## roadmap

- [x] hello world: it literally just echoes your message param back to you
- [x] find an initial model to support (llama 2 7b chat gguf)
- [x] download model programmatically on device
- [x] load the model and successfully run it
- [x] do inference over HTTP
- [x] multi-step dialogue
- [x] relay.py, which passes plaintext requests from client to a server (hardcoded URL, run locally for now) and the response back to the client.
- [ ] end-to-end encrypt communication between server and client with public key cryptography (server generates public/private key pair on init and gives public key to relay, which passes it on to the server [but does not reveal server's IP address])
  - [x] IP obfuscation
  - [x] end-to-end encryption (short responses, under 256 bytes of utf-8 encoded text) for client.py
  - [x] end-to-end encryption for longer responses for client.py
  - [x] integration test demonstrating the above
  - [x] Llama 2 -> 3 (7B -> 8B)
  - [ ] end-to-end encryption on landing page (relay.py / GET)
  - [ ] automated browser testing of landing page, preventing regressions of the chat ui
  - [ ] delete old /inference endpoint and everything upstream and downstream that's now unused
- [ ] set up production server (raspberry pi cluster lol)
- [ ] bandwidth improvements
- [ ] allow participation from other server.pys
- [ ] allow users to specify which model they want from a growing list

## installation

### virtual environment

create a virtual environment:

```sh
$ python -m venv env
```

activate the virtual environment:

#### unix/linux/macos

```sh
source env/bin/activate
```

#### windows

```sh
.\env\Scripts\activate
```

If this command doesn't work (e.g. `Activate.ps1 cannot be loaded because running scripts is disabled on this system`), you may have to run the following command in an Administrator PowerShell session:

```sh
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

### install dependencies

```
pip install -r requirements.txt
```

### hardware acceleration

If you want to also utilize your GPU (instead of just your CPU), follow these platform-specific instructions.

#### windows

This is the resource I used to get things finally working: https://medium.com/@piyushbatra1999/installing-llama-cpp-python-with-nvidia-gpu-acceleration-on-windows-a-short-guide-0dfac475002d

Summarizing:

**Prerequisites**

- Visual Studio with
  - C++ CMake tools for windows
  - C++ core features
  - Windows 10/11 SDK
- [CUDA Toolkit](https://developer.nvidia.com/cuda-12-2-0-download-archive?target_os=Windows)

The next steps need to be executed in the same virtual environment you set up above. You'll see something like (env) on the bottom left in your terminal (may not be true on all platforms in all terminals).

This will replace the llama-cpp-python you installed via `pip install -r requirements.txt` and will instruct it to use [cuBLAS](https://docs.nvidia.com/cuda/cublas/index.html).

**if you're using Command Prompt**

```
set CMAKE_ARGS=-DLLAMA_CUBLAS=on
set FORCE_CMAKE=1
pip install llama-cpp-python --force-reinstall --upgrade --no-cache-dir --verbose
```

**if you're using Powershell**

```
$env:CMAKE_ARGS = "-DLLAMA_CUBLAS=on"
$env:FORCE_CMAKE=1
pip install llama-cpp-python --force-reinstall --upgrade --no-cache-dir --verbose
```

when you run `server.py` next, you'll see `BLAS = 1` in a collection of lines that looks like this:

```
AVX = 1 | AVX_VNNI = 0 | AVX2 = 1 | AVX512 = 0 | AVX512_VBMI = 0 | AVX512_VNNI = 0 | FMA = 1 | NEON = 0 | ARM_FMA = 0 | F16C = 1 | FP16_VA = 0 | WASM_SIMD = 0 | BLAS = 1 | SSE3 = 1 | SSSE3 = 0 | VSX = 0 |
```

This indicates that `server.py` can correctly access your GPU resources.

llama_cpp_python is initialized like this:

```py
llm = Llama(
        model_path=model_path,
        n_gpu_layers=-1,
        n_ctx=8192,
        chat_format="llama-3"
    )
```

`n_gpu_layers` instructs llama to use as much of your GPU resources as possible.

#### macos

TODO

#### unix/linux

TODO

## Running the servers

### Dev-like environment

You'll need a way to switch between terminal tabs (e.g. tmux, VS Code terminal tabs).

Launch the relay, which runs on http://localhost:5000:

```sh
python relay.py
```

Then, in a separate terminal tab, launch the server, which runs on http://localhost:3000:

```sh
python server.py
```

NOTE: When first launched, or if the model file isn't present (currently only [Llama 7B Chat GGUF by TheBloke](https://huggingface.co/TheBloke/Llama-2-7B-Chat-GGUF)), the script will download the model (approximately 4GB) and will save it in the `models/` directory in your project directory under the same filename. This will be gated by user interaction in the future to prevent large file downloads without the user's consent. Eventually you'll basically browse models and choose one from a list.

`relay.py` acts as a proxy between the client (including, but not limited to, this repo's `client.py`) and `server.py`, obfuscating each other's public IP from each other, solving one of the big limitations of P2P networks (e.g. for .torrents). In a future version, `relay.py` will not see the contents of the conversation between server and client thanks to end-to-end encryption. Anyone can fork this project and run your own relay, which have compute provided by various `server.py`s running on various consumer hardware.

You can test things out using the simple command-line client, `client.py`:

```sh
python client.py
```

Type your message when prompted and press Enter. All of this is now happening on your local hardware, thanks to `llama_cpp_python`, a binding for llama.cpp.

To exit, press Ctrl+C/Cmd+C.

Alternatively, you can visit http://localhost:5000 in your browser.

### Production-like environment

TODO
