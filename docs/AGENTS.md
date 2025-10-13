# token.place

> token.place is a secure proxy service for AI models that implements end-to-end encryption between clients and AI services. It provides transparent API compatibility with original services while ensuring that message content is encrypted on the client before being sent to the server, preventing the service from accessing the plaintext content of user prompts or AI responses.

token.place uses a hybrid encryption approach combining RSA and AES for secure communication. The Python backend serves as a proxy between clients and AI model providers, while the JavaScript client library handles encryption and decryption in the browser.

Key components:
- Python backend with Flask/FastAPI APIs
- JavaScript client library for browser-based encryption/decryption
- Hybrid RSA/AES encryption system ensuring secure message exchange
- Cross-language compatibility between Python and JavaScript implementations

Important: Always stylize the project name as lowercase `token.place` (not Title case "Token.place") to emphasize that it's a URL.

## Setup and Installation

- [README.md](../README.md): Project overview and getting started instructions
- [ONBOARDING.md](ONBOARDING.md): Quick orientation to the repository structure
- [requirements.txt](../requirements.txt): Python dependencies for the project
- [package.json](../package.json): Node.js dependencies for JavaScript components
- `docker compose up` starts the relay container for quick local testing

## Core Components

- [encrypt.py](../encrypt.py): The core encryption/decryption implementation in Python
- [static/chat.js](../static/chat.js): JavaScript client library for encryption/decryption in browsers
- [utils/crypto/crypto_manager.py](../utils/crypto/crypto_manager.py): Python class managing key generation and encryption/decryption operations
- [server.py](../server.py): Main server implementation for the proxy service
- [relay.py](../relay.py): Middleware server that forwards encrypted messages between clients and servers

## Documentation

