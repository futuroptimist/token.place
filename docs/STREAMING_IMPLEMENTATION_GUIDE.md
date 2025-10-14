# Streaming Implementation Guide for token.place

This guide outlines the steps required to implement streaming inference in the token.place application, allowing users to see responses as they are generated.

> **Status update (2025-09-30):** `/api/v2/chat/completions` now supports Server-Sent Events when the `stream` flag is provided. The Flask route sends role, content, and completion markers so plaintext clients receive incremental updates without waiting for the full JSON payload. The remaining sections continue to track work for true token-by-token generation, relay integration, and encrypted streaming.

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
- Create a new encryption function for streams that maintains session keys
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
- Implement a new endpoint `/stream-source` in the relay server
- Modify the polling mechanism to handle streaming responses
- Store streaming state to track active streams

### 2. Client-side Changes

#### 2.1 Streaming API Client
- ✅ Implemented streaming client chunk processing via `CryptoClient.stream_chat_completion`
- Add support for handling and decrypting streaming data
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
- Implement a typing effect for a better user experience
- ✅ Added support for rendering markdown/formatting as it arrives via a sanitized renderer in the chat UI

### 3. API Endpoints

#### 3.1 Add Streaming Endpoints
- Create new endpoints specifically for streaming:
  - `/api/v1/chat/completions/stream`
  - `/api/v1/completions/stream`

#### 3.2 Update API Documentation
- ✅ README now includes a "streaming usage" section with SSE examples and `stream=true`
  guidance for both `/api/v1` and `/v1` aliases.

### 4. Testing Infrastructure

#### 4.1 Streaming Tests
- Implement tests for streaming functionality
- Test different chunk sizes and timing scenarios
- Verify encryption/decryption with streaming

#### 4.2 Integration Tests
- Create end-to-end tests for the streaming experience
- Test reconnection and error handling

## Implementation Phases

1. **Phase 1**: Server-side streaming without encryption
2. **Phase 2**: Encryption for streaming content
3. **Phase 3**: Client-side processing and UI updates
4. **Phase 4**: Full integration and testing
5. **Phase 5**: Performance optimization

## Security Considerations

- Ensure each chunk is properly encrypted
- Implement reconnection logic that maintains encryption state
- Verify that partial responses cannot be intercepted or modified
- Consider rate limiting and denial-of-service protections

## Technical Challenges

1. **Maintaining encryption context** across multiple stream chunks
2. **Handling connection interruptions** during streaming
3. **Ensuring chunk ordering** and handling out-of-order arrivals
4. **Managing memory efficiently** for long-running streams
5. **Balancing chunk size** for responsiveness vs. overhead

## Fallback Mechanisms

Implement graceful degradation when streaming fails:
- Fall back to non-streaming mode
- Cache partial responses for resumption
- Provide clear error messaging to users
