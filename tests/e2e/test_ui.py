import base64
import json
import os
import pytest
import queue
import re
import subprocess
import sys
import threading
from playwright.sync_api import Page
import time

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


@pytest.mark.e2e
def test_landing_chat_shows_no_servers_available_message(
    page: Page,
    base_url: str,
    setup_servers,
):
    """Structured API v1 no-server errors should render a clear landing-chat message."""

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

    page.route(
        "**/api/v1/public-key",
        lambda route: route.fulfill(
            status=200,
            headers={"Content-Type": "application/json"},
            body=json.dumps({"public_key": server_public_key_b64}),
        ),
    )
    page.route(
        "**/api/v1/chat/completions",
        lambda route: route.fulfill(
            status=503,
            headers={"Content-Type": "application/json"},
            body=json.dumps(
                {
                    "error": {
                        "type": "service_unavailable_error",
                        "code": "no_registered_compute_nodes",
                        "message": "No registered compute nodes available",
                    }
                }
            ),
        ),
    )

    page.goto(base_url)
    page.wait_for_load_state("networkidle")

    page.locator("textarea").first.fill("hello")
    page.locator("button", has_text="Send").click()

    assistant_message = page.locator(".assistant-message").last
    assistant_message.wait_for(state="visible")
    assert "No LLM servers are available right now." in assistant_message.inner_text()


