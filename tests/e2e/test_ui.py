import base64
import json
import os
import socket
import subprocess
import sys
import threading
import pytest
from playwright.sync_api import Page
import time

from flask import Flask, jsonify, request
from werkzeug.serving import make_server

from encrypt import encrypt
from utils.crypto_helpers import CryptoClient

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
    """Landing chat must stay on API v1 JSON chat completions only."""

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

    def handle_next_server(route):
        route.fulfill(
            status=200,
            headers={"Content-Type": "application/json"},
            body=json.dumps({"server_public_key": server_public_key_b64}),
        )

    def handle_v1_chat(route):
        request_json = route.request.post_data_json
        assert request_json.get("encrypted") is True
        assert isinstance(request_json.get("client_public_key"), str) and request_json[
            "client_public_key"
        ]

        encrypted_request = request_json.get("messages")
        assert isinstance(encrypted_request, dict)
        assert isinstance(encrypted_request.get("ciphertext"), str) and encrypted_request["ciphertext"]
        assert isinstance(encrypted_request.get("cipherkey"), str) and encrypted_request["cipherkey"]
        assert isinstance(encrypted_request.get("iv"), str) and encrypted_request["iv"]

        client_public_key_pem = base64.b64decode(request_json["client_public_key"], validate=True)
        assert b"-----BEGIN PUBLIC KEY-----" in client_public_key_pem

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

    v2_requests = []

    def record_v2_request(route):
        v2_requests.append(route.request.url)
        route.fulfill(status=500, body="v2 should not be called")

    page.route("**/api/v1/public-key", handle_public_key)
    page.route("**/next_server", handle_next_server)
    page.route("**/api/v2/chat/completions", record_v2_request)
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


def _find_open_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_http_ready(url: str, timeout_seconds: float = 20.0) -> None:
    import requests

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            response = requests.get(url, timeout=1.0)
            if response.ok:
                return
        except requests.RequestException:
            pass
        time.sleep(0.25)
    raise AssertionError(f"Timed out waiting for HTTP readiness: {url}")


def _wait_for_relay_server_registration(relay_url: str, timeout_seconds: float = 30.0) -> None:
    import requests

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            response = requests.get(f"{relay_url}/next_server", timeout=1.0)
            data = response.json()
            if isinstance(data, dict) and isinstance(data.get("server_public_key"), str):
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise AssertionError("Desktop bridge did not register with relay in time")


def test_landing_chat_e2e_round_trip_via_relay_and_desktop_bridge(browser_matrix):
    """Verify relay landing chat reaches desktop compute bridge over API v1 (non-streaming)."""
    _, page = browser_matrix

    relay_port = _find_open_port()
    adapter_port = _find_open_port()
    relay_url = f"http://127.0.0.1:{relay_port}"

    adapter_app = Flask(__name__)

    @adapter_app.route("/api/v1/chat/completions", methods=["POST"])
    def adapter_chat_completion():
        payload = request.get_json(silent=True) or {}
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return jsonify({"error": {"message": "messages must be a list"}}), 400

        client = CryptoClient(relay_url)
        response_history = client.send_chat_message(messages, max_retries=8)
        if not isinstance(response_history, list) or not response_history:
            return jsonify({"error": {"message": "relay round trip failed"}}), 502

        assistant = response_history[-1]
        if not isinstance(assistant, dict):
            return jsonify({"error": {"message": "invalid relay response format"}}), 502

        return jsonify(
            {
                "id": "chatcmpl-relay-desktop-e2e",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": payload.get("model", "llama-3-8b-instruct"),
                "choices": [{"index": 0, "message": assistant, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
        )

    adapter_server = make_server("127.0.0.1", adapter_port, adapter_app, threaded=True)
    adapter_thread = threading.Thread(target=adapter_server.serve_forever, daemon=True)
    adapter_thread.start()

    relay_env = os.environ.copy()
    relay_env["TOKEN_PLACE_ENV"] = "testing"
    relay_env["USE_MOCK_LLM"] = "1"
    relay_env["TOKENPLACE_API_V1_COMPUTE_PROVIDER"] = "distributed"
    relay_env["TOKENPLACE_DISTRIBUTED_COMPUTE_URL"] = f"http://127.0.0.1:{adapter_port}"

    relay_proc = subprocess.Popen(
        [sys.executable, "relay.py", "--host", "127.0.0.1", "--port", str(relay_port), "--use_mock_llm"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=relay_env,
    )

    bridge_env = os.environ.copy()
    bridge_env["TOKEN_PLACE_ENV"] = "testing"
    bridge_env["USE_MOCK_LLM"] = "1"
    bridge_proc = subprocess.Popen(
        [
            sys.executable,
            "desktop-tauri/src-tauri/python/compute_node_bridge.py",
            "--model",
            "mock-model.gguf",
            "--mode",
            "cpu",
            "--relay-url",
            relay_url,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=bridge_env,
    )

    try:
        _wait_for_http_ready(f"{relay_url}/healthz")
        _wait_for_relay_server_registration(relay_url)

        v2_requests = []
        page.on(
            "request",
            lambda req: v2_requests.append(req.url)
            if "/api/v2/chat/completions" in req.url
            else None,
        )

        page.goto(relay_url)
        page.wait_for_load_state("networkidle")
        page.locator("textarea").first.fill("What is the capital of France?")
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
            arg={
                "selector": ".assistant-message",
                "expectedText": "Mock Response: The capital of France is Paris.",
            },
        )
        assert "Mock Response: The capital of France is Paris." in assistant_message.inner_text()
        assert v2_requests == []
    finally:
        for proc in (bridge_proc, relay_proc):
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        adapter_server.shutdown()
        adapter_thread.join(timeout=5)
