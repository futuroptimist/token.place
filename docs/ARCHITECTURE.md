# token.place Architecture

This document provides an overview of the token.place architecture, explaining how the system's components interact to provide secure, end-to-end encrypted communication with AI services.

## System Overview

token.place is an end-to-end encrypted proxy service that sits between clients and AI service providers (like OpenAI, Anthropic, etc.). It ensures that the plaintext content of prompts and responses never reaches the token.place servers, while maintaining API compatibility with the original services.

![Architecture Diagram](../assets/architecture_diagram.png)

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
  - Proxies encrypted communications to AI providers
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
   - Server forwards the still-encrypted message to the AI provider
   - AI provider responds with encrypted data
   - Server passes the encrypted response back to the client

4. **Response Decryption**:
   - Client decrypts the response using its AES key
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

- **No Plaintext Storage**: Message content is never stored in plaintext on the server
- **Forward Secrecy**: New AES keys for each message
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

## Deployment Architecture

token.place is designed to be deployed in various configurations:

1. **Self-Hosted**: Run on your own infrastructure
2. **Cloud Services**: Deploy to standard cloud providers
3. **Development Mode**: Local setup for testing and development
