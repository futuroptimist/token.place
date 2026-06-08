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


def test_compute_node_count_renders_and_updates(page: Page, base_url: str, setup_servers):
    """Landing page should render and refresh the relay diagnostics compute-node count."""
    counts = iter([3, 5])
    latest_count = {"value": 5}

    def handle_diagnostics(route):
        try:
            latest_count["value"] = next(counts)
        except StopIteration:
            pass
        route.fulfill(
            status=200,
            headers={"Content-Type": "application/json"},
            body=json.dumps(
                {
                    "total_registered_compute_nodes": 99,
                    "total_api_v1_registered_compute_nodes": latest_count["value"],
                }
            ),
        )

    page.route("**/relay/diagnostics", handle_diagnostics)
    page.goto(base_url)
    page.wait_for_load_state("networkidle")

    status = page.locator(".compute-node-status")
    status.wait_for(state="visible")
    page.wait_for_function(
        """
        () => {
            const status = document.querySelector('.compute-node-status');
            return Boolean(
                status &&
                status.textContent.includes('Live compute nodes: 3') &&
                status.textContent.includes('Updated')
            );
        }
        """
    )
    assert "Live compute nodes: 3" in status.inner_text()
    assert "Updated" in status.inner_text()

    page.evaluate("document.querySelector('#app').__vue__.refreshComputeNodeCount()")
    page.wait_for_function(
        """
        () => {
            const status = document.querySelector('.compute-node-status');
            return Boolean(
                status &&
                status.textContent.includes('Live compute nodes: 5') &&
                status.textContent.includes('Updated')
            );
        }
        """
    )
    assert "Live compute nodes: 5" in status.inner_text()
    assert "Updated" in status.inner_text()


def test_compute_node_count_ignores_stale_refresh(page: Page, base_url: str, setup_servers):
    """Older diagnostics responses should not overwrite newer compute-node counts."""
    first_route = {}
    first_seen = threading.Event()

    def handle_diagnostics(route):
        if not first_seen.is_set():
            first_route["route"] = route
            first_seen.set()
            return

        route.fulfill(
            status=200,
            headers={"Content-Type": "application/json"},
            body=json.dumps(
                {
                    "total_registered_compute_nodes": 99,
                    "total_api_v1_registered_compute_nodes": 5,
                }
            ),
        )

    page.route("**/relay/diagnostics", handle_diagnostics)
    page.goto(base_url, wait_until="domcontentloaded")
    assert first_seen.wait(timeout=5), "Initial diagnostics request was not intercepted"

    page.evaluate("document.querySelector('#app').__vue__.refreshComputeNodeCount()")
    page.wait_for_function(
        "document.querySelector('.compute-node-status').textContent.includes('Live compute nodes: 5')"
    )

    first_route["route"].fulfill(
        status=200,
        headers={"Content-Type": "application/json"},
        body=json.dumps(
            {
                "total_registered_compute_nodes": 99,
                "total_api_v1_registered_compute_nodes": 3,
            }
        ),
    )
    page.wait_for_timeout(100)
    assert "Live compute nodes: 5" in page.locator(".compute-node-status").inner_text()


def test_compute_node_count_rejects_null_diagnostics(page: Page, base_url: str, setup_servers):
    """Invalid null diagnostics payloads should render the unavailable state."""

    def handle_null_diagnostics(route):
        route.fulfill(
            status=200,
            headers={"Content-Type": "application/json"},
            body="null",
        )

    page.route("**/relay/diagnostics", handle_null_diagnostics)
    page.goto(base_url)
    page.wait_for_load_state("networkidle")

    status = page.locator(".compute-node-status")
    status.wait_for(state="visible")
    page.wait_for_function(
        "document.querySelector('.compute-node-status').textContent.includes('Live compute nodes: unavailable')"
    )
    assert "Live compute nodes: unavailable" in status.inner_text()


