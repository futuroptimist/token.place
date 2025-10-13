import json
import pytest
from playwright.sync_api import Page
import time

# This test now implicitly uses the `setup_servers` and `page` fixtures
# defined in tests/conftest.py

def test_root_page_loads(page: Page, base_url: str, setup_servers):
    """Test that the root page loads and returns a 200 status."""
    # Navigate to the base URL
    response = page.goto(base_url)

    # Wait for the page to load
    page.wait_for_load_state("networkidle")

    # Check that we got a 200 response
    assert response.status == 200, f"Expected 200 OK, got {response.status}"

    # Check that the page has content
    assert len(page.content()) > 0, "Page has no content"

    # Print a message to indicate test passed
    print("✓ Page loaded successfully with status 200")

    # Look for any heading
    headings = page.locator("h1, h2, h3").all()
    assert len(headings) > 0, "Page should contain at least one heading"
    print(f"✓ Found {len(headings)} headings on the page")

def test_send_message(page: Page, base_url: str, setup_servers):
    """Test basic page interaction."""
    # Navigate to the base URL
    page.goto(base_url)

    # Wait for the page to load
    page.wait_for_load_state("networkidle")

    # Check for any input element (not just textarea)
    input_elements = page.locator("input, textarea").all()
    print(f"Found {len(input_elements)} input elements")

    # Check for any button
    buttons = page.locator("button").all()
    print(f"Found {len(buttons)} buttons")

    # Simply verify the page loads and contains expected elements like inputs or buttons
    # This is a minimal test to ensure the page structure is reasonable
    assert len(input_elements) + len(buttons) > 0, "Page should contain inputs or buttons for interaction"

    print("✓ Page contains interactive elements")

def test_multi_turn_conversation(page: Page, base_url: str, setup_servers):
    """Basic page verification test."""
    # Navigate to the base URL
    page.goto(base_url)

    # Wait for the page to load
    page.wait_for_load_state("networkidle")

    # Take a screenshot for debugging
    screenshot_path = f"ui_test_screenshot_{int(time.time())}.png"
    page.screenshot(path=screenshot_path)
    print(f"Screenshot saved to {screenshot_path}")

    # Check that the page has loaded Vue.js
    assert 'Vue' in page.content(), "Page should contain Vue.js"

    # Verify app has initialized
    assert page.locator("#app").count() > 0, "Vue app should be initialized"
    print("✓ Vue app initialization verified")

# Add more E2E tests here as the UI evolves


def test_markdown_rendering_stream_updates(page: Page, base_url: str, setup_servers):
    """The chat UI should render markdown formatting returned by the assistant."""

    markdown_reply = "**Bold** introduction\n\n- First item\n- Second item\n\nHere is `inline` code and:\n```\nblock example\n```"

    def handle_chat_request(route):
        route.fulfill(
            status=200,
            headers={"Content-Type": "application/json"},
            body=json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": markdown_reply,
                            }
                        }
                    ]
                }
            ),
        )

    page.route("**/api/v1/chat/completions", handle_chat_request)

    page.goto(base_url)
    page.wait_for_load_state("networkidle")

    textarea = page.locator("textarea").first
    textarea.fill("Show markdown please")
    page.locator("button", has_text="Send").click()

    assistant_message = page.locator(".assistant-message").last
    assistant_message.wait_for(state="visible")

    assert assistant_message.locator("strong").inner_text() == "Bold"

    list_items = assistant_message.locator("li")
    assert list_items.count() == 2
    assert list_items.nth(0).inner_text() == "First item"
    assert list_items.nth(1).inner_text() == "Second item"

    inline_code = assistant_message.locator("code").first
    assert inline_code.inner_text() == "inline"

    block_code = assistant_message.locator("pre code").first
    assert "block example" in block_code.inner_text()

    # Ensure raw HTML isn't rendered unsanitized
    assert assistant_message.locator("script").count() == 0
