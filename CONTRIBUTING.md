# Contributing to token.place

Thank you for your interest in contributing to token.place! This document provides guidelines and instructions to help you get started with development.

## Development Environment Setup

### Prerequisites

- Python 3.11 or higher
- Node.js 18 or higher
- Git

### Setting up the development environment

1. Clone the repository:
   ```bash
   git clone https://github.com/your-username/token.place.git
   cd token.place
   ```

2. Create and activate a Python virtual environment:
   ```bash
   python -m venv env
   # Windows
   env\Scripts\activate
   # Linux/macOS
   source env/bin/activate
   ```

3. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Install Node.js dependencies:
   ```bash
    npm ci
   ```

5. Set up pre-commit hooks:
   ```bash
   pre-commit install
   ```

### Unified Workflow

Use the Makefile for common tasks:

```bash
make lint        # run linters and pre-commit checks
make format      # apply formatting
make test        # run all tests
make docker-build  # build Docker images
make k8s-deploy    # deploy manifests
```

## Development Workflow

### Running the server

```bash
python server.py
```

By default, the server runs on http://localhost:5000. You can configure the port and other settings in `config.py`.

### Running Tests

See the comprehensive test guide in [tests/README.md](tests/README.md).

Basic test commands:
```bash
# Run Python tests
python -m pytest

# Run JavaScript tests
npm run test:js

# Run compatibility tests
python tests/test_crypto_compatibility_simple.py
```

## Windows and PowerShell Compatibility

When developing on Windows, be aware of these important compatibility considerations:

1. **Command Chaining**: Use semicolons (`;`) instead of ampersands (`&&`) for command chaining in PowerShell:
   ```powershell
   # CORRECT
   cd scripts; python example.py

   # INCORRECT (will cause errors)
   cd scripts && python example.py
   ```

2. **IPv4/IPv6 Compatibility**: Always use explicit IPv4 addresses (`127.0.0.1`) instead of `localhost` for network services to avoid IPv6 resolution issues:
   ```powershell
   # PREFERRED
   curl http://127.0.0.1:5000/test

   # MAY CAUSE ISSUES
   curl http://localhost:5000/test
   ```

3. **Path Separators**: Use backslashes (`\`) for Windows paths:
   ```powershell
   cd tests\unit
   ```

4. **Port Management**: Check for and clean up conflicting processes:
   ```powershell
   # List processes using a port
   netstat -ano | findstr :5000

   # Kill a process by PID
   taskkill /F /PID <pid>
   ```

## Understanding the Encryption System

token.place employs a hybrid encryption approach that combines the security of RSA with the performance of AES:

### Key Components

1. **RSA Key Pair Generation**:
   - Each server generates a 2048-bit RSA key pair on startup
   - The public key is shared with clients, while the private key remains secure on the server

2. **AES for Message Encryption**:
   - Messages are encrypted using AES-256 in CBC mode
   - A unique random AES key is generated for each message
   - The AES key itself is encrypted with the recipient's RSA public key

3. **Cross-language Compatibility**:
   - The encryption system is implemented in both Python and JavaScript
   - All implementations must maintain byte-level compatibility

### Code Organization

- `encrypt.py`: Core encryption/decryption functions
- `utils/crypto/crypto_manager.py`: High-level interface for the encryption system
- `static/chat.js`: JavaScript client implementation

When making changes to the encryption system, be careful to maintain compatibility between all implementations. Always run the compatibility tests after modifying encryption code.

## Testing Framework

The project maintains a comprehensive test suite to ensure quality and prevent regressions:

### Test Types

1. **Unit Tests**:
   - Located in `tests/unit/`
   - Test individual components in isolation
   - Run with `python -m pytest tests/unit/`

2. **Integration Tests**:
   - Located in `tests/integration/`
   - Test interactions between components
   - Run with `python -m pytest tests/integration/`

3. **End-to-End Tests**:
   - Located in `tests/`
   - Test complete workflows
   - Run with `python -m pytest tests/test_e2e_*.py`

4. **API Tests**:
   - Located in `tests/test_api.py`
   - Verify API functionality and compatibility
   - Run with `python -m pytest tests/test_api.py`

5. **Compatibility Tests**:
   - Test cross-language functionality
   - Run with `python -m pytest tests/test_crypto_compatibility*.py`

6. **Failure Recovery Tests**:
   - Test system resilience against errors
   - Run with `python -m pytest tests/test_failure_recovery.py`

### Writing Tests

When adding new features or fixing bugs, please include appropriate tests:

- For bug fixes, include a test that would have caught the bug
- For new features, add tests that verify the feature works as expected
- Always run the full test suite before submitting a PR

### Testing Best Practices

1. **Network Testing**:
   - Always use explicit IPv4 addresses (`127.0.0.1`) instead of `localhost`
   - Allow sufficient time for servers to start before testing
   - Check for port conflicts before starting services

2. **Cross-Platform Testing**:
   - Test on both Windows and Linux when possible
   - Be aware of path separator differences (`\` vs `/`)
   - Use platform-specific commands in the appropriate scripts

3. **Cleanup**:
   - Always clean up processes after testing
   - Check for and terminate orphaned processes that might interfere with future tests

## Coding Standards

### Python

- Follow [PEP 8](https://www.python.org/dev/peps/pep-0008/) coding style
- Use type hints for function parameters and return values
- Include docstrings for classes and functions (Google style)
- Maintain test coverage for all new code

### JavaScript

- Follow the ESLint configuration for the project
- Write documentation using JSDoc comments
- Maintain compatibility with the Python implementation for crypto functions

### Naming and Stylization

- Always style the project name as lowercase `token.place` (not Title case "Token.place")
- Follow the complete style guidelines in [docs/STYLE_GUIDE.md](docs/STYLE_GUIDE.md)
- Use appropriate naming conventions for your code (snake_case for Python, camelCase for JavaScript)

## Architecture Guidelines

### Encryption Components

- All encryption/decryption operations should be performed through the `CryptoManager` class
- Cross-language compatibility between Python and JavaScript is essential
- Follow the hybrid encryption pattern established in the codebase

### Server Components

- Keep the server stateless where possible
- Maintain API compatibility with the underlying AI services
- Handle encryption/decryption transparently to clients

## Documentation

- Update documentation when making changes to APIs or adding new features
- Follow the style guidelines in [docs/STYLE_GUIDE.md](docs/STYLE_GUIDE.md)
- Make sure your changes are reflected in both code comments and external documentation

## Pull Request Process

1. Create a new branch for your feature or bugfix (`git checkout -b feature/my-feature` or `git checkout -b fix/my-bugfix`)
2. Make your changes, following the coding standards
3. Write tests for your changes
4. Update documentation as needed
5. Run `pre-commit run --all-files` and ensure all tests pass
6. Submit a pull request to the main repository
7. Address any review comments

## Common Pitfalls and Tips

- **Encryption**: Make sure the JavaScript and Python implementations remain compatible
- **Cross-platform**: Test your changes on both Windows and Linux if possible
- **Performance**: Be mindful of encryption performance, especially for large payloads
- **Dependencies**: Keep external dependencies to a minimum to reduce security risks
- **Stylization**: Remember to use `token.place` (lowercase) in documentation and communication
- **PowerShell**: On Windows, use `;` for command chaining, not `&&`
- **Network Services**: Use explicit IPv4 (`127.0.0.1`) for reliable connection testing
- **Port Management**: Be vigilant about checking for and clearing orphaned processes

## License

By contributing to token.place, you agree that your contributions will be licensed under the
project's [LICENSE](LICENSE) file.

## Contact

If you have questions or need assistance, please open an issue or contact the maintainers.
