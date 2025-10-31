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

Ensure you have Node.js 18+ installed (`nvm use` respects the included .nvmrc).

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

### Developer workflow quick reference

- **Personalise local config** — `./scripts/setup.sh YOURNAME YOURFORK` seeds `.env.local` with
  fork-specific defaults and updates repo metadata for your clone.
- **Run the services locally** — `python relay.py` and `python server.py` keep iteration tight when
  changing Python code.
- **Run via containers** — `docker compose up --build` matches the relay + server layout exercised
  by CI and production deployments.
- **Format & lint** — `pre-commit run --all-files` (or `make lint`) mirrors the CI bot's
  formatting, linting, and quick tests.
- **Full test sweep** — `./run_all_tests.sh` (or `make test`) calls pytest, Playwright, npm checks,
  and Bandit just like CI.
- **Deploy Kubernetes manifests** — `make k8s-deploy` applies everything under `k8s/` to the active
  cluster context.

Make targets surface the same workflows in shorthand:

- `make lint` → run the full pre-commit suite (formatters, linters, tests)
- `make test` → execute `./run_all_tests.sh`
- `make docker-build` → build the relay Docker image used by remote nodes
- `make k8s-deploy` → apply the current Kubernetes manifests

### Key environment variables

Environment variables can be stored in a `.env` file and overridden in a `.env.local` file, which is ignored by git.

| Variable        | Default      | Description                                                        |
|-----------------|--------------|--------------------------------------------------------------------|
| API_RATE_LIMIT  | 60/hour      | Per-IP rate limit for API requests                                |
| API_STREAM_RATE_LIMIT | 30/minute   | Per-IP rate limit applied only to streaming chat completions          |
| SERVICE_NAME    | token.place  | Service identifier returned by health endpoints (whitespace-only overrides
|                 |              | fall back to `token.place`)                                             |
| API_DAILY_QUOTA | 1000/day     | Per-IP daily request quota                                        |
| USE_MOCK_LLM    | 0            | Use mock LLM instead of downloading a model (`1` to enable)        |
| TOKEN_PLACE_ENV | development  | Deployment environment (`development`, `testing`, `production`)    |
| CONTENT_MODERATION_MODE | disabled     | Set to `block` to enable request filtering before inference           |
| CONTENT_MODERATION_BLOCKLIST | (defaults)  | Comma-separated phrases added to the default safety blocklist         |
| CONTENT_MODERATION_INCLUDE_DEFAULTS | 1            | Set to `0` to skip the built-in phrases when filtering requests        |
| PROD_API_HOST   | 127.0.0.1    | IP address for production API host                                |
| API_FALLBACK_URLS | (empty)   | Comma-separated Cloudflare or other relay fallbacks tried in order |
| TOKEN_PLACE_RELAY_CLOUDFLARE_URLS | (empty) | Optional Cloudflare relay URLs appended to the server's relay pool |

Set `API_FALLBACK_URLS=https://relay.cloudflare.workers.dev/api/v1` to let the bundled clients
retry through a Cloudflare-hosted relay whenever the primary endpoint is unreachable.

Set `TOKEN_PLACE_RELAY_CLOUDFLARE_URLS` (or `TOKEN_PLACE_RELAY_CLOUDFLARE_URL` for a single
endpoint) so `server.py` can fail over to a Cloudflare tunnel when the local relays are down.

#### Configuration precedence

token.place automatically loads environment files before initialising the Python configuration
stack. The precedence is:

1. `.env`
2. `.env.<TOKEN_PLACE_ENV>` (for example `.env.production`)
3. `.env.local`
4. File referenced by `TOKEN_PLACE_ENV_FILE`

Values already present in `os.environ` win over file-based values, so deployment platforms and
local shells retain ultimate control.

The development requirements live in [requirements.txt](requirements.txt).

### Content moderation hooks

Set `CONTENT_MODERATION_MODE=block` to enable pre-inference moderation for both
`/api/v1/chat/completions` and `/api/v1/completions`.
Requests containing phrases from the built-in safety blocklist (or any terms supplied via
`CONTENT_MODERATION_BLOCKLIST`) are rejected with a standardized `content_policy_violation` error before they reach the model.
Set `CONTENT_MODERATION_INCLUDE_DEFAULTS=0` if you only want to enforce your custom blocklist.

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
For a directory-by-directory atlas, visit [docs/REPO_MAP.md](docs/REPO_MAP.md).

