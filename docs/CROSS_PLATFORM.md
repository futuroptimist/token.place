# Cross-Platform and Containerization Support

This document explains the improvements made to make token.place work seamlessly across Windows, macOS, and Linux platforms, as well as containerization for easy deployment.

## Table of Contents

1. [Docker Containerization](#docker-containerization)
2. [Cross-Platform Path Handling](#cross-platform-path-handling)
3. [Configuration System](#configuration-system)
4. [Platform-Specific Launchers](#platform-specific-launchers)
5. [Cross-Platform Testing](#cross-platform-testing)
6. [Development Guidelines](#development-guidelines)
7. [Compatibility Status](#compatibility-status)
8. [Next Steps](#next-steps)

## Docker Containerization

We've implemented Docker support for easy deployment:

- `docker/Dockerfile.relay` - Relay component container
- `config/docker-compose.yml` - Launches the relay service

### Building and Running with Docker

```bash
# Build and start the relay service
docker-compose up -d

# View logs
docker-compose logs -f

# Stop the service
docker-compose down
```

### Container Architecture

Each container follows a multi-stage build pattern to minimize size:
- First stage installs build dependencies and Python packages
- Second stage copies only the necessary files for runtime
- Non-root user for better security
- Volume mounts for persistent data

## Cross-Platform Path Handling

We've implemented a comprehensive path handling system in `utils/path_handling.py` that:

- Uses `pathlib.Path` consistently for cross-platform compatibility
- Detects the operating system and uses appropriate paths
- Handles user directories, config paths, and cache locations correctly per platform
- Ensures directories exist before attempting to use them

### Platform-Specific Paths

| Path Type | Windows | macOS | Linux |
|-----------|---------|-------|-------|
| Config | %APPDATA%\token.place\config | ~/Library/Application Support/token.place/config | ~/.config/token.place |
| Data | %APPDATA%\token.place | ~/Library/Application Support/token.place | ~/.local/share/token.place |
| Cache | %LOCALAPPDATA%\token.place\cache | ~/Library/Caches/token.place | ~/.cache/token.place |
| Logs | %APPDATA%\token.place\logs | ~/Library/Logs/token.place | ~/.local/state/token.place/logs |

## Configuration System

The new configuration system in `config.py` provides:

- Environment-specific settings (development, testing, production)
- Platform detection and adaptation
- Hierarchical configuration with dot notation access
- User-defined configuration overrides
- Sensible defaults for all components

### Using the Configuration System

```python
from config import get_config

# Get the global configuration instance
config = get_config()

# Access configuration values
server_port = config.get('server.port')
debug_mode = config.get('server.debug')

# Set configuration values
config.set('server.workers', 8)

# Save user configuration
config.save_user_config()

# Check environment and platform
if config.is_development and config.is_windows:
    # Windows-specific development logic
    pass
```

## Platform-Specific Launchers

The repository now uses a `Makefile` for common tasks.

```bash
make docker-build  # build container images
make k8s-deploy    # deploy manifests to your cluster
```

## Cross-Platform Testing

We've implemented a comprehensive testing framework that:

- Detects and adapts to the current platform
- Uses temporary directories for test isolation
- Tests platform-specific code paths
- Provides fixtures for common test scenarios

### Running Tests

```bash
# Run all tests
pytest

# Run platform-specific tests
pytest tests/platform_tests/

# Run a specific test
pytest tests/platform_tests/test_path_handling.py

# Run with verbose output
pytest -v
```

## Development Guidelines

When contributing to the codebase, please follow these guidelines:

1. **Path Handling**:
   - Always use `pathlib.Path` for path manipulation
   - Import path utilities from `utils.path_handling`
   - Never use hardcoded directory separators (`\` or `/`)

2. **Configuration**:
   - Store configuration values in the config system, not as constants
   - Use `config.get()` to retrieve configuration values
   - Add new settings to the DEFAULT_CONFIG dictionary

3. **Platform Detection**:
   - Use the `config.is_windows`, `config.is_macos`, and `config.is_linux` properties
   - Add platform-specific code branches where necessary
   - Test on all supported platforms

4. **Testing**:
   - Add tests for new functionality
   - Include platform-specific tests when adding platform-dependent code
   - Use the provided fixtures for temporary directories and files

5. **Docker**:
   - Update Docker files when adding new dependencies
   - Test Docker builds on your platform before submitting changes

## Compatibility Status

We've successfully implemented and tested cross-platform compatibility for token.place:

| Feature | Windows | macOS | Linux | Docker |
|---------|---------|-------|-------|--------|
| Path handling | ✅ | ✅ | ✅ | ✅ |
| Configuration | ✅ | ✅ | ✅ | ✅ |
| Launchers | ✅ | ✅ | ✅ | ✅ |
| Testing | ✅ | ✅ | ✅ | ✅ |
| Server | ✅ | ✅ | ✅ | ✅ |
| Relay | ✅ | ✅ | ✅ | ✅ |
| API | ✅ | ✅ | ✅ | ✅ |
| Crypto Utilities | ✅ | ✅ | ✅ | ✅ |

All unit tests pass successfully on all platforms.

**Notes**:
- UI tests require a running server on localhost:5010
- Streaming tests are skipped as that feature is still in development
- End-to-end network tests require multiple components to be running

## Simplified Encryption with CryptoClient

We've added a `CryptoClient` utility in `utils/crypto_helpers.py` that simplifies encryption/decryption operations for secure communication with the token.place server. This utility:

- Handles generating and managing encryption keys
- Simplifies fetching server public keys
- Provides easy-to-use methods for encrypting/decrypting messages
- Works seamlessly across all platforms

### Using CryptoClient

```python
from utils.crypto_helpers import CryptoClient

# Create a client for the relay server
client = CryptoClient('http://localhost:5010')

# Fetch the server's public key
client.fetch_server_public_key()

# Send an encrypted message and get the response
response = client.send_chat_message("Hello, how are you?")

# For API access
client.fetch_server_public_key('/api/v1/public-key')
messages = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Tell me a joke."}
]
api_response = client.send_api_request(messages)
```

The `CryptoClient` utility significantly reduces the amount of boilerplate code needed to work with the token.place encryption system, making it easier to build secure applications that communicate with the platform.

See the detailed documentation in `utils/README.md` for more information on using the CryptoClient.

## Next Steps

To further enhance cross-platform support, future work includes:

1. **Cross-Platform Installers**:
   - Windows installer (.msi)
   - macOS package (.pkg)
   - Linux package (.deb, .rpm)
   - Windows `.exe` and macOS `.dmg` builds are generated automatically via GitHub Actions

2. **Platform-Specific UI Optimizations**:
   - Native UI integrations
   - Platform-specific UX enhancements
   - Touch support for mobile devices

3. **Performance Tuning**:
   - Platform-specific performance optimizations
   - Hardware acceleration on supported platforms
   - Resource usage monitoring and tuning

4. **CI/CD Pipeline**:
   - Matrix testing across all supported platforms
   - Automated builds for all platforms
   - Containerized testing environments