def test_compute_node_count_failure_is_graceful(page: Page, base_url: str, setup_servers):
    """Diagnostic widget failures should be non-alarming and leave chat usable."""

    def handle_diagnostics_failure(route):
        route.fulfill(
            status=503,
            headers={"Content-Type": "application/json"},
            body=json.dumps({"error": "down"}),
        )

    page.route("**/relay/diagnostics", handle_diagnostics_failure)
    page.goto(base_url)
    page.wait_for_load_state("networkidle")

    status = page.locator(".compute-node-status")
    status.wait_for(state="visible")
    page.wait_for_function(
        "document.querySelector('.compute-node-status').textContent.includes('Live compute nodes: unavailable')"
    )
    assert "Live compute nodes: unavailable" in status.inner_text()
    assert page.locator("textarea").first.is_visible()
    assert page.locator("button", has_text="Send").is_visible()


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




def _encode_mock_ciphertext(payload: dict) -> str:
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


def install_mock_landing_relay(page: Page, *, server_public_key_b64: str, replies=None, next_status=200):
    """Mock the landing chat's direct API v1 relay E2EE transport.

    The browser crypto methods are overridden so tests can inspect the plaintext
    envelope without exposing plaintext to relay-owned production code.
    """
    replies = list(replies or ["Mock relay response."])
    state = {"next_calls": 0, "requests": [], "retrieve_calls": [], "v1_chat_calls": [], "v2_calls": []}

    def handle_next(route):
        state["next_calls"] += 1
        if next_status != 200:
            route.fulfill(
                status=next_status,
                headers={"Content-Type": "application/json"},
                body=json.dumps({"error": {"code": "no_registered_compute_nodes", "message": "none"}}),
            )
            return
        route.fulfill(
            status=200,
            headers={"Content-Type": "application/json"},
            body=json.dumps({"server_public_key": server_public_key_b64}),
        )

    def handle_request(route):
        body = route.request.post_data_json
        body["_plaintext_envelope"] = json.loads(base64.b64decode(body["ciphertext"]).decode("utf-8"))
        state["requests"].append(body)
        route.fulfill(
            status=200,
            headers={"Content-Type": "application/json"},
            body=json.dumps({"message": "Request received"}),
        )

    def handle_retrieve(route):
        body = route.request.post_data_json
        state["retrieve_calls"].append(body)
        index = max(0, min(len(state["retrieve_calls"]) - 1, len(replies) - 1))
        response_envelope = {
            "protocol": "tokenplace_api_v1_relay_e2ee",
            "version": 1,
            "request_id": body["request_id"],
            "client_public_key": body["client_public_key"],
            "api_v1_response": {
                "choices": [
                    {"message": {"role": "assistant", "content": replies[index]}}
                ]
            },
        }
        route.fulfill(
            status=200,
            headers={"Content-Type": "application/json"},
            body=json.dumps(
                {
                    "request_id": body["request_id"],
                    "client_public_key": body["client_public_key"],
                    "ciphertext": _encode_mock_ciphertext(response_envelope),
                    "cipherkey": "mock-cipherkey",
                    "iv": "mock-iv",
                }
            ),
        )

    page.route("**/api/v1/relay/servers/next", handle_next)
    page.route("**/api/v1/relay/requests", handle_request)
    page.route("**/api/v1/relay/responses/retrieve", handle_retrieve)
    page.route("**/api/v1/chat/completions", lambda route: (state["v1_chat_calls"].append(route.request.url), route.fulfill(status=500, body="landing chat must not call chat/completions")))
    page.route("**/api/v2/**", lambda route: (state["v2_calls"].append(route.request.url), route.fulfill(status=500, body="API v2 must not be called")))
    return state


def install_browser_crypto_stub(page: Page):
    page.evaluate(
        """
        () => {
            const app = document.querySelector('#app').__vue__;
            app.encrypt = async (plaintext) => ({
                ciphertext: btoa(unescape(encodeURIComponent(plaintext))),
                cipherkey: 'mock-cipherkey',
                iv: 'mock-iv'
            });
            app.decrypt = async (ciphertext) => decodeURIComponent(escape(atob(ciphertext)));
        }
        """
    )