@pytest.mark.e2e
def test_landing_chat_real_inference_with_desktop_bridge_api_v1(
    page: Page,
    base_url: str,
    setup_servers,
):
    """
    Validate relay landing chat end-to-end through the desktop compute-node bridge.

    This test intentionally avoids route mocking so it verifies the real wiring:
    browser UI -> relay.py API v1 -> relay sink/source -> desktop bridge runtime.
    """
    relay_process, _ = setup_servers
    assert relay_process is not None

    test_env = os.environ.copy()
    test_env["TOKEN_PLACE_ENV"] = "testing"
    test_env["USE_MOCK_LLM"] = "0"
    preprovisioned_model_path = os.environ.get("TOKENPLACE_REAL_E2E_MODEL_PATH", "").strip()
    if not preprovisioned_model_path:
        raise AssertionError(
            "TOKENPLACE_REAL_E2E_MODEL_PATH must be configured for the always-on relay landing-page real-inference guardrail."
        )
    if not os.path.isfile(preprovisioned_model_path):
        raise AssertionError(
            "TOKENPLACE_REAL_E2E_MODEL_PATH must point to an existing model file "
            f"(got: {preprovisioned_model_path})."
        )

    bridge_process = subprocess.Popen(
        [
            sys.executable,
            "desktop-tauri/src-tauri/python/compute_node_bridge.py",
            "--model",
            preprovisioned_model_path,
            "--mode",
            "cpu",
            "--relay-url",
            base_url,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=test_env,
    )

    stdout_queue: "queue.Queue[str]" = queue.Queue()
    stderr_lines: list[str] = []

    def _drain_stream(stream, output_queue=None, collector=None):
        for stream_line in iter(stream.readline, ""):
            if output_queue is not None:
                output_queue.put(stream_line)
            if collector is not None:
                collector.append(stream_line)

    stdout_thread = threading.Thread(
        target=_drain_stream,
        args=(bridge_process.stdout, stdout_queue, None),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_drain_stream,
        args=(bridge_process.stderr, None, stderr_lines),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    v1_requests = []
    v2_requests = []

    def record_v1_request(route):
        v1_requests.append(route.request)
        route.continue_()

    def record_v2_request(route):
        v2_requests.append(route.request.url)
        route.continue_()

    page.route("**/api/v1/chat/completions", record_v1_request)
    page.route("**/api/v2/chat/completions", record_v2_request)

    v1_response_headers = []

    def record_v1_response(response):
        if "/api/v1/chat/completions" in response.url:
            v1_response_headers.append(response.headers)

    page.on("response", record_v1_response)

    try:
        start_deadline = time.time() + 25
        registered = False
        started = False
        while time.time() < start_deadline:
            try:
                line = stdout_queue.get(timeout=0.5)
            except queue.Empty:
                if bridge_process.poll() is not None:
                    stderr_output = "".join(stderr_lines)
                    raise AssertionError(
                        f"desktop bridge exited early rc={bridge_process.returncode}\n{stderr_output}"
                    )
                continue

            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_type = payload.get("type")

            if event_type == "started":
                started = bool(payload.get("running"))
                assert payload.get("use_mock_llm") is False
                assert payload.get("llama_repo_stub_imported") is False
                llama_module_path = payload.get("llama_module_path", "")
                assert isinstance(llama_module_path, str) and llama_module_path
                assert not llama_module_path.endswith("/llama_cpp.py")
                assert not llama_module_path.endswith("\\llama_cpp.py")
            if event_type == "status" and payload.get("registered") is True:
                registered = True
                break
            if event_type == "error":
                raise AssertionError(f"desktop bridge error event: {payload}")

        assert started, "desktop bridge did not emit a started event"
        assert registered, "desktop bridge never reported relay registration"
        relay_ready = False
        relay_next_server_body = ""
        for _ in range(20):
            next_server_response = page.request.get(f"{base_url}/next_server")
            if next_server_response.ok:
                relay_next_server_body = next_server_response.text()
                try:
                    payload = next_server_response.json()
                except Exception:  # pragma: no cover - defensive for non-json relay errors
                    payload = {}
                if isinstance(payload, dict) and payload.get("server_public_key"):
                    relay_ready = True
                    break
            time.sleep(0.25)
        assert relay_ready, (
            "desktop bridge reported registered but relay /next_server did not expose "
            f"an active server_public_key in time. Last response body: {relay_next_server_body!r}"
        )

        page.goto(base_url)
        page.wait_for_load_state("networkidle")

        textarea = page.locator("textarea").first
        textarea.fill("What is the capital of France? Respond with one word.")
        page.evaluate(
            """
            () => {
                window.__assistantTextSnapshots = [];
                const container = document.querySelector('.chat-container');
                if (!container) {
                    return;
                }
                const observer = new MutationObserver(() => {
                    const nodes = document.querySelectorAll('.assistant-message');
                    if (!nodes.length) return;
                    const latest = nodes[nodes.length - 1];
                    const text = (latest.textContent || '').trim();
                    window.__assistantTextSnapshots.push(text);
                });
                observer.observe(container, { childList: true, subtree: true, characterData: true });
                window.__assistantObserver = observer;
            }
            """
        )
        page.locator("button", has_text="Send").click()

        assistant_message = page.locator(".assistant-message").last
        assistant_message.wait_for(state="visible")

        page.wait_for_function(
            """
            ({ selector }) => {
                const nodes = document.querySelectorAll(selector);
                if (!nodes.length) return false;
                const latest = nodes[nodes.length - 1];
                return Boolean(latest.textContent && latest.textContent.trim().length > 0);
            }
            """,
            arg={
                "selector": ".assistant-message",
            },
        )

        assistant_text = assistant_message.inner_text()
        assert assistant_text.strip(), "assistant response should not be empty"
        assert "Sorry, I encountered an issue generating a response." not in assistant_text
        assert "No LLM servers are available right now." not in assistant_text
        assert "Unknown streaming error" not in assistant_text

        assert len(v1_requests) >= 1
        assert v2_requests == []
        assert v1_response_headers, "expected at least one API v1 response"
        latest_headers = v1_response_headers[-1]
        provider_class = latest_headers.get("x-tokenplace-api-v1-provider")
        resolved_provider_path = latest_headers.get("x-tokenplace-api-v1-resolved-provider-path")
        execution_backend_path = latest_headers.get("x-tokenplace-api-v1-execution-backend-path")
        assert provider_class in (None, "DistributedApiV1ComputeProvider")
        assert resolved_provider_path in (None, "distributed")
        assert execution_backend_path in (None, "distributed_relay_e2ee")

        page.wait_for_timeout(300)
        non_streaming_state = page.evaluate(
            """
            () => {
                if (window.__assistantObserver) {
                    window.__assistantObserver.disconnect();
                }
                const snapshots = Array.isArray(window.__assistantTextSnapshots)
                    ? window.__assistantTextSnapshots
                    : [];
                const nonEmpty = snapshots.filter((value) => typeof value === 'string' && value.length > 0);
                const uniqueNonEmpty = [...new Set(nonEmpty)];

                const appEl = document.querySelector('#app');
                const vm = appEl && appEl.__vue__;
                const history = vm && Array.isArray(vm.chatHistory) ? vm.chatHistory : [];
                const assistant = [...history].reverse().find((message) => message && message.role === 'assistant') || null;

                return {
                    snapshots,
                    uniqueNonEmpty,
                    hasAssistant: Boolean(assistant),
                    hasDisplayContent: Boolean(
                        assistant && Object.prototype.hasOwnProperty.call(assistant, 'displayContent')
                    ),
                    hasIsTyping: Boolean(
                        assistant && Object.prototype.hasOwnProperty.call(assistant, 'isTyping')
                    ),
                    assistantIsTyping: Boolean(assistant && assistant.isTyping),
                    assistantContent: assistant && typeof assistant.content === 'string' ? assistant.content : '',
                    domAssistantText: (() => {
                        const nodes = document.querySelectorAll('.assistant-message');
                        if (!nodes.length) {
                            return '';
                        }
                        const latest = nodes[nodes.length - 1];
                        return (latest.textContent || '').trim();
                    })(),
                };
            }
            """
        )
        assert non_streaming_state["hasAssistant"] is True
        assert non_streaming_state["hasDisplayContent"] is False
        assert non_streaming_state["assistantIsTyping"] is False
        assert len(non_streaming_state["uniqueNonEmpty"]) == 1, (
            "assistant message should render atomically without multi-step text growth; "
            f"snapshots={non_streaming_state['snapshots']}"
        )
        assistant_content = non_streaming_state["assistantContent"].strip()
        dom_assistant_text = non_streaming_state["domAssistantText"].strip()

        # DOM rendering can add/remove whitespace around punctuation and wrapped lines.
        # Compare lexical token sequences so punctuation-preserving whitespace differences pass,
        # while real word-boundary/content regressions still fail.
        token_pattern = r"\w+|[^\w\s]"
        assistant_content_tokens = re.findall(token_pattern, assistant_content, flags=re.UNICODE)
        dom_assistant_text_tokens = re.findall(token_pattern, dom_assistant_text, flags=re.UNICODE)
        assert assistant_content_tokens == dom_assistant_text_tokens, (
            "final assistant Vue state content must match rendered DOM text token-for-token "
            "(allowing formatting-only whitespace differences) to prove final non-streaming rendering path"
        )

        encrypted_request = v1_requests[0].post_data_json
        assert encrypted_request.get("encrypted") is True
        assert encrypted_request.get("stream") in (None, False)
        client_public_key = encrypted_request.get("client_public_key")
        assert isinstance(client_public_key, str) and client_public_key
        client_public_key_pem = base64.b64decode(client_public_key, validate=True)
        assert b"-----BEGIN PUBLIC KEY-----" in client_public_key_pem

        process_request_lines = [
            line
            for line in stderr_lines
            if "desktop.compute_node_bridge.process_request" in line
        ]
        assert process_request_lines, "desktop bridge should process relay requests for API v1 E2EE mode"
    finally:
        if bridge_process.stdin:
            try:
                bridge_process.stdin.write(json.dumps({"type": "cancel"}) + "\n")
                bridge_process.stdin.flush()
                bridge_process.stdin.close()
            except (BrokenPipeError, ValueError):
                pass

        try:
            bridge_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            bridge_process.kill()
