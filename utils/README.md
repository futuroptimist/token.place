# token.place Utilities

This directory contains utility modules for token.place that provide reusable functionality across the project.

The package's top level re-exports a few commonly used helpers, so you can import them directly:

```python
from utils import get_model_manager, get_crypto_manager, RelayClient
```

## Available Utilities

### Path Handling (`path_handling.py`)

Cross-platform path handling utilities that ensure consistent behavior across Windows, macOS, and Linux.
These helpers now fall back to standard `AppData` locations when Windows environment variables are missing
and automatically create directories when accessed.

On Linux, these functions honor the `XDG_DATA_HOME`, `XDG_CONFIG_HOME`,
`XDG_CACHE_HOME`, and `XDG_STATE_HOME` environment variables when they are set.

- `normalize_path(path)`: Accepts strings or `os.PathLike` objects, strips surrounding whitespace, expands `~` and environment
  variables, then returns a normalized absolute path. Raises a `TypeError` when `path` is `None` and `ValueError` for empty
  strings.
- `ensure_dir_exists(path)`: Accepts strings or `os.PathLike` objects, strips whitespace and creates the directory if missing,
  expanding `~` and environment variables, and raises `NotADirectoryError` when the path points to an existing file. Passing
  `None` now raises `TypeError`, and empty paths raise `ValueError`.
- `get_app_data_dir()`: Returns the platform-specific application data directory and ensures it exists.
- `get_logs_dir()`: Returns the platform-specific logs directory and ensures it exists.
- `get_relative_path(path, base_path)`: Returns `path` relative to `base_path`, using `..` segments when the
  two locations do not share a common ancestor. If the paths are on different drives
  (Windows), the absolute `path` is returned instead of raising an error. Passing a
  base path that points to a file now raises `NotADirectoryError`.

### Crypto Helpers (`crypto_helpers.py`)

Simplifies encryption and decryption operations for end-to-end encrypted communication with the token.place server and relay.
`CryptoClient.decrypt_message` now validates required fields and returns `None` when data is incomplete or
malformed to prevent unexpected exceptions. `CryptoClient.send_encrypted_message` returns `None` when a 200 OK
response cannot be decoded as JSON, avoiding unhandled `ValueError` exceptions.

### Crypto Manager (`crypto/crypto_manager.py`)

Manages server-side encryption keys and message processing. The
`decrypt_message` helper now returns raw bytes when decrypted content is not
valid UTF-8 or JSON and returns `None` when given non-dict input to avoid
attribute errors.

Network requests in this module now use a default 10 second timeout to prevent
hanging connections. You can override this by passing a `timeout` argument to
`CryptoClient.fetch_server_public_key` or `CryptoClient.send_encrypted_message`.
`encrypt_message` validates inputs, raising a `ValueError` for `None` and a
`TypeError` for unsupported message types to avoid confusing cryptography
errors. Passing `None` previously encrypted the string "None"; now it raises
`ValueError` to surface invalid inputs early.

## Crypto Helpers

The `CryptoClient` class provides a high-level abstraction over the encryption/decryption process, making it easy to:

- Fetch public keys from the server
- Encrypt messages for the server
- Decrypt messages from the server
- Send encrypted chat messages
- Make encrypted API requests

### Basic Usage

```python
from utils.crypto_helpers import CryptoClient

# Create a client
client = CryptoClient('http://localhost:5010')

# Fetch the server's public key
if client.fetch_server_public_key():
    # Send a chat message
    response = client.send_chat_message("Hello, world!")

    # Print the assistant's response
    for message in response:
        if message["role"] == "assistant":
            print(message["content"])
```

### API Usage

```python
from utils.crypto_helpers import CryptoClient

# Create a client for API access
client = CryptoClient('http://localhost:5010')

# Fetch the API public key
if client.fetch_server_public_key('/api/v1/public-key'):
    # Send an API request
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Explain encryption in simple terms."}
    ]

    response = client.send_api_request(messages)

    if response:
        print(response['choices'][0]['message']['content'])
```

### Testing

The `CryptoClient` can also be used to simplify testing:

```python
def test_with_crypto_client(setup_servers):
    """Test using the CryptoClient for cleaner test code"""
    # Create a client
    client = CryptoClient(base_url)

    # Verify connection
    assert client.fetch_server_public_key() is True

    # Test sending a message
    response = client.send_chat_message("Test message")

    # Assertions on response
    assert response is not None
    assert len(response) >= 2
    assert response[-1]["role"] == "assistant"
```

### Benefits

Using the `CryptoClient` has several advantages:

1. **Reduces boilerplate**: Eliminates repetitive encryption/decryption code
2. **Improves readability**: Makes tests and application code more concise and focused
3. **Centralizes logic**: Puts all encryption-related operations in one place
4. **Simplifies maintenance**: Changes to encryption protocols only need to be updated in one place
5. **Encapsulates complexity**: Hides the details of key management and encryption behind a clean API
