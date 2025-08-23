# token.place

[![Lint & Format][ci-lint-badge]][ci-workflow]
[![Tests][ci-tests-badge]][ci-workflow]
[![Coverage][coverage-badge]][coverage-url]
[![Docs][ci-docs-badge]][ci-workflow]
[![License][license-badge]](LICENSE)
[![Dependabot][dependabot-badge]][dependabot-url]
[![CodeQL][codeql-badge]][codeql-url]
[![Secret Scanning][secret-badge]][secret-url]

[ci-workflow]: https://github.com/futuroptimist/token.place/actions/workflows/ci.yml
[ci-lint-badge]: https://img.shields.io/github/actions/workflow/status/futuroptimist/token.place/ci.yml?label=lint-format
[ci-tests-badge]: https://img.shields.io/github/actions/workflow/status/futuroptimist/token.place/ci.yml?label=tests
[ci-docs-badge]: https://img.shields.io/github/actions/workflow/status/futuroptimist/token.place/ci.yml?label=docs
[coverage-badge]: https://codecov.io/gh/futuroptimist/token.place/graph/badge.svg?branch=main
[coverage-url]: https://codecov.io/gh/futuroptimist/token.place
[license-badge]: https://img.shields.io/github/license/futuroptimist/token.place
[dependabot-badge]: https://img.shields.io/badge/dependabot-enabled-brightgreen?logo=dependabot
[dependabot-url]: https://github.com/futuroptimist/token.place/network/updates
[codeql-badge]: https://github.com/futuroptimist/token.place/actions/workflows/codeql.yml/badge.svg?branch=main
[codeql-url]: https://github.com/futuroptimist/token.place/actions/workflows/codeql.yml
[secret-badge]: https://img.shields.io/badge/secret%20scanning-enabled-brightgreen
[secret-url]: https://docs.github.com/en/code-security/secret-scanning

Secure peer-to-peer generative AI platform

# Quickstart

Ensure you have Node.js 18+ installed.

```bash
git clone https://github.com/futuroptimist/token.place.git
cd token.place
pip install -r config/requirements_server.txt
pip install -r config/requirements_relay.txt
pip install -r requirements.txt
npm ci
playwright install --with-deps chromium
pip install pre-commit
pre-commit install
pre-commit run --all-files
```

The development requirements live in [requirements.txt](requirements.txt).

Run the relay and server in separate terminals:

```bash
python relay.py
python server.py
```

Or start both services with Docker Compose:

```bash
docker compose up --build
```

Open `http://localhost:5000` or run `python client.py`. For a minimal client use
`python client_simplified.py`; it clears the screen when running interactively using ANSI codes
with flushed output. Metrics are exposed at `/metrics`.

### Key environment variables

| Variable        | Default      | Description                                                        |
|-----------------|--------------|--------------------------------------------------------------------|
| API_RATE_LIMIT  | 60/hour      | Per-IP rate limit for API requests                                |
| API_DAILY_QUOTA | 1000/day     | Per-IP daily request quota                                        |
| USE_MOCK_LLM    | 0            | Use mock LLM instead of downloading a model (`1` to enable)        |
| TOKEN_PLACE_ENV | development  | Deployment environment (`development`, `testing`, `production`)    |

## CI pass criteria

All pull requests must:

- run `pre-commit run --all-files`
- pass `npm run lint`
- pass `npm run type-check`
- pass `npm run build`
- pass `npm run test:ci`
- pass `pytest -q tests/test_security.py`
- pass `bandit -r . -lll` with no medium or high findings
- keep Dependabot, CodeQL, and secret-scanning badges in this README

See [docs/TESTING.md](docs/TESTING.md) for the full testing guide.

# vision
There are tons of personal computers and homelabs out there with lots of compute that remain idle. This project aims to create a marketplace of people with spare compute and people with needs for compute. Note that this is not a financial marketplace -- this is intended to be a public good. If it takes off is anyone's guess, but I'll donate whatever compute I can in the meantime once this is up and running.

