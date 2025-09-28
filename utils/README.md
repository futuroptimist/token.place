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
Environment variable values are stripped of surrounding whitespace before use.

- `normalize_path(path)`: Accepts strings or `os.PathLike` objects, strips surrounding whitespace, expands `~` and environment
  variables, then returns a normalized absolute path. Raises a `TypeError` when `path` is `None` and `ValueError` for empty
  strings or when environment variables remain unexpanded.
- `ensure_dir_exists(path)`: Accepts strings or `os.PathLike` objects, strips whitespace and creates the directory if missing,
  expanding `~` and environment variables, and raises `NotADirectoryError` when the path points to an existing file. Passing
  `None` now raises `TypeError`, and empty paths or unexpanded environment variables raise `ValueError`.
- `get_app_data_dir()`: Returns the platform-specific application data directory and ensures it exists.
- `get_logs_dir()`: Returns the platform-specific logs directory, creating it if missing.
- `get_temp_dir()`: Returns a token.place subdirectory inside the system temporary
  directory and ensures it exists.
- `is_subpath(path, parent)`: Returns `True` when `path` is the same as or within `parent`.
- `get_relative_path(path, base_path)`: Returns `path` relative to `base_path`, using `..` segments when the
  two locations do not share a common ancestor. If the paths are on different drives
  (Windows), the absolute `path` is returned instead of raising an error. Passing a
  base path that points to a file now raises `NotADirectoryError`.

### Crypto Helpers (`crypto_helpers.py`)

Simplifies encryption and decryption operations for end-to-end encrypted communication with the token.place server and relay.
`CryptoClient.decrypt_message` now validates required fields, returns `None` when data is incomplete or
malformed to prevent unexpected exceptions, and correctly yields an empty string when the decrypted content is empty.
`CryptoClient.send_encrypted_message` returns `None` when a 200 OK response cannot be decoded as JSON, avoiding
unhandled `ValueError` exceptions.

### Crypto Manager (`crypto/crypto_manager.py`)

Manages server-side encryption keys and message processing. The
`decrypt_message` helper accepts dicts or JSON strings, returns raw bytes when
decrypted content is not valid UTF-8 or JSON, and yields `None` for invalid
inputs or when required fields are missing.

Network requests in this module now use a default 10 second timeout to prevent
hanging connections. You can override this by passing a `timeout` argument to
`CryptoClient.fetch_server_public_key` or `CryptoClient.send_encrypted_message`.
`encrypt_message` validates inputs, raising a `ValueError` for `None` and a
`TypeError` for unsupported message types to avoid confusing cryptography
errors. Passing `None` previously encrypted the string "None"; now it raises
`ValueError` to surface invalid inputs early. `encrypt_message` also accepts
raw ``bytes`` messages in addition to strings and JSON-serializable objects.
It now trims whitespace from base64-encoded public keys before decoding so
keys copied with line breaks do not raise errors.

With the new performance monitor integration, successful encrypt and decrypt
operations optionally record payload sizes, durations, and throughput metrics
when the `TOKEN_PLACE_PERF_MONITOR=1` environment variable is set. Metrics can
be inspected through `utils.performance.get_encryption_monitor()` for basic
resource usage tuning during profiling sessions.

### Performance Monitoring (`performance/monitor.py`)

Provides a lightweight in-memory collector that tracks recent encryption and
decryption samples. Enable it by setting `TOKEN_PLACE_PERF_MONITOR=1` (and
optionally `TOKEN_PLACE_PERF_SAMPLES` to control the sliding window length).
The aggregated statistics expose counts, average payload sizes, operation
durations, and computed throughput so you can baseline performance while
keeping overhead negligible in production where the monitor remains disabled
by default.

## Crypto Helpers

The `CryptoClient` class provides a high-level abstraction over the encryption/decryption process, making it easy to:

- Fetch public keys from the server
- Check if the server public key has been loaded
- Encrypt messages for the server
- Decrypt messages from the server
- Send encrypted chat messages
- Make encrypted API requests

### Basic Usage

The `CryptoClient` requires a base URL with an explicit `http://` or `https://` scheme.

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