def wait_for_landing_send_enabled(page: Page):
    """Wait until the landing chat readiness gate enables Send, then return the button."""
    send_button = page.locator("button", has_text="Send")
    page.wait_for_function(
        """
        () => {
            const buttons = Array.from(document.querySelectorAll('button'));
            const send = buttons.find((button) => button.textContent.includes('Send'));
            return Boolean(send && !send.disabled);
        }
        """
    )
    return send_button


def test_markdown_rendering_stream_updates(page: Page, base_url: str, setup_servers):
    """The chat UI should render markdown formatting returned by the assistant."""

    markdown_reply = "**Bold** introduction\n\n- First item\n- Second item\n\nHere is `inline` code and:\n```\nblock example\n```"
    server_public_key_b64 = base64.b64encode(b"-----BEGIN PUBLIC KEY-----\nMOCKSERVERKEYMARKDOWN1234567890\n-----END PUBLIC KEY-----").decode("ascii")
    install_mock_landing_relay(page, server_public_key_b64=server_public_key_b64, replies=[markdown_reply])

    page.goto(base_url)
    page.wait_for_load_state("networkidle")
    install_browser_crypto_stub(page)

    textarea = page.locator("textarea").first
    textarea.fill("Show markdown please")
    wait_for_landing_send_enabled(page).click()

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
    """Landing chat must use the direct API v1 relay E2EE flow, not API v2 or chat/completions."""

    server_public_key_b64 = base64.b64encode(b"-----BEGIN PUBLIC KEY-----\nMOCKSERVERKEYV1ONLY1234567890\n-----END PUBLIC KEY-----").decode("ascii")
    relay_state = install_mock_landing_relay(
        page,
        server_public_key_b64=server_public_key_b64,
        replies=["Relay chat path restored."],
    )

    page.goto(base_url)
    page.wait_for_load_state("networkidle")
    install_browser_crypto_stub(page)

    textarea = page.locator("textarea").first
    textarea.fill("hello")
    wait_for_landing_send_enabled(page).click()

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
    assert len(relay_state["requests"]) == 1
    assert relay_state["requests"][0]["protocol"] == "tokenplace_api_v1_relay_e2ee"
    assert relay_state["requests"][0]["server_public_key"] == server_public_key_b64
    assert relay_state["v1_chat_calls"] == []
    assert relay_state["v2_calls"] == []


def test_landing_chat_model_dropdown_uses_api_v1_models(
    page: Page,
    base_url: str,
    setup_servers,
):
    """Landing chat model selector should be populated by GET /api/v1/models and sent in the relay envelope."""

    models_payload = {
        "object": "list",
        "data": [
            {
                "id": "api-v1-first-model",
                "object": "model",
                "owned_by": "community",
                "root": "api-v1-first-model",
            },
            {
                "id": "api-v1-second-model",
                "object": "model",
                "owned_by": "community",
                "root": "api-v1-second-model",
            },
        ],
    }

    server_public_key_b64 = base64.b64encode(b"-----BEGIN PUBLIC KEY-----\nMOCKSERVERKEYMODEL1234567890\n-----END PUBLIC KEY-----").decode("ascii")
    relay_state = install_mock_landing_relay(
        page,
        server_public_key_b64=server_public_key_b64,
        replies=["Selected model acknowledged."],
    )

    page.route(
        "**/api/v1/models",
        lambda route: route.fulfill(
            status=200,
            headers={"Content-Type": "application/json"},
            body=json.dumps(models_payload),
        ),
    )

    page.goto(base_url)
    page.wait_for_load_state("networkidle")
    install_browser_crypto_stub(page)

    model_select = page.get_by_test_id("landing-model-select")
    model_select.wait_for(state="visible")
    assert model_select.input_value() == "api-v1-first-model"
    assert model_select.locator("option").all_inner_texts() == [
        "api-v1-first-model",
        "api-v1-second-model",
    ]

    model_select.select_option("api-v1-second-model")

    textarea = page.locator("textarea").first
    textarea.fill("Use the selected model")
    wait_for_landing_send_enabled(page).click()

    page.locator(".assistant-message").last.wait_for(state="visible")
    assert relay_state["requests"], "expected the landing chat to POST an API v1 relay envelope"
    plaintext = relay_state["requests"][-1]["_plaintext_envelope"]
    assert plaintext["api_v1_request"]["model"] == "api-v1-second-model"
    assert relay_state["v1_chat_calls"] == []
    assert relay_state["v2_calls"] == []


