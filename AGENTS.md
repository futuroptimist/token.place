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

- [README.md](README.md): Project overview and getting started instructions
- [requirements.txt](requirements.txt): Python dependencies for the project
- [package.json](package.json): Node.js dependencies for JavaScript components

## Core Components

- [encrypt.py](encrypt.py): The core encryption/decryption implementation in Python
- [static/chat.js](static/chat.js): JavaScript client library for encryption/decryption in browsers
- [utils/crypto/crypto_manager.py](utils/crypto/crypto_manager.py): Python class managing key generation and encryption/decryption operations
- [server.py](server.py): Main server implementation for the proxy service
- [relay.py](relay.py): Middleware server that forwards encrypted messages between clients and servers

## Documentation

- [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md): Contribution guidelines for developers
- [docs/STYLE_GUIDE.md](docs/STYLE_GUIDE.md): Style and branding guidelines, including proper name stylization
- [docs/TESTING.md](docs/TESTING.md): Comprehensive guide to the testing approach
- [docs/TESTING_IMPROVEMENTS.md](docs/TESTING_IMPROVEMENTS.md): Ideas for further testing improvements
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): Detailed architectural overview of the system
- [docs/LLM_ASSISTANT_GUIDE.md](docs/LLM_ASSISTANT_GUIDE.md): Guide for AI assistants working with this codebase

## User Journeys

The project tests are organized around key user journeys that represent how users interact with the system:

1. **End-to-End Encrypted Conversation**: A user sends an encrypted message through the relay to an LLM server and receives an encrypted response
2. **Browser-Based Chat Interface**: A user accesses the web interface, sends messages through the browser, and sees responses
3. **OpenAI-Compatible API Usage**: A developer uses the API with standard OpenAI client libraries
4. **Cross-Platform Functionality**: Users run token.place on different operating systems
5. **Security and Failure Recovery**: System maintains security and recovers from errors during use
6. **Real LLM Integration**: A user interacts with a real LLM model through the system
7. **Integration with External Applications**: token.place is used as a drop-in replacement for OpenAI in external applications

Each journey is mapped to specific test files that verify the functionality works as expected. See [docs/TESTING.md](docs/TESTING.md) for details.

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

- [tests/unit/](tests/unit/): Unit tests for individual components
- [tests/test_e2e_conversation_flow.py](tests/test_e2e_conversation_flow.py): End-to-end tests for conversation flows
- [tests/test_crypto_compatibility_local.py](tests/test_crypto_compatibility_local.py): Tests for crypto compatibility between Python and JavaScript
- [tests/test_performance_benchmarks.py](tests/test_performance_benchmarks.py): Performance benchmark tests
- [tests/test_failure_recovery.py](tests/test_failure_recovery.py): Tests for system resilience and error recovery
- [tests/test_security.py](tests/test_security.py): Tests for security properties
- [tests/test_real_llm.py](tests/test_real_llm.py): Tests with actual LLM models

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

## Configuration

- [config.py](config.py): Configuration management for different environments
- [utils/logging/logger.py](utils/logging/logger.py): Logging infrastructure for the application
- [pytest.ini](pytest.ini): Configuration for Python test suite 