- [CONTRIBUTING.md](../CONTRIBUTING.md): Contribution guidelines for developers
- [STYLE_GUIDE.md](STYLE_GUIDE.md): Style and branding guidelines, including proper name stylization
- [TESTING.md](TESTING.md): Comprehensive guide to the testing approach
- [TESTING_IMPROVEMENTS.md](TESTING_IMPROVEMENTS.md): Ideas for further testing improvements
- [ARCHITECTURE.md](ARCHITECTURE.md): Detailed architectural overview of the system
- [LLM_ASSISTANT_GUIDE.md](LLM_ASSISTANT_GUIDE.md): Guide for AI assistants working with this codebase
- [RPI_DEPLOYMENT_GUIDE.md](RPI_DEPLOYMENT_GUIDE.md#bill-of-materials): Hardware list, setup instructions, and troubleshooting tips for Raspberry Pi deployments (including rpi-clone prompt walkthrough)
- [../llms.txt](../llms.txt): Machine-readable project summary for LLM assistants
- [baseline.md](prompts/codex/baseline.md): Baseline prompt for routine contributions
- [automation.md](prompts/codex/automation.md): Prompt for upkeep and chore work
- [ci-fix.md](prompts/codex/ci-fix.md): Prompt for fixing CI failures
- [security.md](prompts/codex/security.md): Prompt for security reviews
- [docs.md](prompts/codex/docs.md): Prompt for doc updates
- [polish.md](prompts/codex/polish.md): Prompt for structural polish after saturation

## User Journeys

The project tests are organized around key user journeys that represent how users interact with the system:

1. **End-to-End Encrypted Conversation**: A user sends an encrypted message through the relay to an LLM server and receives an encrypted response
2. **Browser-Based Chat Interface**: A user accesses the web interface, sends messages through the browser, and sees responses
3. **OpenAI-Compatible API Usage**: A developer uses the API with standard OpenAI client libraries
4. **Cross-Platform Functionality**: Users run token.place on different operating systems
5. **Security and Failure Recovery**: System maintains security and recovers from errors during use
6. **Real LLM Integration**: A user interacts with a real LLM model through the system
7. **Integration with External Applications**: token.place is used as a drop-in replacement for OpenAI in external applications

Each journey is mapped to specific test files that verify the functionality works as expected. See [TESTING.md](TESTING.md) for details.

## API Structure

The API is designed to be compatible with OpenAI's API format:

- `/api/v1/models`: List available models
- `/api/v1/models/{model_id}`: Get information about a specific model
- `/api/v1/chat/completions`: Create chat completions
- `/api/v1/completions`: Create completions (legacy)
- `/api/v1/public-key`: Retrieve server's public key for encryption

All API endpoints support both encrypted and unencrypted modes for maximum flexibility.

## Testing

token.place uses a comprehensive testing approach with multiple test types:

### Test Types

- **Unit Tests**: Verify individual components in isolation
- **Integration Tests**: Verify that components work together correctly
- **End-to-End (E2E) Tests**: Validate complete user workflows
- **Real LLM Tests**: Tests with actual LLM models
- **Performance Benchmarks**: Measure system performance
- **Failure Recovery Tests**: Verify system resilience
- **Security Tests**: Validate encryption and security properties

### Key Test Files

- [tests/unit/](../tests/unit/): Unit tests for individual components
- [tests/test_e2e_conversation_flow.py](../tests/test_e2e_conversation_flow.py): End-to-end tests for conversation flows
- [tests/test_crypto_compatibility_local.py](../tests/test_crypto_compatibility_local.py): Tests for crypto compatibility between Python and JavaScript
- [tests/test_performance_benchmarks.py](../tests/test_performance_benchmarks.py): Performance benchmark tests
- [tests/test_failure_recovery.py](../tests/test_failure_recovery.py): Tests for system resilience and error recovery
- [tests/test_security.py](../tests/test_security.py): Tests for security properties
- [tests/test_real_llm.py](../tests/test_real_llm.py): Tests with actual LLM models

### Test Markers

token.place uses pytest markers to categorize tests:

- `unit`: Unit tests
- `integration`: Integration tests
- `api`: API tests
- `crypto`: Encryption-related tests
- `js`: JavaScript tests
- `browser`: Tests requiring a browser
- `slow`: Slow-running tests
- `benchmark`: Performance benchmark tests
- `failure`: Failure recovery tests
- `e2e`: End-to-end tests
- `parametrize`: Parameterized tests

Run tests with a specific marker using:
```bash
python -m pytest -m marker_name
```

For convenience, you can execute all available test suites with `./run_all_tests.sh` (or `./run_all_tests.ps1` on Windows).

## Configuration

- [config.py](../config.py): Configuration management for different environments
- [server/server_app.py](../server/server_app.py): Logging infrastructure for the application
- [pytest.ini](../pytest.ini): Configuration for Python test suite
- **Privacy**: Do not log plaintext or ciphertext of user messages. Remove debugging statements that could leak sensitive data.

## Development Tips

- Run `./run_all_tests.sh` to verify changes before committing. Set `TEST_COVERAGE=1` to collect coverage data when running this script. Coverage is uploaded to Codecov automatically via the GitHub Actions workflow.
- Install `pre-commit` and run `pre-commit run --all-files` before pushing changes.
- Keep the roadmap in [README.md](../README.md) updated as features progress.
- Use `npm ci` for faster, reproducible Node.js installs (mirrors the CI pipeline's behavior).
- Install Python dependencies with `pip install -r config/requirements_server.txt` and
  `pip install -r config/requirements_relay.txt` before running tests.
  If Playwright tests complain about missing browsers or system libraries,
  run `playwright install chromium` and `playwright install-deps`.
- For development and unit testing, also run `pip install -r requirements.txt` to install extra tooling like pytest-playwright, then run `playwright install`.
- Every pull request triggers the GitHub Actions workflow in `.github/workflows/ci.yml` which runs `./run_all_tests.sh` with coverage enabled and uploads results to Codecov.
- Tag **@claude** in a pull request or issue to invoke the Claude PR Assistant defined in `.github/workflows/claude.yml`.
- Ensure the [Codecov GitHub App](https://github.com/marketplace/codecov) is installed on your fork so coverage badges and PR comments work reliably.
- Real-world integration tests live in the [DSPACE project](https://github.com/democratizedspace/dspace); token.place acts as its OpenAI-compatible backend.