For a quick orientation to the repository layout and key docs, see [docs/ONBOARDING.md](docs/ONBOARDING.md).

## Contents

- End-to-end encryption powered by RSA and AES
- Cross-platform Python server and JavaScript client
- Comprehensive tests and CI via GitHub Actions
- [AGENTS.md](AGENTS.md) lists repo helpers for LLMs
- [llms.txt](llms.txt) provides machine-readable context
- [CLAUDE.md](CLAUDE.md) summarizes Claude integration tips

## roadmap

- [x] hello world: it literally just echoes your message param back to you
- [x] find an initial model to support (llama 2 7b chat gguf)
- [x] download model programmatically on device
- [x] load the model and successfully run it
- [x] do inference over HTTP
- [x] multi-step dialogue
- [x] relay.py, which passes plaintext requests from client to a server (hardcoded URL, run locally for now) and the response back to the client.
- [x] end-to-end encrypt communication between server and client with public key cryptography (server generates public/private key pair on init and gives public key to relay, which passes it on to the server [but does not reveal server's IP address])
  - [x] IP obfuscation
  - [x] end-to-end encryption (short responses, under 256 bytes of utf-8 encoded text) for client.py
  - [x] end-to-end encryption for longer responses for client.py
  - [x] integration test demonstrating the above
  - [x] Llama 2 -> 3 (7B -> 8B)
  - [x] end-to-end encryption on landing page (relay.py / GET)
  - [x] automated browser testing of landing page, preventing regressions of the chat ui
  - [x] delete old /inference endpoint and everything upstream and downstream that's now unused 💯
  - [x] simplified crypto utility (CryptoClient) for easy encrypted communication
- [ ] distribute relay.py across multiple machines
  - [x] personal gaming PC
  - [x] raspi k3s pod 💯
  - [ ] once k3s pod is stable, run relay.py only on the cluster
  - [ ] optional cloud fallback via Cloudflare
- [x] OpenAI-compatible API with end-to-end encryption
  - [x] Models listing endpoint
  - [x] Chat completions endpoint
  - [x] Text completions endpoint
  - [x] Compatibility with standard OpenAI client libraries
  - [x] Optional encryption for enhanced privacy
- [x] Comprehensive test suite
  - [x] Unit tests for core components
  - [x] API integration tests
  - [x] End-to-end tests with mock LLM
  - [x] Support for testing with real LLM models
  - [x] GitHub Actions CI for automated tests
  - [x] CI caching for faster dependency installs
- [x] API v1 with at least 1 model supported and available
- [x] landing page chat UI integrated with API v1
- [ ] use best available llama family model that can run on an RTX 4090
- [ ] [DSPACE](https://github.com/democratizedspace/dspace) (first 1st party integration) uses API v1 for dChat
- [ ] set up production k3s raspberry pi pod running relay.py
  - [ ] server.py stays on personal gaming PC
  - [ ] potential cloud fallback node via Cloudflare
- [ ] allow participation from other server.pys
  - [x] split relay/server python dependencies to reduce installation toil for relay-only nodes
- [ ] API v2 with at least 10 models supported and available
  - [ ] Streaming response support for faster UI feedback
  - [ ] Function/tool calling support via Machine Conversation Protocol (MCP)
  - [ ] Multi-modal support (text + images input)
  - [ ] Local image generation support (Stable Diffusion 3, Flux)
  - [ ] Vision model support (analyzing images)
  - [ ] Fine-tuned models and model adapter support
- [ ] Performance optimizations
  - [ ] Token streaming between client/server for faster responses
  - [ ] GPU memory optimizations for running multiple models
  - [ ] Batched inference for relay servers with multiple connected clients
- [ ] Advanced security features
  - [ ] Client authentication for relay servers
  - [x] Rate limiting and quota enforcement 💯
- [ ] Enhanced encryption options for model weights and inference data
  - [ ] Key rotation for relay and server certificates
  - [ ] Signed relay binaries for client verification
  - [ ] Optional content moderation hooks
  - [ ] External security review of protocol and code
- [ ] Community features
  - [ ] Server provider directory/registry
  - [ ] Model leaderboard based on community feedback
  - [ ] Contribution system for donating compute resources

## installation

### virtual environment

create a virtual environment:

```sh
$ python -m venv .venv
```

Depending on your environment, you may need to replace `python` in the above command with `python3`.

On Debian-based distributions, you may additionally need to install venv first, if it's not already installed:

```sh
apt install python3.12-venv
```

activate the virtual environment:

#### unix/linux/macos

```sh
source .venv/bin/activate
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

You may be missing some CMAKE dependencies

on Debian/Ubuntu you can run the following

```sh
sudo apt-get install build-essential cmake
```

on macOS, install the Xcode command line tools and CMake via Homebrew:

```sh
xcode-select --install  # if not already installed
brew install cmake
```

On other Linux distributions, use your package manager to install the equivalents of `build-essential` and `cmake`. For example on Fedora:

```sh
sudo dnf install gcc-c++ make cmake
```

then, run:

```
pip install -r config/requirements_server.txt  # server/API dependencies
pip install -r config/requirements_relay.txt   # relay-only dependencies
```

For JavaScript dependencies, run:

```
npm ci
```

#### troubleshooting `llama_cpp_python` builds

If `pip install` fails while building `llama_cpp_python` with an error such as
`CMAKE_CXX_COMPILER not set` or `Could not find compiler set in environment
variable CXX`, it usually means a C++ compiler is missing from your system.
Install `g++` (e.g. via `build-essential` on Debian/Ubuntu) and ensure it is on
your `PATH` or set `CXX=g++` before running `pip install`. The
[`hardware acceleration`](#hardware-acceleration) section below describes how to
reinstall `llama_cpp_python` with GPU support once your build environment is set
up correctly.

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

The next steps need to be executed in the same virtual environment you set up above. You'll see something like (.venv) on the bottom left in your terminal (may not be true on all platforms in all terminals).

This will replace the llama-cpp-python you installed via `pip install -r requirements.txt` and will instruct it to use [cuBLAS](https://docs.nvidia.com/cuda/cublas/index.html).

**if you're using Command Prompt**

```
set CMAKE_ARGS=-DGGML_CUDA=on
set FORCE_CMAKE=1
pip install llama-cpp-python --force-reinstall --upgrade --no-cache-dir --verbose
```

**if you're using Powershell**

```
$env:CMAKE_ARGS = "-DGGML_CUDA=on"
$env:FORCE_CMAKE=1
pip install llama-cpp-python --force-reinstall --upgrade --no-cache-dir --verbose
```

> **Note:** The compilation process can take 5-20 minutes depending on your system. The `--verbose` flag shows build progress, but there may still be periods with no visible output. This is normal - the compiler is working in the background. As long as your system shows CPU activity, the process is still running.

when you run `server.py` next, you'll see `BLAS = 1` in a collection of lines that looks like this:

```
AVX = 1 | AVX_VNNI = 0 | AVX2 = 1 | AVX512 = 0 | AVX512_VBMI = 0 | AVX512_VNNI = 0 | FMA = 1 | NEON = 0 | ARM_FMA = 0 | F16C = 1 | FP16_VA = 0 | WASM_SIMD = 0 | BLAS = 1 | SSE3 = 1 | SSSE3 = 0 | VSX = 0 |
```

This indicates that `server.py` can correctly access your GPU resources.

llama-cpp-python is initialized like this:

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
Install the [Homebrew](https://brew.sh) package manager if you haven't already,
then reinstall `llama-cpp-python` with Metal support:

```sh
brew install cmake
CMAKE_ARGS="-DLLAMA_METAL=on" FORCE_CMAKE=1 pip install llama-cpp-python --force-reinstall --upgrade
```

#### unix/linux
Most distributions can compile `llama-cpp-python` with OpenBLAS:

```sh
sudo apt-get install build-essential cmake libopenblas-dev
CMAKE_ARGS="-DLLAMA_OPENBLAS=on" FORCE_CMAKE=1 pip install llama-cpp-python --force-reinstall --upgrade
```

## Running the servers

### Dev-like environment

You'll need a way to switch between terminal tabs (e.g. tmux, VS Code terminal tabs).

Launch the relay, which runs on http://localhost:5000:

```sh
python relay.py
```

The relay listens on port 5000. It automatically connects to the default server
address baked into the project, so no environment variables are required.

In a separate terminal, launch the server, which binds to `0.0.0.0` on port 3000 (accessible via http://localhost:3000):

```sh
python server.py
```

For testing with mock LLM (faster startup):

```sh
python server.py --use_mock_llm
```

### Using the Application

`relay.py` acts as a proxy between the client (including, but not limited to, this repo's `client.py`) and `server.py`, obfuscating each other's public IP from each other, solving one of the big limitations of P2P networks (e.g. for .torrents). The relay.py provides end-to-end encryption for communication between server and client, ensuring that your messages are private even from the relay itself.

`server.py` runs on volunteers' machines and hosts the LLM model. It serves inference requests forwarded by the relay while keeping the server's network details private.

You can test things out using the simple command-line client, `client.py`:

```sh
python client.py
```

Type your message when prompted and press Enter. All of this is now happening on your local hardware, thanks to `llama-cpp-python`, a binding for llama.cpp.

To exit, press Ctrl+C/Cmd+C.

Alternatively, you can visit http://localhost:5000 in your browser to use the web interface.

### Raspberry Pi 5 deployment

For a complete walkthrough of the Raspberry Pi 5 setup—including hardware recommendations, Docker instructions, k3s cluster steps, and troubleshooting (including rpi-clone prompts)—see [docs/RPI_DEPLOYMENT_GUIDE.md](docs/RPI_DEPLOYMENT_GUIDE.md#bill-of-materials).

## Testing

The project includes a comprehensive test suite to ensure functionality and prevent regressions.

### Running Tests

Run all tests:

```sh
python -m pytest
```

To execute all available test suites (including JavaScript and end‑to‑end tests), use:

```bash
./run_all_tests.sh
```
or on Windows:

```powershell
./run_all_tests.ps1
```

Run specific test files:

```sh
python -m pytest tests/test_api.py
python -m pytest tests/test_e2e_network.py
```

Run tests with coverage report:

```sh
TEST_COVERAGE=1 ./run_all_tests.sh
# Coverage results are uploaded to Codecov on CI
```
If you don't see coverage comments on your pull requests, install the [Codecov GitHub App](https://github.com/marketplace/codecov) on your fork.
Every pull request automatically runs this test suite in GitHub Actions, so you can rely on the status checks for pass/fail information.

Tag **@claude** in any pull request or issue to invoke the automated Claude PR Assistant for implementation help.

### Test Categories

- **Unit Tests**: Test individual components in isolation
  - Core crypto functionality (RSA/AES encryption)
  - Client message handling
  - Server request processing

- **Integration Tests**: Test component interactions
  - Client-server communication
  - Relay server message passing
  - Multi-step conversation handling

- **End-to-End Tests**: Test complete workflows from client to server
  - Chat functionality through the relay
  - Browser-based UI testing with Playwright
  - API endpoints with encrypted communication

- **API Tests**: Test the OpenAI-compatible API endpoints
  - Models listing
  - Chat completions
  - Regular completions
  - Error handling

- **Real LLM Tests**: Test with actual LLM models
  - Model file verification with checksums
  - Single-turn inference
  - Multi-turn conversations
  - Error handling and recovery

- **Mock Testing**: Mock mode for faster development and testing
  - Models can be swapped with mock implementations
  - Test complex scenarios without real inference

### Real LLM Testing

To run tests with the actual LLM model:

```sh
python -m pytest tests/test_real_llm.py -v
```

This test will:
1. Automatically download the model file (~4GB) if it doesn't exist
2. Verify the model file with SHA-256 checksum
3. Cache the model locally to avoid re-downloading
4. Run inference with the real LLM model

For more comprehensive real LLM testing:

```sh
python -m pytest tests/test_real_llm_validation.py -v
```

This performs additional validation including:
- Multi-turn conversation testing
- Complex reasoning validation
- Error handling and recovery tests

Note: The first run may take several minutes to download the model file. Subsequent runs will use the cached model file if it passes checksum verification.

If you want to disable the test to avoid downloading, edit `tests/test_real_llm.py` and set:

```python
RUN_REAL_LLM_TEST = False
```

## Project Documentation for LLMs

See [AGENTS.md](AGENTS.md) for a list of repo helpers. For LLMs, `llms.txt` contains the same helper summary in plain text.
This project includes an `llms.txt` file in the root directory that provides structured information for Large Language Models (LLMs) about the project. Following the [llms.txt specification](https://llmstxt.org/), this file helps LLMs understand the codebase structure, available components, and key documentation links.

The `llms.txt` file includes:
- Project overview and description
- Links to setup and installation instructions
- Core component descriptions
- Testing documentation links
- Development guidelines

When working with LLMs to understand or develop this codebase, reference the `llms.txt` file for comprehensive context.

## Naming Conventions

Always stylize the project name as lowercase `token.place` (not Title case "Token.place") to emphasize that it is a URL. For complete stylization guidelines, see [docs/STYLE_GUIDE.md](docs/STYLE_GUIDE.md).

## API (OpenAI-compatible)

The token.place API is designed to be compatible with the OpenAI API format, making it easy to integrate with existing applications that use OpenAI's services.

### API Endpoints

All routes are served under `/api/v1` (preferred) and are also available at
`/v1` for compatibility with standard OpenAI clients. Set the base URL to
`https://token.place/api/v1`.

#### List Models
```
GET /api/v1/models
# or
GET /v1/models
```
Returns a list of available models.

#### Get Model
```
GET /api/v1/models/{model_id}
# or
GET /v1/models/{model_id}
```
Returns information about a specific model.

#### Chat Completions
```
POST /api/v1/chat/completions
# or
POST /v1/chat/completions
```
Creates a completion for chat messages.

Request body:
```json
{
  "model": "llama-3-8b-instruct",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Hello!"}
  ]
}
```

#### Text Completions
```
POST /api/v1/completions
# or
POST /v1/completions
```
Traditional completions API returning text completion data.

Request body:
```json
{
  "model": "llama-3-8b-instruct",
  "prompt": "Write a poem about AI",
  "max_tokens": 256
}
```

### End-to-End Encryption

For enhanced privacy, you can use end-to-end encryption with the API:

1. Get the server's public key:
```
GET /api/v1/public-key
# or
GET /v1/public-key
```

2. Encrypt your request with the server's public key

3. Send your encrypted request:
```json
{
  "model": "llama-3-8b-instruct",
  "encrypted": true,
  "client_public_key": "YOUR_PUBLIC_KEY_HERE",
  "messages": {
    "ciphertext": "ENCRYPTED_DATA_HERE",
    "cipherkey": "ENCRYPTED_AES_KEY_HERE",
    "iv": "INITIALIZATION_VECTOR_HERE"
  }
}
```

The server will encrypt its response with your public key, ensuring end-to-end encryption.

## System Architecture

The project follows a distributed architecture with end-to-end encryption
(see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)):

```

┌───────────────┐    Encrypted     ┌─────────────────┐    Encrypted     ┌─────────────────┐
│               │    Requests      │                 │    Requests      │                 │
│  Web Browser  │ ───────────────► │  Relay Server   │ ───────────────► │  Server (LLM)   │ ◄─── LLM Model File (~4GB)
│  or CLI       │ ◄─────────────── │  (lightweight)  │ ◄─────────────── │                 │      (downloaded locally)
│               │    Encrypted     │                 │    Encrypted     │                 │
└───────────────┘    Responses     └─────────────────┘    Responses     └─────────────────┘
                                           │
                                           │ can scale to
                                           ▼
                                   ┌─────────────────┐
                                   │  Multiple       │
                                   │  Relay Servers  │
                                   │  & LLM Servers  │
                                   └─────────────────┘
```

The architecture consists of:

1. **Clients** (Web browsers, CLI tools) - Send encrypted requests to relay servers
2. **Relay Servers** - Forward encrypted requests/responses, hiding IP addresses of clients and servers
3. **LLM Servers** - Run inference on local hardware (GPU/CPU), download and use LLM models, and return encrypted results
4. **LLM Models** - Downloaded and cached locally on each LLM server

All communication uses hybrid RSA/AES encryption to ensure end-to-end security, preventing relay servers from accessing the content of messages. The relay server only knows which client and server are communicating, but not what they're saying.

The API is compatible with the OpenAI format for easy integration with existing tools and libraries.

## Cross-Platform Support

token.place now has full cross-platform support for Windows, macOS, and Linux. This includes:

- Platform-specific path handling for user data, configs, and logs
- Native launcher scripts for each platform
- Docker containerization for consistent deployment
- Cross-platform testing framework
- Automated desktop installers for Windows (.exe) and macOS (.dmg) built via GitHub Actions

For detailed information on cross-platform features and containerization, see [docs/CROSS_PLATFORM.md](docs/CROSS_PLATFORM.md).

### Quick Start on Different Platforms

**Windows:**
```batch
docker compose up -d
```

**macOS/Linux:**
```bash
make docker-build
docker compose up -d
```

**Docker:**
```bash
docker compose up -d  # starts the relay service
```

## Features

- [x] OpenAI-compatible API with end-to-end encryption
- [x] Local and remote running modes
- [x] Compatibility with standard OpenAI client libraries
- [x] No token leakage to proxy servers
- [x] Cross-platform support (Windows, macOS, Linux)

## Quick Start

1. Clone the repository
2. Run `docker compose up` to build and start the relay container
3. Open `http://localhost:5000` in your browser to begin chatting

## Development

### Testing

We have comprehensive testing to ensure quality. See [docs/TESTING.md](docs/TESTING.md) for a complete overview:

```bash
# Run all Python tests
python -m pytest

# Run JS unit tests
npm run test:js

# Run crypto compatibility tests
python tests/test_crypto_compatibility_simple.py
```

To run every test category in one command (including Playwright and JS tests) execute `TEST_COVERAGE=1 ./run_all_tests.sh` (or `./run_all_tests.ps1` on Windows).

### Windows PowerShell Tips

In PowerShell, use semicolons (`;`) for command chaining instead of ampersands (`&&`):

```powershell
# Correct:
cd folder_path; python script.py

# Incorrect (will cause errors):
cd folder_path && python script.py
```

Always use explicit IPv4 addresses for reliable network testing:

```powershell
# Preferred:
curl http://127.0.0.1:5000/test

# May cause issues:
curl http://localhost:5000/test
```

## Contributing

We welcome contributions! See our [Contributing Guide](CONTRIBUTING.md) for details.
Before submitting commits, install the pre-commit hooks and run them locally:
```bash
pip install pre-commit
pre-commit install
pre-commit run --all-files
```

## Security

Security is our top priority. Please report any vulnerabilities responsibly.
See [Security and Privacy Audit](docs/SECURITY_PRIVACY_AUDIT.md) for details.
token.place intentionally avoids storing user prompts or LLM responses in logs to protect user privacy.

## License

This project is licensed under the MIT License as detailed in [LICENSE](LICENSE).