## Contents

- End-to-end encryption powered by RSA and AES
- Cross-platform Python server and JavaScript client
- Inline vision analysis for base64 image attachments via the API v2 chat endpoint
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
- [x] distribute relay.py across multiple machines
  - [x] Multi-relay failover via `relay.additional_servers` configuration (server auto-rotates backups)
  - [x] personal gaming PC
  - [x] raspi k3s pod 💯
  - [x] once k3s pod is stable, run relay.py only on the cluster
    - `TOKEN_PLACE_RELAY_CLUSTER_ONLY=1` (or `relay.cluster_only` in `config.json`) disables the
      localhost fallback and requires at least one upstream from `relay.additional_servers` or
      the normalised `relay.server_pool`.
  - [x] optional cloud fallback via Cloudflare
  - [x] Round-robin sink polling to balance traffic across configured relays
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
- [x] use best available llama family model that can run on an RTX 4090
- [x] [DSPACE](https://github.com/democratizedspace/dspace) (first 1st party integration) uses API v1 for dChat
  - [x] Added compatibility aliases (including `gpt-5-chat-latest`) so dChat can target
        token.place without code changes
  - [x] Integration test verifies the OpenAI client flow through `/api/v1/chat/completions`
- [x] set up production k3s raspberry pi pod running relay.py
  - [x] server.py stays on personal gaming PC
  - [x] potential cloud fallback node via Cloudflare
- [x] allow participation from other server.pys
  - [x] Relay enforces invitation tokens so community-run `server.py` nodes can authenticate `/sink` and `/source`
  - [x] split relay/server python dependencies to reduce installation toil for relay-only nodes
- [x] API v2 with at least 10 models supported and available
  - [x] Catalogue exposes Llama 3, Mixtral, Phi-3, Mistral Nemo, and Qwen2.5 variants
  - [x] Dedicated Flask blueprint in `api/v2/routes.py`
  - [x] Streaming response support for faster UI feedback (`api/v2/routes.py`)
  - [x] Function/tool calling support via Machine Conversation Protocol (MCP) (`api/v2/routes.py`)
  - [x] Multi-modal support (text + images input)
    - [x] Structured chat content is flattened for llama.cpp compatibility while inline images
      continue to receive automatic analysis summaries.
  - [x] Local image generation support (deterministic placeholder renderer via Pillow)
    - [x] `/api/v1/images/generations` endpoint for offline-friendly PNG output
  - [x] Vision model support (inline analysis for base64-encoded images)
  - [x] Fine-tuned models and model adapter support
- [x] Performance optimizations
  - [x] Token streaming between client/server for faster responses
  - [x] GPU memory guardrails for multi-model hosting (auto CPU fallback when VRAM is tight)
  - [x] Cached decoded client public keys to avoid repeated base64 work during encryption
  - [x] Batched inference for relay servers with multiple connected clients
  - [x] Cached RSA private key deserialization to eliminate redundant parsing during decrypt
- [x] Advanced security features
  - [x] Zero-trust relay challenge/response hardening
  - [x] Rate limiting and quota enforcement 💯
- [x] Enhanced encryption options for model weights and inference data
  - [x] Optional AES-GCM mode with associated data for protecting weights and inference payloads
  - [x] Key rotation for relay and server certificates
- [x] Signed relay binaries for client verification
- [x] Optional content moderation hooks
- [x] External security review of protocol and code
  - [x] Automated Bandit security scanning integrated into the pytest suite to block medium/high severity regressions
  - [x] Enforced a minimum 2048-bit RSA key size for server key generation
- [x] Community features
  - [x] Server provider directory/registry
  - [x] Model leaderboard based on community feedback
  - [x] Contribution system for donating compute resources
  - [x] Contribution summary endpoint for maintainers

## streaming usage

token.place supports server-sent events (sse) for plaintext requests starting with the api v2
chat completions endpoints. when the `stream` flag is set to `true`, the
`/api/v2/chat/completions` and `/v2/chat/completions` routes emit incremental chunks that match
openai's event format so existing clients can subscribe without code changes.

> ✅ encrypted chat payloads now stream encrypted chunks when you call the
> `/api/v2/chat/completions` endpoint with `encrypted=true` alongside
> `stream=true`.
> ❌ api v1 chat endpoints remain json-only; requests that include `stream=true`
> return an error. use `/api/v2/chat/completions` (or `/v2/chat/completions`)
> for server-sent events.

Encrypted streaming events use the same OpenAI-style `chat.completion.chunk`
payloads as plaintext responses, but each SSE data line contains an envelope
shaped like:

```json
{"event": "delta", "encrypted": true, "data": {"ciphertext": "...", "cipherkey": "...", "iv": "..."}}
```

Clients can reuse `CryptoClient.decrypt_message` (or equivalent) to decrypt the
payload before reading the inner `choices[0].delta` entries, preserving API
compatibility with existing OpenAI SDKs.

### example curl request

```bash
curl \
  -N \
  -H "content-type: application/json" \
  -H "authorization: bearer $TOKEN_PLACE_API_KEY" \
  -d '{
        "model": "gpt-5-chat-latest",
        "stream": true,
        "messages": [
          {"role": "user", "content": "summarize the roadmap"}
        ]
      }' \
  http://localhost:5050/api/v2/chat/completions
```

### consuming the stream in python

```python
import requests

response = requests.post(
    "http://localhost:5050/v2/chat/completions",
    headers={"Authorization": "Bearer $TOKEN_PLACE_API_KEY"},
    json={
        "model": "gpt-5-chat-latest",
        "stream": True,
        "messages": [{"role": "user", "content": "give me a streaming demo"}],
    },
    stream=True,
    timeout=30,
)

for line in response.iter_lines():
    if not line:
        continue
    if line.startswith(b"data: "):
        payload = line.removeprefix(b"data: ")
        if payload == b"[DONE]":
            break
        print(payload.decode("utf-8"))
```

each chunk includes the role delta, message content, optional tool calls, and a
final `[DONE]` marker. clients can accumulate the `delta.content` strings to
display streaming completions in their ui.

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

#### Configuring relay upstream server nodes

token.place is bootstrapping its first community LLM cluster with volunteer-run
`server.py` nodes. The pioneer machine still lives on futuroptimist's gaming PC,
but the relay now accepts a list of upstream hosts so new contributors can join
without code changes.

When the Raspberry Pi relay cluster runs in production, set
`TOKEN_PLACE_RELAY_UPSTREAMS` before launching `relay.py` to provide a
comma-separated (or JSON array) list of volunteer nodes:

```sh
export TOKEN_PLACE_ENV=production
export TOKEN_PLACE_RELAY_UPSTREAMS="https://gaming-pc.local:8000,https://your-node.example.com:8443"
python relay.py
```

`Config` normalises these URLs, keeps the historical gaming PC entry as the
default, and surfaces any secondary nodes from `/api/v1/relay/server-nodes` so
relay operators can verify who is online. The legacy `PERSONAL_GAMING_PC_URL`
variable still works; it is treated as shorthand for a single-entry upstream
list.

#### Zero-trust relay verification

`token.place` now ships with an opt-in challenge/response layer for compute
nodes. Set `TOKEN_PLACE_RELAY_SERVER_TOKEN` before launching the relay and
every `/sink` or `/source` call must include an `X-Relay-Server-Token`
header that matches the configured value. Requests missing the header are
rejected with an HTTP 401 so unknown machines can no longer impersonate
trusted servers. Sensitive tokens are stripped when saving config files, so
store them in environment variables instead of `config.json`.

The bundled `RelayClient` automatically reads the same configuration and sends
the header, so volunteer operators only need to export the token once:

```sh
export TOKEN_PLACE_RELAY_SERVER_TOKEN="rotate-me-often"
python relay.py --host 0.0.0.0
# on the compute node
python server.py --relay_url http://relay.example.com --relay_port 5010
```

Clients remain zero-auth: they never see or transmit the relay token. This
keeps the network open for end users while letting operators quarantine
suspicious server nodes using cryptography instead of static passwords.

Once that upstream list is stable, export `TOKEN_PLACE_RELAY_CLUSTER_ONLY=1`
before launching `server.py`. The background `RelayClient` will refuse to talk
to `localhost` and instead require at least one upstream derived from
`TOKEN_PLACE_RELAY_UPSTREAMS`, `relay.additional_servers`, or the
`relay.server_pool` values surfaced by `config`. This keeps production traffic
pinned to the k3s pod rather than silently falling back to the gaming PC.

### Using the Application

`relay.py` acts as a proxy between the client (including, but not limited to, this repo's `client.py`) and `server.py`, obfuscating each other's public IP from each other, solving one of the big limitations of P2P networks (e.g. for .torrents). The relay.py provides end-to-end encryption for communication between server and client, ensuring that your messages are private even from the relay itself.

`server.py` runs on volunteers' machines and hosts the LLM model. It serves inference requests forwarded by the relay while keeping the server's network details private.

You can test things out using the simple command-line client, `client.py`:

```sh
python client.py
```

#### Relay batching

Operators running `server.py` can include a `max_batch_size` integer in their `/sink` polls to
retrieve multiple pending jobs at once. The relay removes up to that many queued faucet requests,
returns the first item via the legacy `client_public_key`/`chat_history` fields, and exposes the full
batch under a `batch` array for upgraded workers. Leaving `max_batch_size` unset preserves the
single-request behavior.

Type your message when prompted and press Enter. All of this is now happening on your local hardware, thanks to `llama-cpp-python`, a binding for llama.cpp.

To exit, press Ctrl+C/Cmd+C.

Alternatively, you can visit http://localhost:5000 in your browser to use the web interface.

### Raspberry Pi 5 deployment

For a complete walkthrough of the Raspberry Pi 5 setup—including hardware recommendations, Docker instructions, k3s cluster steps, and troubleshooting (including rpi-clone prompts)—see [docs/RPI_DEPLOYMENT_GUIDE.md](docs/RPI_DEPLOYMENT_GUIDE.md#bill-of-materials).

If you're booting via the [`sugarkube`](https://github.com/futuroptimist/sugarkube) Pi image, copy the Helm bundle from [`k8s/sugarkube/`](k8s/sugarkube/) into `/etc/sugarkube/helm-bundles.d/` so the relay deploys automatically once k3s is ready.

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

API v2 extends the surface with adapter-aware metadata. Fine-tuned variants such as
`llama-3-8b-instruct:alignment` inherit the base weights and automatically prepend their
alignment charter as a system prompt. OpenAI SDKs can opt into domain-specific behaviour by
selecting the derived model ID. The curated v2 catalogue, including deployment notes for
RTX 4090-class hardware, is documented in [docs/api_v2_model_catalog.md](docs/api_v2_model_catalog.md).

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

> **API v1 catalogue**: token.place intentionally restricts `/api/v1/models` to the
> `llama-3-8b-instruct` base model (backed by the Meta Llama 3.1 8B Q4_K_M weights) and its safety-tuned
> `llama-3-8b-instruct:alignment` adapter. The broader RTX 4090-ready line-up
> lives behind `/api/v2/models`.

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

#### Health Check
```
GET /api/v1/health
# or
GET /v1/health
```
Returns the service's readiness metadata. The JSON payload includes `status`, `version`,
`service`, and a Unix `timestamp`. Override the reported service identifier by setting the
`SERVICE_NAME` environment variable; blank or whitespace-only overrides are ignored so the
default `token.place` label is preserved.

#### Image Generations
```
POST /api/v1/images/generations
# or
POST /v1/images/generations
```
Creates a deterministic PNG using the local Pillow-based renderer. The endpoint is
compatible with OpenAI SDK helpers that expect a `b64_json` payload.

Request body:
```json
{
  "prompt": "Neon skyline over a calm ocean",
  "size": "256x256",
  "seed": 42
}
```

Response body:
```json
{
  "created": 1731976800,
  "size": "256x256",
  "data": [
    {
      "b64_json": "iVBORw0KGgoAAAANSUhEUgAA...",
      "revised_prompt": "Neon skyline over a calm ocean"
    }
  ],
  "seed": 42
}
```

If `size` is omitted the renderer defaults to `512x512`. Provide an integer `seed` to
generate reproducible art assets for offline demos or unit tests.

#### Community Provider Directory
```
GET /api/v1/community/providers
```
Lists community-operated relay nodes and server operators that have opted into the public registry.
Each entry includes the provider identifier, advertised region, contact details, current status, and the exposed endpoint URLs so clients can preselect a compatible provider.
A representative latency measurement may also be included to help clients pick a nearby relay.

Example response snippet:

```json
{
  "object": "list",
  "data": [
    {
      "id": "local-dev",
      "name": "Local Development Node",
      "region": "local",
      "status": "active",
      "endpoints": [
        {"type": "relay", "url": "http://localhost:5010"},
        {"type": "server", "url": "http://localhost:3000"}
      ]
    }
  ],
  "metadata": {
    "updated_at": "2025-02-15T00:00:00Z"
  }
}
```

#### Community Model Leaderboard
```
GET /api/v1/community/leaderboard
```
Aggregates community ratings for deployed models, returning the highest-rated experiences first.
Each entry reports the average rating, number of submitted votes, and the most recent feedback timestamp so client applications can highlight trending models.

Example response snippet:

```json
{
  "entries": [
    {
      "model_id": "anthropic/claude-3.5-sonnet",
      "average_rating": 4.8,
      "ratings_count": 27,
      "last_feedback_at": "2024-12-19T09:10:00Z"
    }
  ],
  "updated": "2024-12-19T09:10:00Z"
}
```

#### Community Contribution Queue
```
POST /api/v1/community/contributions
```
Allows community operators to offer spare compute resources for the shared relay network.
Submissions are validated server-side, assigned a UUID, and appended to a JSONL queue so maintainers can review and onboard new providers.

Request body:

```json
{
  "operator_name": "Compute Collective",
  "region": "us-west",
  "availability": "weekends",
  "capabilities": ["openai-compatible", "gpu"],
  "contact": {"email": "ops@example.org"},
  "hardware": "2x RTX 4090",
  "notes": "Can scale to 4 nodes with notice"
}
```

Response body:

```json
{
  "status": "queued",
  "submission_id": "82b900a7-1c05-4e2a-8ce0-3b18a835adcb"
}
```

For deployments that need to relocate the queue file, set `TOKEN_PLACE_CONTRIBUTION_QUEUE` to an absolute path. The server will create the file if it does not exist and append one JSON document per line.

#### Authorising community-operated servers

Once an operator is ready to host `server.py`, generate an invitation token and expose it to the relay by
setting `TOKEN_PLACE_RELAY_SERVER_TOKENS` (comma or newline delimited) before launching `relay.py`.
Each joined node must supply the matching token via the `TOKEN_PLACE_RELAY_SERVER_TOKEN` environment
variable, which the relay client automatically forwards to the `/sink` and `/source` endpoints as the
`X-Relay-Server-Token` header. Requests without a valid token are rejected, preventing uninvited nodes
from queueing or retrieving encrypted workloads while still keeping the workflow simple for approved
operators.

#### Community Contribution Summary
```
GET /api/v1/community/contributions/summary
```
Summarises queued community contributions so maintainers can understand incoming capacity at a glance.
Returns the total number of submissions, a sorted list of participating regions, the occurrence count for each advertised capability, and the timestamp of the most recent submission.

Example response:

```json
{
  "object": "community.contribution_summary",
  "total_submissions": 2,
  "regions": ["eu-central", "us-west"],
  "capability_counts": {"gpu": 2, "openai-compatible": 1},
  "last_submission_at": "2025-01-15T09:30:00Z"
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

If you need to invalidate the existing key pair (for example after suspected compromise),
rotate the credentials:
```
POST /api/v1/public-key/rotate
# or
POST /v1/public-key/rotate

# Include your operator token via either header:
# Authorization: Bearer <token>
# X-Token-Place-Operator: <token>

Configure the server-side secret by setting `TOKEN_PLACE_OPERATOR_TOKEN` (or
`TOKEN_PLACE_KEY_ROTATION_TOKEN`) in the environment before starting the API.
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

`client_public_key` may be provided as a PEM-formatted string or a base64-encoded key.

The server will encrypt its response with your public key, ensuring end-to-end encryption.

> **New:** When encrypting high-value assets such as model weights or inference payloads, call
> `encrypt(..., cipher_mode="GCM", associated_data=...)` to switch to AES-GCM.
> The response payload includes an additional `tag` field alongside `ciphertext` and `iv`, providing
> authenticated encryption without breaking compatibility with existing AES-CBC clients.

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
These hooks run linting, tests, and spelling checks via codespell.

## Security

Security is our top priority. Please report any vulnerabilities responsibly.
See [Security and Privacy Audit](docs/SECURITY_PRIVACY_AUDIT.md) for details.
token.place intentionally avoids storing user prompts or LLM responses in logs to protect user privacy.

Relay distributions now ship with Ed25519 signatures so operators can confirm binaries before running
them. Verify a download with the bundled helper:

```bash
python -m utils.signing.relay_signature relay.py config/signing/relay.py.sig
```

Use `--public-key` to supply a custom key if you host your own release channel. The command exits with
status code `0` on success and `1` if verification fails.

## License

This project is licensed under the MIT License as detailed in [LICENSE](LICENSE).
