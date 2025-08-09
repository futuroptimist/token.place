# token.place Utilities

This directory contains utility modules for token.place that provide reusable functionality across the project.

## Available Utilities

### Path Handling (`path_handling.py`)

Cross-platform path handling utilities that ensure consistent behavior across Windows, macOS, and Linux.
These helpers now fall back to standard `AppData` locations when Windows environment variables are missing
and automatically create directories when accessed.

On Linux, these functions honor the `XDG_DATA_HOME`, `XDG_CONFIG_HOME`, and
`XDG_CACHE_HOME` environment variables when they are set.

- `normalize_path(path)`: Expands `~` and environment variables then returns a normalized absolute path.
- `ensure_dir_exists(path)`: Creates the directory if missing (expands `~` to the user's home) and raises
  `NotADirectoryError` when the path points to an existing file.
- `get_app_data_dir()`: Returns the platform-specific application data directory and ensures it exists.

### Crypto Helpers (`crypto_helpers.py`)

Simplifies encryption and decryption operations for end-to-end encrypted communication with the token.place server and relay.

Network requests in this module now use a default 10 second timeout to prevent
hanging connections. You can override this by passing a `timeout` argument to
`CryptoClient.fetch_server_public_key` or `CryptoClient.send_encrypted_message`.

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
