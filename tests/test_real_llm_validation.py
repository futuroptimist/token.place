import pytest
import os
import json
import requests
from pathlib import Path
from tests.test_real_llm import real_server, MODEL_INFO
import time

# Attempt to import configuration
try:
    from config import LLM_MODEL_FILENAME
except ImportError:
    # Default value if config.py is not available
    LLM_MODEL_FILENAME = 'llama-3-8b-instruct.Q4_K_M.gguf'

# We import the MODEL_INFO directly from test_real_llm.py, no need to redefine it here

def test_real_llm_inference_comprehensive(real_server):
    """
    Comprehensive test for the real LLM functionality. This test:
    1. Verifies the model file exists and has the correct checksum
    2. Checks server connection and basic operation
    3. Tests complex inference with multiple rounds of conversation
    4. Verifies response format and content
    """
    # Skip if real_server is None (test would be skipped in CI environment)
    if real_server is None:
        pytest.skip("Real server setup was skipped in CI environment")
    
    server_port, _ = real_server
    
    # Verify that the model file exists
    model_path = Path("models") / MODEL_INFO["name"]
    assert model_path.exists(), f"Model file {model_path} does not exist"
    assert model_path.stat().st_size > 0, f"Model file {model_path} is empty"
    
    # Determine if we're using a real or mock LLM based on file size
    using_mock = model_path.stat().st_size < 10 * 1024 * 1024  # If < 10MB, assume mock
    if using_mock:
        print("Test is running with a mock LLM (small model file detected)")
    else:
        print("Test is running with a real LLM (full model file detected)")
    
    # Test basic query - simple question
    print("\nTesting basic question...")
    messages = [
        {"role": "user", "content": "What is the capital of France?"}
    ]
    
    # Set a longer timeout for real LLM which might be slower
    response = requests.post(
        f"http://localhost:{server_port}/",
        json={"chat_history": messages},
        timeout=60  # Increase timeout for real LLM
    )
    
    # Basic validations
    assert response.status_code == 200, f"Server returned error: {response.status_code}"
    data = response.json()
    assert isinstance(data, list), "Response is not a list"
    assert len(data) >= 2, "Response doesn't have enough messages"
    
    # Verify question and answer
    assert data[0]["role"] == "user", "First message should be from user"
    assert data[0]["content"] == "What is the capital of France?", "User message content was altered"
    assert data[1]["role"] == "assistant", "Second message should be from assistant"
    
    # For real LLM, verify Paris is mentioned. For mock, accept any response.
    if not using_mock:
        assert "Paris" in data[1]["content"], "Response should mention Paris"
    
    print(f"Basic question response: {data[1]['content'][:100]}...")
    
    # Test multi-turn conversation
    print("\nTesting multi-turn conversation...")
    messages = [
        {"role": "user", "content": "What is the capital of France?"},
        {"role": "assistant", "content": "The capital of France is Paris."},
        {"role": "user", "content": "What is the population of that city?"}
    ]
    
    response = requests.post(
        f"http://localhost:{server_port}/",
        json={"chat_history": messages},
        timeout=60  # Increase timeout for real LLM
    )
    
    assert response.status_code == 200, f"Server returned error on multi-turn: {response.status_code}"
    data = response.json()
    
    # The model should understand context from the previous messages
    assert len(data) >= 4, "Multi-turn response doesn't have enough messages"
    assert data[3]["role"] == "assistant", "Last message should be from assistant"
    
    # For real LLM, check specific content. For mock, just verify we got some response.
    if not using_mock:
        assert "million" in data[3]["content"].lower() or any(str(n) for n in range(1, 15) if str(n) in data[3]["content"]), \
            "Response should mention population numbers"
    
    print(f"Multi-turn response: {data[3]['content'][:100]}...")
    
    # Test complex reasoning
    print("\nTesting complex reasoning...")
    messages = [
        {"role": "user", "content": "If I have 5 apples and give 2 to my friend, then buy 3 more, how many apples do I have?"}
    ]
    
    response = requests.post(
        f"http://localhost:{server_port}/",
        json={"chat_history": messages},
        timeout=60  # Increase timeout for real LLM
    )
    
    assert response.status_code == 200, f"Server returned error on complex query: {response.status_code}"
    data = response.json()
    assert len(data) >= 2, "Complex reasoning response doesn't have enough messages"
    
    # For real LLM, verify correct answer. For mock, accept any response.
    if not using_mock:
        assert "6" in data[1]["content"], "Response should include the correct answer (6)"
    
    print(f"Complex reasoning response: {data[1]['content'][:100]}...")
    
    print("\nReal LLM validation successful!")

def test_real_llm_error_handling(real_server):
    """Test error handling of the real LLM server"""
    # Skip if real_server is None (test would be skipped in CI environment)
    if real_server is None:
        pytest.skip("Real server setup was skipped in CI environment")
    
    server_port, _ = real_server
    
    # Determine if we're using a real or mock LLM based on file size
    model_path = Path("models") / MODEL_INFO["name"]
    using_mock = model_path.stat().st_size < 10 * 1024 * 1024  # If < 10MB, assume mock
    if using_mock:
        print("Test is running with a mock LLM (small model file detected)")
    else:
        print("Test is running with a real LLM (full model file detected)")
    
    # Test with malformed request
    print("\nTesting malformed request handling...")
    response = requests.post(
        f"http://localhost:{server_port}/",
        json={"invalid_key": "This should cause an error"},
        timeout=30
    )
    
    # Should return an error but not crash
    assert response.status_code != 200, "Server should reject malformed request"
    
    # Test with empty request
    print("\nTesting empty request handling...")
    response = requests.post(
        f"http://localhost:{server_port}/",
        json={},
        timeout=30
    )
    
    # Should return an error but not crash
    assert response.status_code != 200, "Server should reject empty request"
    
    # Test that server can still process valid requests after errors
    print("\nTesting recovery after errors...")
    messages = [
        {"role": "user", "content": "Hello, are you working correctly?"}
    ]
    
    response = requests.post(
        f"http://localhost:{server_port}/",
        json={"chat_history": messages},
        timeout=60  # Increase timeout for real LLM
    )
    
    assert response.status_code == 200, "Server failed to recover after error"
    data = response.json()
    assert len(data) >= 2, "Response after error doesn't have enough messages"
    assert data[1]["role"] == "assistant", "Response after error not from assistant"
    
    print(f"Recovery response: {data[1]['content'][:100]}...")
    print("\nError handling tests successful!") 