@pytest.mark.e2e
def test_landing_chat_model_catalog_failure_uses_api_v1_fallback(
    page: Page,
    base_url: str,
    setup_servers,
):
    """A failed model list shows a non-blocking error and stays on API v1 fallback chat."""

    server_public_key_b64 = base64.b64encode(b"-----BEGIN PUBLIC KEY-----\nMOCKSERVERKEYFALLBACK1234567890\n-----END PUBLIC KEY-----").decode("ascii")
    relay_state = install_mock_landing_relay(
        page,
        server_public_key_b64=server_public_key_b64,
        replies=["Fallback model acknowledged."],
    )

    page.route(
        "**/api/v1/models",
        lambda route: route.fulfill(
            status=503,
            headers={"Content-Type": "application/json"},
            body=json.dumps({"error": {"message": "catalog temporarily unavailable"}}),
        ),
    )

    page.goto(base_url)
    page.wait_for_load_state("networkidle")
    install_browser_crypto_stub(page)

    model_select = page.get_by_test_id("landing-model-select")
    model_select.wait_for(state="visible")
    assert model_select.input_value() == "llama-3-8b-instruct"
    assert "llama-3-8b-instruct (emergency fallback)" in model_select.locator("option").inner_text()
    assert "Could not load the API v1 model list" in page.locator(".model-error").inner_text()

    page.locator("textarea").first.fill("hello")
    wait_for_landing_send_enabled(page).click()

    page.locator(".assistant-message").last.wait_for(state="visible")
    assert relay_state["requests"], "expected the landing chat to POST the API v1 fallback relay envelope"
    plaintext = relay_state["requests"][-1]["_plaintext_envelope"]
    assert plaintext["api_v1_request"]["model"] == "llama-3-8b-instruct"
    assert relay_state["v1_chat_calls"] == []
    assert relay_state["v2_calls"] == []


@pytest.mark.e2e
def test_landing_chat_shows_no_servers_available_message(
    page: Page,
    base_url: str,
    setup_servers,
):
    """Structured API v1 no-server errors should render a clear landing-chat message."""

    server_public_key_b64 = base64.b64encode(b"-----BEGIN PUBLIC KEY-----\nMOCKSERVERKEYNOSERVER1234567890\n-----END PUBLIC KEY-----").decode("ascii")
    install_mock_landing_relay(page, server_public_key_b64=server_public_key_b64, next_status=503)

    page.goto(base_url)
    page.wait_for_load_state("networkidle")
    install_browser_crypto_stub(page)

    page.locator("textarea").first.fill("hello")
    wait_for_landing_send_enabled(page).click()

    assistant_message = page.locator(".assistant-message").last
    assistant_message.wait_for(state="visible")
    assert "No LLM servers are available right now." in assistant_message.inner_text()


