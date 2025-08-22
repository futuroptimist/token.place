# Changelog

## Unreleased

### Fixes
- Validate PKCS#7 unpadding length to reject improperly padded input
- Remove unused imports from simplified CLI client to avoid unnecessary dependencies
- Handle EOF in simplified CLI client to end sessions cleanly
- Deep copy default configuration to prevent cross-test mutations
- Accept memoryview inputs in PKCS#7 padding helpers to support more bytes-like objects

## Version 1.0.0 (March 2025)

### New Features
- Implemented end-to-end encryption for all communications
  - Hybrid encryption using RSA for key exchange and AES for data
  - Encrypted chat on the web interface
  - Encryption support in Python client
  - Encryption support in JavaScript client
- Created OpenAI-compatible API (v1) with the following endpoints:
  - `GET /api/v1/models` - List available models
  - `GET /api/v1/models/{model_id}` - Get model information
  - `POST /api/v1/chat/completions` - Create chat completions
  - `POST /api/v1/completions` - Create text completions
  - `GET /api/v1/public-key` - Get server's public key for encryption
  - `GET /api/v1/health` - API health check
- Added support for both encrypted and unencrypted API requests
- Updated to Llama 3 8B Instruct model

### Improvements
- Complete refactoring of code to improve maintainability
- Added modular API architecture with easy version management
- Enhanced encryption utilities with comprehensive error handling
- Updated web UI with API documentation

### Testing
- Added comprehensive test suite:
  - Unit tests for Python encryption/decryption
  - Unit tests for JavaScript encryption/decryption
  - Cross-compatibility tests between Python and JavaScript encryption
  - End-to-end tests using Playwright
  - API endpoint tests
  - Integration tests for the full system

### Documentation
- Updated README with API documentation
- Added API section to the web interface
- Improved code documentation throughout the codebase
