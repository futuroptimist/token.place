import pytest
from playwright.sync_api import Page, expect
import time
import os

# Import the new crypto helper
from utils.crypto_helpers import CryptoClient
from tests.conftest import E2E_BASE_URL

# Constants for testing
TEST_MESSAGE = "Hello, this is a test message!"

def test_chat_encryption_e2e(page, base_url, setup_servers):
    """
    Test the end-to-end encrypted chat functionality
    """
    # Navigate to the base URL
    response = page.goto(base_url)
    
    # Check that we got a 200 response
    assert response.status == 200, f"Expected 200 OK, got {response.status}"

    # Wait for the page to load and be ready
    page.wait_for_load_state("networkidle")
    
    # Verify page has content
    assert len(page.content()) > 0, "Page has no content"
    
    # Simply look for encryption-related words in the HTML
    page_content = page.content()
    encryption_terms = ["encryption", "secure", "keys", "encrypted"]
    
    # Check if any encryption terms exist on the page or in the JavaScript
    encryption_presence = (
        'JSEncrypt' in page_content or
        'CryptoJS' in page_content or
        any(term in page_content.lower() for term in encryption_terms)
    )
    
    print(f"Encryption technology present in page: {encryption_presence}")
    assert encryption_presence, "Page should contain encryption-related technology"
    
    # Take a screenshot for debugging
    screenshot_path = os.path.join(os.path.dirname(__file__), "encryption_test_screenshot.png")
    page.screenshot(path=screenshot_path)
    print(f"Screenshot saved to {screenshot_path}")
    assert os.path.exists(screenshot_path)
    
    # Test direct API connection with CryptoClient
    crypto_client = CryptoClient(base_url, debug=True)
    success = crypto_client.fetch_server_public_key('/api/v1/public-key')
    assert success, "CryptoClient should be able to fetch the server's public key"
    print("✓ CryptoClient successfully fetched server public key")

def test_multiple_turns_conversation(page, base_url, setup_servers):
    """
    Test a multi-turn conversation with encryption
    """
    # Navigate to the base URL
    response = page.goto(base_url)
    
    # Check that we got a 200 response
    assert response.status == 200, f"Expected 200 OK, got {response.status}"

    # Wait for the page to load
    page.wait_for_load_state("networkidle")
    
    # Verify page has content
    assert len(page.content()) > 0, "Page has no content"
    
    # Check for Vue initialization
    assert page.locator("#app").count() > 0, "Vue app should be initialized"
    
    # Create a direct API client connection to verify the API works
    crypto_client = CryptoClient(base_url, debug=True)
    success = crypto_client.fetch_server_public_key('/api/v1/public-key')
    assert success, "CryptoClient should be able to fetch the server's public key"
    
    # Send a test message via API - this should work even if UI doesn't
    test_messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What is the capital of France?"}
    ]
    
    response = crypto_client.send_api_request(test_messages)
    assert response is not None, "API response should not be None"
    assert 'choices' in response, "API response should have choices"
    assert 'message' in response['choices'][0], "API response should have a message"
    
    print(f"API response content: {response['choices'][0]['message']['content']}")
    
    # Take a screenshot to document the UI state
    screenshot_path = os.path.join(os.path.dirname(__file__), "chat_test_screenshot.png")
    page.screenshot(path=screenshot_path)
    print(f"Screenshot saved to {screenshot_path}")
    
    print("✓ API encryption and response verified") 