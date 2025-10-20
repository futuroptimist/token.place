# Streaming Implementation Guide for token.place

This guide outlines the steps required to implement streaming inference in the token.place application, allowing users to see responses as they are generated.

> **Status update (2025-10-20):** `/api/v2/chat/completions` now supports Server-Sent Events for both plaintext **and encrypted** requests when the `stream` flag is provided. The Flask route wraps each OpenAI-style chunk in an encrypted envelope so clients can continue using `CryptoClient.decrypt_message` to recover the delta payloads. **API v1 intentionally remains non-streaming** so we can stabilize the surface area before promoting API v2 to GA; ignore any TODOs suggesting `/api/v1/.../stream` routes.

## Architecture Overview

The current architecture follows this flow:
```
Client → Encrypted Request → Relay → Server → LLM → Complete Response → Relay → Client
```

The streaming architecture would be:
```
Client → Encrypted Request → Relay → Server → LLM → Streaming Chunks → Encrypted Chunks → Relay → Client → Real-time Display
```

## Implementation Steps

### 1. Server-side Changes

- ✅ `/api/v2/chat/completions` streams plaintext responses via SSE (role + content + stop markers) when `stream=true`.

#### 1.1 LLM Integration
- Modify `llama_cpp_get_response` to use the streaming mode of llama-cpp-python
- Implement a generator function that yields tokens as they become available
- Example:
  ```python
  def llama_cpp_stream_response(chat_history):
      model_instance = get_llm_instance()
      if model_instance is None:
          yield {"error": "Model not available"}
          return

      try:
          # Use the streaming API
          for chunk in model_instance.create_chat_completion(
              messages=chat_history,
              stream=True
          ):
              if "choices" in chunk and chunk["choices"]:
                  content = chunk["choices"][0].get("delta", {}).get("content", "")
                  if content:
                      yield {"chunk": content}
      except Exception as e:
          yield {"error": str(e)}
  ```

#### 1.2 Encryption for Streaming
- Modify the encryption system to handle small chunks
- ✅ Create a new encryption function for streams that maintains session keys
  - Added `encrypt_stream_chunk`/`decrypt_stream_chunk` helpers in `encrypt.py` that reuse a
    negotiated AES session across sequential SSE payloads while still supporting optional
    associated data.
- ✅ `api/v2/routes.py` now emits encrypted SSE envelopes when `encrypted=true`
  requests ask for `stream=true`, allowing clients to decrypt each chunk without
  changing their existing helpers.
- Example:
  ```python
  def encrypt_stream(data_chunk, client_public_key, session_key=None):
      """Encrypt a stream chunk using an existing session key or create a new one"""
      if session_key is None:
          # First chunk - create new session key
          session_key = generate_session_key()
          # Encrypt session key with RSA
          encrypted_session_key = rsa_encrypt(session_key, client_public_key)
          # Return both
          return encrypt_with_key(data_chunk, session_key), encrypted_session_key, session_key
      else:
          # Subsequent chunks - use existing session key
          return encrypt_with_key(data_chunk, session_key), None, session_key
  ```

#### 1.3 Relay Communication
- ✅ Implemented a new `/stream/source` endpoint in the relay server to ingest streaming chunks
  from compute nodes.
- ✅ Modified the polling mechanism so `/sink` surfaces `stream_session_id` metadata for streaming
  clients, letting servers bind chunk uploads to the correct session.
- ✅ Store streaming state to track active streams using thread-safe registries exposed through the
  `/stream/retrieve` client endpoint.

### 2. Client-side Changes

#### 2.1 Streaming API Client
- ✅ Implemented streaming client chunk processing via `CryptoClient.stream_chat_completion`
- ✅ Added support for decrypting encrypted streaming chunks in `CryptoClient.stream_chat_completion`
- ✅ Added automatic reconnection with configurable retries in `CryptoClient.stream_chat_completion`
- Example:
  ```javascript
  async function streamRequest(messages) {
      // Initial request
      const response = await fetch('/api/stream', {
          method: 'POST',
          body: JSON.stringify({ messages })
      });

      // Create a reader for the stream
      const reader = response.body.getReader();

      // Session key for decryption (will be set on first chunk)
      let sessionKey = null;

      while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          // Process the chunk
          const chunk = JSON.parse(new TextDecoder().decode(value));

          // Handle first chunk with session key
          if (chunk.session_key) {
              sessionKey = decryptSessionKey(chunk.session_key);
          }

          // Decrypt and process content
          const decryptedContent = decryptWithKey(chunk.content, sessionKey);

          // Update UI with the new content
          updateStreamingUI(decryptedContent);
      }
  }
  ```

#### 2.2 UI Updates
- Modify the UI to handle incremental updates
- ✅ Implemented a typing effect for a better user experience (`static/chat.js` + `static/chat_typing.js`)
- ✅ Added support for rendering markdown/formatting as it arrives via a sanitized renderer in the chat UI

### 3. API Endpoints

#### 3.1 Add Streaming Endpoints
- ✅ `/api/v2/chat/completions` streams plaintext responses via SSE when `stream=true`.
- 🚫 **Do not implement** `/api/v1/.../stream` endpoints; v1 will stay JSON-only until it is formally deprecated in favor of API v2.

#### 3.2 Update API Documentation
- ✅ README now includes a "streaming usage" section with SSE examples and `stream=true`
  guidance for the `/api/v2` and `/v2` aliases.

### 4. Testing Infrastructure

#### 4.1 Streaming Tests
- Implement tests for streaming functionality
- ✅ Test different chunk sizes and timing scenarios (`tests/test_streaming.py::test_v2_streaming_handles_varied_chunk_sizes_and_delays`)
- Verify encryption/decryption with streaming

#### 4.2 Integration Tests
- ✅ Added end-to-end coverage for the streaming experience via
  `tests/test_e2e_conversation_flow.py::test_streaming_chat_completion_end_to_end`,
  which boots the relay + server stack and asserts the SSE role/content/stop
  sequence emitted by `/api/v2/chat/completions`.
- ✅ Test reconnection and error handling

## Implementation Phases

1. **Phase 1**: Server-side streaming without encryption
2. **Phase 2**: Encryption for streaming content
3. **Phase 3**: Client-side processing and UI updates
4. **Phase 4**: Full integration and testing
5. **Phase 5**: Performance optimization

## Security Considerations

- ✅ Ensure each chunk is properly encrypted via StreamSession IV collision detection and retry logic
- Implement reconnection logic that maintains encryption state
- ✅ Verify that partial responses cannot be intercepted or modified
  - Covered by `tests/unit/test_crypto_helpers_streaming.py::test_stream_chat_completion_flags_tampered_encrypted_chunks`,
    which simulates a tampered ciphertext chunk and asserts the streaming client emits a `decrypt_failed` error event.
- Consider rate limiting and denial-of-service protections

## Technical Challenges

1. **Maintaining encryption context** across multiple stream chunks
2. **Handling connection interruptions** during streaming
3. **Ensuring chunk ordering** and handling out-of-order arrivals
4. **Managing memory efficiently** for long-running streams
5. **Balancing chunk size** for responsiveness vs. overhead

## Fallback Mechanisms

Implement graceful degradation when streaming fails:
- ✅ Fall back to non-streaming mode
- ✅ Cache partial responses for resumption (CryptoClient now surfaces
  `partial_response` events so UIs can render recovered text before
  non-streaming fallbacks complete)
- ✅ Provide clear error messaging to users (streaming helpers now expose user-facing messages)