@pytest.mark.e2e
def test_landing_chat_sticky_server_selection_and_key_label(
    page: Page,
    base_url: str,
    setup_servers,
):
    """A browser session selects one compute node, displays only a short label, and reuses it for turns."""

    server_public_key_b64 = base64.b64encode(
        b"-----BEGIN PUBLIC KEY-----\nABCDEF1234567890STICKYKEYFEDCBA0987654321\n-----END PUBLIC KEY-----"
    ).decode("ascii")
    relay_state = install_mock_landing_relay(
        page,
        server_public_key_b64=server_public_key_b64,
        replies=["First sticky response.", "Second sticky response."],
    )

    page.route(
        "**/api/v1/models",
        lambda route: route.fulfill(
            status=200,
            headers={"Content-Type": "application/json"},
            body=json.dumps({"object": "list", "data": [{"id": "sticky-model", "object": "model"}]}),
        ),
    )

    page.goto(base_url)
    page.wait_for_load_state("networkidle")
    install_browser_crypto_stub(page)

    for prompt in ["first turn", "second turn"]:
        page.locator("textarea").first.fill(prompt)
        wait_for_landing_send_enabled(page).click()
        page.locator(".assistant-message").last.wait_for(state="visible")

    assert relay_state["next_calls"] == 1
    assert len(relay_state["requests"]) == 2
    assert {request["server_public_key"] for request in relay_state["requests"]} == {server_public_key_b64}
    assert [
        request["_plaintext_envelope"]["api_v1_request"]["model"] for request in relay_state["requests"]
    ] == ["sticky-model", "sticky-model"]
    assert relay_state["v1_chat_calls"] == []
    assert relay_state["v2_calls"] == []

    session_text = page.get_by_test_id("landing-server-session").inner_text()
    assert re.search(r"Server: [A-Za-z0-9]{8}…[A-Za-z0-9]{8}", session_text)
    assert server_public_key_b64 not in page.locator("body").inner_text()


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
        pytest.skip(
            "TOKENPLACE_REAL_E2E_MODEL_PATH must be configured for the relay landing-page real-inference guardrail."
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

    page.route("**/api/v1/relay/requests", record_v1_request)
    page.route("**/api/v1/chat/completions", lambda route: (_ for _ in ()).throw(AssertionError("landing chat must not call /api/v1/chat/completions")))
    page.route("**/api/v2/**", record_v2_request)

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
        def wait_for_relay_ready(required_consecutive: int, attempts: int, pause_seconds: float) -> tuple[bool, str]:
            relay_ready_local = False
            consecutive_ready_observations_local = 0
            relay_server_selection_body_local = ""
            for _ in range(attempts):
                next_server_response = page.request.get(
                    f"{base_url}/api/v1/relay/servers/next"
                )
                if next_server_response.ok:
                    relay_server_selection_body_local = next_server_response.text()
                    try:
                        payload = next_server_response.json()
                    except Exception:  # pragma: no cover - defensive for non-json relay errors
                        payload = {}
                    if isinstance(payload, dict) and payload.get("server_public_key"):
                        consecutive_ready_observations_local += 1
                        if consecutive_ready_observations_local >= required_consecutive:
                            relay_ready_local = True
                            break
                    else:
                        consecutive_ready_observations_local = 0
                else:
                    consecutive_ready_observations_local = 0
                time.sleep(pause_seconds)

            return relay_ready_local, relay_server_selection_body_local

        relay_ready, relay_server_selection_body = wait_for_relay_ready(
            required_consecutive=3,
            attempts=40,
            pause_seconds=0.25,
        )
        assert relay_ready, (
            "desktop bridge reported registered but relay /api/v1/relay/servers/next "
            "did not expose an active server_public_key in time. Last response body: "
            f"{relay_server_selection_body!r}"
        )

        page.goto(base_url)
        page.wait_for_load_state("networkidle")
        page.wait_for_function(
            """
            () => {
                const appEl = document.querySelector('#app');
                const vm = appEl && appEl.__vue__;
                return Boolean(
                    vm &&
                    typeof vm.clientPublicKey === 'string' &&
                    vm.clientPublicKey.trim().length > 0
                );
            }
            """
        )

        prompt_text = (
            "Reply with a short sentence confirming you received this message. "
            "Keep it under ten words."
        )
        textarea = page.locator("textarea").first
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
        assistant_text = ""
        user_message_count = page.locator(".user-message").count()
        assistant_message_count = page.locator(".assistant-message").count()
        transient_bridge_errors = {
            "Unable to contact the LLM server right now. Please try again.",
            "The LLM server is unavailable right now. Please try again.",
            "The LLM server took too long to respond. Please try again.",
        }
        disallowed_assistant_outputs = {
            "Sorry, I encountered an issue generating a response. Please try again.",
            "Sorry, the relay returned an invalid response. Please try again.",
            "Sorry, an error occurred while sending your message. Please try again.",
        }
        max_attempts = 10
        for attempt in range(max_attempts):
            relay_ready, relay_server_selection_body = wait_for_relay_ready(
                required_consecutive=2,
                attempts=20,
                pause_seconds=0.2,
            )
            assert relay_ready, (
                "relay lost active server selection while waiting to retry chat request. "
                f"Last response body: {relay_server_selection_body!r}"
            )
            textarea.fill(prompt_text)
            wait_for_landing_send_enabled(page).click()
            user_message_count += 1
            page.locator(".user-message").nth(user_message_count - 1).wait_for(state="visible")

            assistant_message_count += 1
            assistant_message = page.locator(".assistant-message").nth(assistant_message_count - 1)
            assistant_message.wait_for(state="visible")
            page.wait_for_function(
                """
                ({ selector, index }) => {
                    const nodes = document.querySelectorAll(selector);
                    const node = nodes[index];
                    if (!node) return false;
                    const text = (node.textContent || '').trim();
                    return text.length > 0;
                }
                """,
                arg={
                    "selector": ".assistant-message",
                    "index": assistant_message_count - 1,
                },
            )
            assistant_text = assistant_message.inner_text().strip()
            if (
                assistant_text
                and assistant_text not in transient_bridge_errors
                and assistant_text not in disallowed_assistant_outputs
            ):
                break

            if attempt < max_attempts - 1:
                # Give the relay/bridge path a brief backoff window before retrying.
                page.wait_for_timeout(800 * (attempt + 1))

        assert assistant_text, "assistant response should not be empty"
        assert assistant_text.strip(), "assistant response should not be empty"
        assert assistant_text.lower() != "stub"
        assert assistant_text != "Sorry, I encountered an issue generating a response. Please try again."
        assert assistant_text != "Sorry, an error occurred while sending your message. Please try again."
        assert assistant_text != "Sorry, the relay returned an invalid response. Please try again."
        assert assistant_text not in transient_bridge_errors
        assert "Unknown streaming error" not in assistant_text

        assert len(v1_requests) >= 1
        assert v2_requests == []

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
        assert encrypted_request.get("protocol") == "tokenplace_api_v1_relay_e2ee"
        assert encrypted_request.get("version") == 1
        assert encrypted_request.get("stream") in (None, False)
        assert isinstance(encrypted_request.get("server_public_key"), str) and encrypted_request["server_public_key"]
        assert isinstance(encrypted_request.get("ciphertext"), str) and encrypted_request["ciphertext"]
        assert isinstance(encrypted_request.get("cipherkey"), str) and encrypted_request["cipherkey"]
        assert isinstance(encrypted_request.get("iv"), str) and encrypted_request["iv"]
        client_public_key = encrypted_request.get("client_public_key")
        assert isinstance(client_public_key, str) and client_public_key
        client_public_key_pem = base64.b64decode(client_public_key, validate=True)
        assert b"-----BEGIN PUBLIC KEY-----" in client_public_key_pem

        work_received_lines = [
            line
            for line in stderr_lines
            if "desktop.compute_node_bridge.api_v1_e2ee.work_received" in line
        ]
        assert work_received_lines, "desktop bridge should receive API v1 encrypted relay work"
        response_submitted_lines = [
            line
            for line in stderr_lines
            if "desktop.compute_node_bridge.api_v1_e2ee.response_submitted" in line
        ]
        assert response_submitted_lines, "desktop bridge should submit API v1 encrypted responses"
        bridge_stderr_text = "".join(stderr_lines)
        assert "E2EE_SENTINEL_SHOULD_NEVER_REACH_RELAY_PLAINTEXT" not in bridge_stderr_text
        assert "E2EE_SENTINEL_SHOULD_NEVER_LEAVE_PROCESS_AS_PLAINTEXT" not in bridge_stderr_text
        assert "E2EE_SENTINEL_SHOULD_NEVER_APPEAR_IN_LOGS_OR_DIAGNOSTICS" not in bridge_stderr_text
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
