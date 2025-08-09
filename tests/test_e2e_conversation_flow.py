import pytest
import os
import json
import time
import subprocess
import signal
from contextlib import contextmanager
import requests
from typing import Generator, List

from tests.utils.client_simulator import ClientSimulator

# Check if we should run with the mock LLM to avoid downloading large models
USE_MOCK_LLM = os.environ.get('USE_MOCK_LLM', '1') == '1'  # Default to mock mode for tests

@contextmanager
def start_server(use_mock_llm: bool = True) -> Generator[None, None, None]:
    """
    Start the server process for testing.
    
    Args:
        use_mock_llm: Whether to use the mock LLM
        
    Yields:
        None
    """
    # Set up environment for server
    env = os.environ.copy()
    if use_mock_llm:
        env['USE_MOCK_LLM'] = '1'
    
    # Start the server process
    server_cmd = ["python", "server.py", "--server_port", "3000", "--relay_port", "5000"]
    if use_mock_llm:
        server_cmd.append("--use_mock_llm")
    
    process = subprocess.Popen(
        server_cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    
    try:
        # Wait for server to start
        time.sleep(2)  # Allow server time to initialize
        
        # Check if server is running
        health_check_url = "http://localhost:3000/health"
        retries = 5
        while retries > 0:
            try:
                response = requests.get(health_check_url)
                if response.status_code == 200:
                    break
            except requests.RequestException:
                pass
            
            time.sleep(1)
            retries -= 1
            
        if retries == 0:
            raise RuntimeError("Server failed to start")
        
        # Server is ready
        yield
    finally:
        # Clean up
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

@contextmanager
def start_relay() -> Generator[None, None, None]:
    """
    Start the relay process for testing.
    
    Yields:
        None
    """
    # Start the relay process
    relay_cmd = ["python", "relay.py", "--port", "5000"]
    env = os.environ.copy()
    env["USE_MOCK_LLM"] = "1"
    process = subprocess.Popen(
        relay_cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    
    try:
        # Wait for relay to start
        time.sleep(2)  # Allow relay time to initialize
        
        # Check if relay is running
        health_check_url = "http://localhost:5000/"
        retries = 5
        while retries > 0:
            try:
                response = requests.get(health_check_url)
                if response.status_code == 200:
                    break
            except requests.RequestException:
                pass
            
            time.sleep(1)
            retries -= 1
            
        if retries == 0:
            raise RuntimeError("Relay failed to start")
        
        # Relay is ready
        yield
    finally:
        # Clean up
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

@pytest.mark.e2e
def test_complete_encrypted_conversation_flow():
    """Test the complete end-to-end encrypted conversation flow."""
    # Start the server and relay
    with start_relay(), start_server(use_mock_llm=USE_MOCK_LLM):
        # Initialize client simulator
        client = ClientSimulator(base_url="http://localhost:5000")
        
        # Get server public key
        server_key = client.fetch_server_public_key()
        assert server_key is not None, "Failed to fetch server public key"
        
        # Test a simple message exchange
        message = "Hello, secure world!"
        # Send request and decrypt response using high-level helper
        decrypted_response = client.send_message([{"role": "user", "content": message}])
        
        # Verify response content
        assert decrypted_response, "Empty response from server"
        assert isinstance(decrypted_response, str), "Response is not a string"
        
        # A more complete test with multiple messages
        conversation = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What's the capital of France?"},
        ]
        
        # Use the high-level send_message method for this test
        response_text = client.send_message(conversation)
        
        # Verify response content
        assert "paris" in response_text.lower(), "Expected 'paris' in response to capital question"

@pytest.mark.e2e
def test_conversation_context_maintenance():
    """Test that conversation context is maintained across multiple exchanges."""
    # Start the server and relay
    with start_relay(), start_server(use_mock_llm=USE_MOCK_LLM):
        # Initialize client simulator
        client = ClientSimulator(base_url="http://localhost:5000")
        
        # Initial conversation
        conversation = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "My name is Alice."},
        ]
        
        # First exchange
        response1 = client.send_message(conversation)
        assert response1, "Empty response from server"
        
        # Add the assistant's response and a follow-up question
        conversation.append({"role": "assistant", "content": response1})
        conversation.append({"role": "user", "content": "What did I tell you my name was?"})
        
        # Second exchange
        response2 = client.send_message(conversation)
        assert response2, "Empty follow-up response from server"

@pytest.mark.e2e
def test_encryption_decryption_integrity():
    """Test that encryption and decryption preserve message integrity."""
    # Start the server and relay
    with start_relay(), start_server(use_mock_llm=USE_MOCK_LLM):
        # Initialize client simulator
        client = ClientSimulator(base_url="http://localhost:5000")
        
        # Create a complex message with special characters
        complex_message = {
            "role": "user", 
            "content": """Test message with special chars: !@#$%^&*()_+
            And multiple lines
            And emojis: 😀🔒🔑
            And JSON-like content: {"key": "value"}"""
        }
        
        # Send and receive the message
        response = client.send_message([complex_message])
        
        # Verify the response exists and is non-empty
        assert response, "Empty response from server"
        assert len(response) > 10, "Response suspiciously short"

if __name__ == "__main__":
    # Run the tests directly if executed as a script
    test_complete_encrypted_conversation_flow()
    test_conversation_context_maintenance()
    test_encryption_decryption_integrity() 
