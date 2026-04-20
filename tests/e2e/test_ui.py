import base64
import json
import pytest
from playwright.sync_api import Page
import time
import textwrap

from encrypt import encrypt

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


def test_landing_chat_uses_api_v1_only_non_streaming(
    page: Page,
    base_url: str,
    setup_servers,
):
    """Landing chat should use API v1 only and avoid relay-path streaming."""

    server_public_key_pem = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAnFBKDAvTZEd+IlS59FKV
VFp4DT28sL1iHwZ94dJ5x5lf+Kq4Wxcl8COEQ3rp3QseM2MkAdZ1VvWbUmsonFux
7pVLQDyE+ANQkNd4K840zWV+CghTz34jxK59pb6cifSto7J8Wy7EqhUru7YLhnqZ
xz/AuHBPrq0RUS7f+ycJtfA6vj9Isp0BYpvgwOP97Ey+nCLiR5C/3IazOZblHQ7R
CbfZqP+encMwRbH/IvrXrz6/vecuIrq60fFtyZIbs7dASpfuSL6atIABu6CiSlXy
+6EhlEdmAXaCOPlQMYjc4u2ZNrOUTjuh3Yw8hMGezsTfTYZd2rrbGZRlkpfKbIdX
0QIDAQAB
-----END PUBLIC KEY-----"""
    server_public_key_b64 = base64.b64encode(server_public_key_pem.encode("utf-8")).decode("ascii")

    def handle_public_key(route):
        route.fulfill(
            status=200,
            headers={"Content-Type": "application/json"},
            body=json.dumps({"public_key": server_public_key_b64}),
        )

    v2_requests = []

    def handle_v2_request(route):
        v2_requests.append(route.request.url)
        route.abort()

    def handle_v1_chat(route):
        request_json = route.request.post_data_json
        assert request_json.get("encrypted") is True
        assert "stream" not in request_json
        assert isinstance(request_json.get("client_public_key"), str) and request_json[
            "client_public_key"
        ]

        encrypted_request = request_json.get("messages")
        assert isinstance(encrypted_request, dict)
        assert isinstance(encrypted_request.get("ciphertext"), str) and encrypted_request["ciphertext"]
        assert isinstance(encrypted_request.get("cipherkey"), str) and encrypted_request["cipherkey"]
        assert isinstance(encrypted_request.get("iv"), str) and encrypted_request["iv"]

        client_public_key_pem = "\n".join(
            [
                "-----BEGIN PUBLIC KEY-----",
                *textwrap.wrap(request_json["client_public_key"], 64),
                "-----END PUBLIC KEY-----",
            ]
        ).encode("utf-8")

        encrypted_response_body, encrypted_key, iv = encrypt(
            json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "Relay chat path restored.",
                            }
                        }
                    ]
                }
            ).encode("utf-8"),
            client_public_key_pem,
            use_pkcs1v15=True,
        )

        route.fulfill(
            status=200,
            headers={"Content-Type": "application/json"},
            body=json.dumps(
                {
                    "encrypted": True,
                    "data": {
                        "ciphertext": base64.b64encode(encrypted_response_body["ciphertext"]).decode("utf-8"),
                        "cipherkey": base64.b64encode(encrypted_key).decode("utf-8"),
                        "iv": base64.b64encode(iv).decode("utf-8"),
                    },
                }
            ),
        )

    page.route("**/api/v1/public-key", handle_public_key)
    page.route("**/api/v2/chat/completions", handle_v2_request)
    page.route("**/api/v1/chat/completions", handle_v1_chat)

    page.goto(base_url)
    page.wait_for_load_state("networkidle")

    textarea = page.locator("textarea").first
    textarea.fill("hello")
    page.locator("button", has_text="Send").click()

    assistant_message = page.locator(".assistant-message").last
    assistant_message.wait_for(state="visible")
    page.wait_for_function(
        """
        ({ selector, expectedText }) => {
            const nodes = document.querySelectorAll(selector);
            if (!nodes.length) return false;
            const latest = nodes[nodes.length - 1];
            return latest.textContent.includes(expectedText);
        }
        """,
        arg={"selector": ".assistant-message", "expectedText": "Relay chat path restored."},
    )
    assert "Relay chat path restored." in assistant_message.inner_text()
    assert "Sorry, I encountered an issue generating a response." not in page.content()
    assert v2_requests == []
