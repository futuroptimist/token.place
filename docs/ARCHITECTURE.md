# token.place Architecture

This document provides an overview of the token.place architecture, explaining how the system's components interact to provide secure, end-to-end encrypted communication with AI services.

## System Overview

token.place is an end-to-end encrypted proxy service that sits between clients and AI service providers (like OpenAI, Anthropic, etc.). In distributed inference, the selected compute node decrypts prompts and generates responses; an honest relay handles ciphertext and safe routing metadata. Current clients trust relay-selected compute keys rather than independently authenticating the operator. See the [K061 verified-compute proposal](design/verified-compute-trust.md) for that trust gap and the proposed migration.

```mermaid
flowchart TD
    user([End user])
    clientApp[Client app<br/>(browser, CLI, SDK)]
    cryptoClient[Local crypto helpers]
    relay[(Relay cluster)]
    server[server.py
    token.place core]
    provider[(AI provider)]

    user --> clientApp
    clientApp --> cryptoClient
    cryptoClient -->|Encrypt request| relay
    relay -->|Forward ciphertext| server
    server -->|Proxy API call| provider
    provider -->|Model response| server
    server -->|Encrypted reply| relay
    relay -->|Opaque ciphertext| cryptoClient
    cryptoClient -->|Decrypt for user| clientApp
```

Client helpers encrypt to the selected compute node. The honest relay forwards ciphertext;
`server.py` decrypts it for inference. Any configured downstream provider receiving that request
is also inside the plaintext trust boundary. The relay cannot independently certify its own
honesty through key discovery.

## Key Components

### 1. Client-Side Components

- **JavaScript Client Library** (`static/chat.js`):
  - Generates client-side RSA key pairs
  - Encrypts messages using a hybrid RSA-AES approach
  - Decrypts responses from the server
  - Provides a drop-in replacement for standard API clients

### 2. Server-Side Components

- **Server Application** (`server.py`):
  - Handles client requests
  - Decrypts requests for inference and encrypts responses for clients
  - Manages server-side keys
  - Implements API-compatible endpoints

- **CryptoManager** (`utils/crypto/crypto_manager.py`):
  - Generates and manages RSA key pairs
  - Provides encryption/decryption services
  - Manages secure session handling
  - Validates presence of client public keys before encryption
  - Supports key rotation via `rotate_keys()` to regenerate RSA keys

- **Model Manager** (`utils/models/model_manager.py`):
  - Connects to various AI providers
  - Handles provider-specific API requirements
  - Manages model configurations

### 3. Core Libraries

- **Encryption Implementation** (`encrypt.py`):
  - Provides the core encryption and decryption functions
  - Implements hybrid RSA-AES encryption
  - Uses RSA-OAEP by default with an optional PKCS#1 v1.5 mode for legacy JavaScript
    compatibility
  - Ensures compatibility between Python and JavaScript implementations

## Data Flow

1. **Client Initialization**:
   - Client generates an RSA key pair
   - Client retrieves the server's public key

2. **Request Encryption**:
   - Client generates a random AES key and initialization vector (IV)
   - Message is encrypted with AES using CBC mode and PKCS7 padding
     (or AES-GCM when authenticated encryption is requested)
   - AES key is encrypted with the server's public RSA key
   - Encrypted message, encrypted AES key, and IV are sent to the server

3. **Server Processing**:
   - Server receives the encrypted package
   - Server decrypts the AES key using its private RSA key
   - Compute decrypts the message and runs inference (or invokes its configured provider)
   - Compute encrypts the completed response to the client public key
   - Relay forwards the response ciphertext

4. **Response Decryption**:
   - Client unwraps the response AES key with its private key and decrypts the response
   - Decrypted content is presented to the user

## Encryption Details

token.place uses a hybrid encryption approach:

1. **RSA (2048-bit)** for secure key exchange:
   - Used to encrypt/decrypt the AES key
   - Provides asymmetric encryption for secure key transmission

2. **AES-256** for message content:
   - Uses a randomly generated key for each message
   - CBC mode (default) employs PKCS7 padding for compatibility with existing clients
   - GCM mode (optional) adds integrity protection for model weights and inference payloads
   - Provides efficient symmetric encryption for potentially large messages

3. **Compatibility Measures**:
   - Base64 encoding for cross-language compatibility
   - Consistent padding and mode selection between JavaScript and Python

## Security Considerations

- **Relay boundary**: Honest relay state remains ciphertext-only; compute sees plaintext.
- **Key authentication**: Current relay-selected keys require trust in that relay; response
  encryption to a public client key alone does not authenticate the compute sender.
- **Forward secrecy limit**: Fresh AES keys alone do not provide forward secrecy if recorded
  wrapped keys can later be decrypted with a compromised recipient private key.
- **Client-Side Key Generation**: Private keys never leave the client
- **Error Handling**: Non-revealing error messages to prevent oracle attacks
- **Cross-Platform Testing**: Rigorous testing across both Python and JavaScript implementations

## Scalability and Performance

- **Stateless Design**: Servers can be horizontally scaled
- **Efficient Encryption**: Hybrid approach minimizes performance impact
- **Minimized Dependencies**: Few external libraries to reduce security surface area and improve performance

## Testing Architecture

The system is tested at multiple levels:

1. **Unit Tests**: Individual components tested in isolation
2. **Integration Tests**: Interactions between components verified
3. **Cross-Language Tests**: Ensures Python and JavaScript implementations remain compatible
4. **API Compatibility Tests**: Verifies functionality with various AI providers


## Migration alignment (current vs target)

Canonical sequencing lives in [roadmap/desktop_compute_node_migration.md](roadmap/desktop_compute_node_migration.md).

- **Current state:** `server.py` is the canonical compute-node runtime/entrypoint; `relay.py` is
  the canonical relay entrypoint; desktop-tauri is MVP only.
- **Legacy compatibility:** `server/server_app.py` is shim-only and delegates into `server.py` to
  prevent dual server implementations from drifting.
- **Near-term:** `server.py` and desktop-tauri co-evolve through a shared compute-node runtime while
  relay operations move onto sugarkube (`relay.py` only).
- **Target state (later):** post-parity migration to API v1-aligned distributed compute contracts.
- **Operator platform focus:** Windows 11 + CUDA/NVIDIA and macOS Apple Silicon + Metal, with CPU
  fallback and later Raspberry Pi support.

Related operations docs:

- [relay_sugarkube_onboarding.md](relay_sugarkube_onboarding.md)
- [k3s-sugarkube-dev.md](k3s-sugarkube-dev.md)
- [k3s-sugarkube-staging.md](k3s-sugarkube-staging.md)
- [k3s-sugarkube-prod.md](k3s-sugarkube-prod.md)

## Deployment Architecture

token.place is designed to be deployed in various configurations:

1. **Self-Hosted**: Run on your own infrastructure
2. **Cloud Services**: Deploy to standard cloud providers
3. **Development Mode**: Local setup for testing and development
