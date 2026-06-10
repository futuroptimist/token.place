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



SERVER_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAnFBKDAvTZEd+IlS59FKV
VFp4DT28sL1iHwZ94dJ5x5lf+Kq4Wxcl8COEQ3rp3QseM2MkAdZ1VvWbUmsonFux
7pVLQDyE+ANQkNd4K840zWV+CghTz34jxK59pb6cifSto7J8Wy7EqhUru7YLhnqZ
xz/AuHBPrq0RUS7f+ycJtfA6vj9Isp0BYpvgwOP97Ey+nCLiR5C/3IazOZblHQ7R
CbfZqP+encMwRbH/IvrXrz6/vecuIrq60fFtyZIbs7dASpfuSL6atIABu6CiSlXy
+6EhlEdmAXaCOPlQMYjc4u2ZNrOUTjuh3Yw8hMGezsTfTYZd2rrbGZRlkpfKbIdX
0QIDAQAB
-----END PUBLIC KEY-----"""
SERVER_PUBLIC_KEY_B64 = base64.b64encode(SERVER_PUBLIC_KEY_PEM.encode("utf-8")).decode("ascii")
ALT_SERVER_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
alt-test-public-key
-----END PUBLIC KEY-----"""
ALT_SERVER_PUBLIC_KEY_B64 = base64.b64encode(ALT_SERVER_PUBLIC_KEY_PEM.encode("utf-8")).decode("ascii")


def patch_landing_crypto_for_visible_envelopes(page: Page):
    """Make landing-chat E2EE envelopes inspectable without weakening production code."""
    page.evaluate(
        """
        () => {
            const vm = document.querySelector('#app').__vue__;
            vm.encrypt = async (plaintext) => ({ ciphertext: plaintext, cipherkey: 'test-cipherkey', iv: 'test-iv' });
            vm.decrypt = async (ciphertext) => ciphertext;
        }
        """
    )


def route_landing_relay_chat(
    page: Page,
    *,
    assistant_content: str = "Relay chat path restored.",
    models_payload: dict | None = None,
    next_status: int = 200,
    next_statuses: list[int] | None = None,
    next_server_keys: list[str] | None = None,
    request_statuses: list[int] | None = None,
    retrieve_statuses: list[int] | None = None,
    diagnostics_count: int | None = None,
    diagnostics_counts: list[int] | None = None,
):
    """Mock the direct API v1 relay routes used by the landing chat."""
    state = {
        "next_calls": 0,
        "relay_requests": [],
        "retrieve_requests": [],
        "cancel_requests": [],
        "chat_completions": [],
        "v2_requests": [],
    }
    default_models = {
        "object": "list",
        "data": [
            {
                "id": "llama-3.1-8b-instruct",
                "object": "model",
                "owned_by": "Meta",
                "root": "llama-3.1-8b-instruct",
            }
        ],
    }

    page.route(
        "**/api/v1/models",
        lambda route: route.fulfill(
            status=200,
            headers={"Content-Type": "application/json"},
            body=json.dumps(models_payload or default_models),
        ),
    )

    if diagnostics_count is not None or diagnostics_counts:
        state["diagnostics_calls"] = 0

        def handle_diagnostics(route):
            state["diagnostics_calls"] += 1
            if diagnostics_counts:
                count = diagnostics_counts[min(state["diagnostics_calls"] - 1, len(diagnostics_counts) - 1)]
            else:
                count = diagnostics_count
            route.fulfill(
                status=200,
                headers={"Content-Type": "application/json"},
                body=json.dumps(
                    {
                        "total_registered_compute_nodes": count,
                        "total_api_v1_registered_compute_nodes": count,
                    }
                ),
            )

        page.route("**/relay/diagnostics", handle_diagnostics)

    def handle_next(route):
        state["next_calls"] += 1
        status = next_status
        if next_statuses:
            status = next_statuses[min(state["next_calls"] - 1, len(next_statuses) - 1)]
        if status != 200:
            route.fulfill(
                status=status,
                headers={"Content-Type": "application/json"},
                body=json.dumps({"error": {"code": "no_registered_compute_nodes"}}),
            )
            return
        server_keys = next_server_keys or [SERVER_PUBLIC_KEY_B64]
        selected_index = min(state["next_calls"] - 1, len(server_keys) - 1)
        route.fulfill(
            status=200,
            headers={"Content-Type": "application/json"},
            body=json.dumps({"server_public_key": server_keys[selected_index]}),
        )

    page.route("**/api/v1/relay/servers/next", handle_next)

    def handle_request(route):
        payload = route.request.post_data_json
        state["relay_requests"].append(payload)
        status = 200
        if request_statuses:
            status = request_statuses[min(len(state["relay_requests"]) - 1, len(request_statuses) - 1)]
        body = {"message": "Request received"} if status == 200 else {"error": {"code": "server_unavailable"}}
        route.fulfill(
            status=status,
            headers={"Content-Type": "application/json"},
            body=json.dumps(body),
        )

    page.route("**/api/v1/relay/requests", handle_request)

    def handle_retrieve(route):
        payload = route.request.post_data_json
        state["retrieve_requests"].append(payload)
        request_id = payload["request_id"]
        client_public_key = payload["client_public_key"]
        status = 200
        if retrieve_statuses:
            status = retrieve_statuses[min(len(state["retrieve_requests"]) - 1, len(retrieve_statuses) - 1)]
        if status != 200:
            route.fulfill(
                status=status,
                headers={"Content-Type": "application/json"},
                body=json.dumps({"error": {"code": "selected_server_terminal"}}),
            )
            return
        route.fulfill(
            status=200,
            headers={"Content-Type": "application/json"},
            body=json.dumps(
                {
                    "chat_history": json.dumps(
                        {
                            "protocol": "tokenplace_api_v1_relay_e2ee",
                            "version": 1,
                            "request_id": request_id,
                            "client_public_key": client_public_key,
                            "api_v1_response": {
                                "message": {
                                    "role": "assistant",
                                    "content": assistant_content,
                                }
                            },
                        }
                    ),
                    "cipherkey": "test-cipherkey",
                    "iv": "test-iv",
                }
            ),
        )

    page.route("**/api/v1/relay/responses/retrieve", handle_retrieve)
    page.route(
        "**/api/v1/relay/requests/cancel",
        lambda route: (
            state["cancel_requests"].append(route.request.post_data_json),
            route.fulfill(status=200, headers={"Content-Type": "application/json"}, body=json.dumps({"status": "cancelled"})),
        ),
    )
    page.route(
        "**/api/v1/chat/completions",
        lambda route: (
            state["chat_completions"].append(route.request.url),
            route.fulfill(status=500, body="landing chat must not call chat/completions"),
        ),
    )
    page.route(
        "**/api/v2/**",
        lambda route: (
            state["v2_requests"].append(route.request.url),
            route.fulfill(status=500, body="landing chat must not call API v2"),
        ),
    )
    return state

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
    route_landing_relay_chat(page, assistant_content=markdown_reply)

    page.goto(base_url)
    page.wait_for_load_state("networkidle")
    patch_landing_crypto_for_visible_envelopes(page)

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
    """Landing chat must use direct API v1 relay E2EE routes and avoid API v2/chat-completions."""

    state = route_landing_relay_chat(page, assistant_content="Relay chat path restored.")

    page.goto(base_url)
    page.wait_for_load_state("networkidle")
    patch_landing_crypto_for_visible_envelopes(page)

    model_select = page.get_by_test_id("landing-model-select")
    model_select.wait_for(state="visible")
    assert model_select.locator("option").all_inner_texts() == ["llama-3.1-8b-instruct"]
    assert "owned by token.place" not in page.locator("body").inner_text().lower()

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
    assert state["next_calls"] == 1
    assert len(state["relay_requests"]) == 1
    assert state["relay_requests"][0]["server_public_key"] == SERVER_PUBLIC_KEY_B64
    request_envelope = json.loads(state["relay_requests"][0]["ciphertext"])
    assert request_envelope["protocol"] == "tokenplace_api_v1_relay_e2ee"
    assert request_envelope["api_v1_request"]["model"] == "llama-3.1-8b-instruct"
    assert request_envelope["api_v1_request"]["messages"] == [{"role": "user", "content": "hello"}]
    assert state["chat_completions"] == []
    assert state["v2_requests"] == []


def test_landing_chat_sticky_server_two_turns_and_key_label(page: Page, base_url: str, setup_servers):
    """A browser chat session selects one compute node once and reuses it across turns."""

    state = route_landing_relay_chat(page, assistant_content="Sticky relay response.")

    page.goto(base_url)
    page.wait_for_load_state("networkidle")
    patch_landing_crypto_for_visible_envelopes(page)

    textarea = page.locator("textarea").first

    textarea.fill("first turn")
    wait_for_landing_send_enabled(page).click()
    page.locator(".assistant-message").last.wait_for(state="visible")

    label = page.get_by_test_id("landing-server-key-label")
    label.wait_for(state="visible")
    label_text = label.inner_text()
    assert re.fullmatch(r"Server: [0-9a-f]{8}…[0-9a-f]{8}", label_text)
    assert SERVER_PUBLIC_KEY_B64 not in page.locator("body").inner_text()
    assert SERVER_PUBLIC_KEY_PEM not in page.locator("body").inner_text()

    textarea.fill("second turn")
    wait_for_landing_send_enabled(page).click()
    page.locator(".assistant-message").nth(1).wait_for(state="visible")

    assert state["next_calls"] == 1
    assert len(state["relay_requests"]) == 2
    assert {payload["server_public_key"] for payload in state["relay_requests"]} == {SERVER_PUBLIC_KEY_B64}
    envelopes = [json.loads(payload["ciphertext"]) for payload in state["relay_requests"]]
    assert envelopes[0]["api_v1_request"]["messages"] == [{"role": "user", "content": "first turn"}]
    assert envelopes[1]["api_v1_request"]["messages"] == [
        {"role": "user", "content": "first turn"},
        {"role": "assistant", "content": "Sticky relay response."},
        {"role": "user", "content": "second turn"},
    ]
    assert all(envelope["api_v1_request"]["model"] == "llama-3.1-8b-instruct" for envelope in envelopes)
    assert state["chat_completions"] == []
    assert state["v2_requests"] == []


@pytest.mark.e2e
@pytest.mark.parametrize(
    ("terminal_endpoint", "terminal_status"),
    [
        ("dispatch", 404),
        ("dispatch", 410),
        ("retrieve", 404),
        ("retrieve", 410),
    ],
)
def test_landing_chat_sticky_server_auto_failover_preserves_history(
    page: Page,
    base_url: str,
    setup_servers,
    terminal_endpoint: str,
    terminal_status: int,
):
    """Terminal selected-server errors automatically reselect once and keep the chat."""

    route_kwargs = {
        "request_statuses": [200, 200, terminal_status, 200, 200] if terminal_endpoint == "dispatch" else None,
        "retrieve_statuses": [200, 200, terminal_status, 200, 200] if terminal_endpoint == "retrieve" else None,
    }
    state = route_landing_relay_chat(
        page,
        assistant_content="Replacement server answered.",
        next_server_keys=[SERVER_PUBLIC_KEY_B64, SERVER_PUBLIC_KEY_B64, ALT_SERVER_PUBLIC_KEY_B64],
        diagnostics_counts=[1, 2],
        **route_kwargs,
    )
    navigations = []
    page.on("framenavigated", lambda frame: navigations.append(frame.url) if frame == page.main_frame else None)

    page.goto(base_url)
    page.wait_for_load_state("networkidle")
    initial_navigation_count = len(navigations)
    patch_landing_crypto_for_visible_envelopes(page)

    textarea = page.locator("textarea").first

    textarea.fill("first turn")
    wait_for_landing_send_enabled(page).click()
    page.locator(".assistant-message").last.wait_for(state="visible")
    first_label = page.get_by_test_id("landing-server-key-label").inner_text()

    textarea.fill("second turn")
    wait_for_landing_send_enabled(page).click()
    page.locator(".assistant-message").nth(1).wait_for(state="visible")
    assert state["next_calls"] == 1
    assert page.get_by_test_id("landing-server-key-label").inner_text() == first_label

    textarea.fill("third turn triggers failover")
    wait_for_landing_send_enabled(page).click()
    page.wait_for_function(
        """
        () => Array.from(document.querySelectorAll('.assistant-message'))
            .filter((node) => node.textContent.includes('Replacement server answered.')).length >= 3
        """
    )

    failure = page.get_by_test_id("landing-selected-server-failure")
    failure.wait_for(state="hidden")
    second_label = page.get_by_test_id("landing-server-key-label").inner_text()
    assert second_label != first_label
    assert re.fullmatch(r"Server: [0-9a-f]{8}…[0-9a-f]{8}", second_label)
    assert "first turn" in page.locator("body").inner_text()
    assert "second turn" in page.locator("body").inner_text()
    assert "third turn triggers failover" in page.locator("body").inner_text()

    textarea.fill("fourth turn stays sticky")
    wait_for_landing_send_enabled(page).click()
    page.wait_for_function(
        """
        () => Array.from(document.querySelectorAll('.user-message'))
            .some((node) => node.textContent.includes('fourth turn stays sticky'))
            && Array.from(document.querySelectorAll('.assistant-message'))
                .filter((node) => node.textContent.includes('Replacement server answered.')).length >= 4
        """
    )

    assert state["next_calls"] == 3
    assert state["diagnostics_calls"] >= 2
    assert [payload["server_public_key"] for payload in state["relay_requests"]] == [
        SERVER_PUBLIC_KEY_B64,
        SERVER_PUBLIC_KEY_B64,
        SERVER_PUBLIC_KEY_B64,
        ALT_SERVER_PUBLIC_KEY_B64,
        ALT_SERVER_PUBLIC_KEY_B64,
    ]
    envelopes = [json.loads(payload["ciphertext"]) for payload in state["relay_requests"]]
    retried_envelope = envelopes[3]
    assert retried_envelope["request_id"] != envelopes[2]["request_id"]
    assert retried_envelope["api_v1_request"]["messages"][-1] == {
        "role": "user",
        "content": "third turn triggers failover",
    }
    assert envelopes[4]["api_v1_request"]["messages"][-1] == {
        "role": "user",
        "content": "fourth turn stays sticky",
    }
    assert len(navigations) == initial_navigation_count
    assert state["chat_completions"] == []
    assert state["v2_requests"] == []


@pytest.mark.e2e
def test_landing_chat_failover_no_servers_keeps_history(
    page: Page,
    base_url: str,
    setup_servers,
):
    """If failover cannot select a replacement compute node, history remains visible."""

    state = route_landing_relay_chat(
        page,
        assistant_content="Initial answer.",
        next_statuses=[200, 503],
        request_statuses=[200, 404],
        next_server_keys=[SERVER_PUBLIC_KEY_B64],
        diagnostics_count=1,
    )

    page.goto(base_url)
    page.wait_for_load_state("networkidle")
    patch_landing_crypto_for_visible_envelopes(page)

    textarea = page.locator("textarea").first
    textarea.fill("first turn remains visible")
    wait_for_landing_send_enabled(page).click()
    page.locator(".assistant-message").last.wait_for(state="visible")

    textarea.fill("second turn cannot fail over")
    wait_for_landing_send_enabled(page).click()
    page.wait_for_function(
        """
        () => document.body.textContent.includes('The previous LLM server disconnected. No replacement LLM server accepted this request. Your chat history is still here.')
        """
    )

    body_text = page.locator("body").inner_text()
    assert "first turn remains visible" in body_text
    assert "Initial answer." in body_text
    assert "second turn cannot fail over" in body_text
    assert "The previous LLM server disconnected. No replacement LLM server accepted this request. Your chat history is still here." in body_text
    assert state["next_calls"] == 1
    assert [payload["server_public_key"] for payload in state["relay_requests"]] == [SERVER_PUBLIC_KEY_B64, SERVER_PUBLIC_KEY_B64]
    assert state["chat_completions"] == []
    assert state["v2_requests"] == []


def test_landing_chat_model_dropdown_uses_api_v1_models(
    page: Page,
    base_url: str,
    setup_servers,
):
    """The landing chat model selector is populated from API v1 and drives relay envelopes."""

    models_payload = {
        "object": "list",
        "data": [
            {
                "id": "api-v1-first-model",
                "object": "model",
                "owned_by": "Meta",
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
    state = route_landing_relay_chat(
        page,
        assistant_content="Selected model acknowledged.",
        models_payload=models_payload,
    )

    page.goto(base_url)
    page.wait_for_load_state("networkidle")
    patch_landing_crypto_for_visible_envelopes(page)

    model_select = page.get_by_test_id("landing-model-select")
    model_select.wait_for(state="visible")
    assert model_select.input_value() == "api-v1-first-model"
    assert model_select.locator("option").all_inner_texts() == [
        "api-v1-first-model",
        "api-v1-second-model",
    ]
    assert "owned by token.place" not in page.locator("body").inner_text().lower()

    model_select.select_option("api-v1-second-model")

    textarea = page.locator("textarea").first
    textarea.fill("Use the selected model")
    wait_for_landing_send_enabled(page).click()

    page.locator(".assistant-message").last.wait_for(state="visible")
    assert state["relay_requests"], "expected the landing chat to POST an API v1 relay payload"
    request_envelope = json.loads(state["relay_requests"][-1]["ciphertext"])
    assert request_envelope["api_v1_request"]["model"] == "api-v1-second-model"
    assert state["chat_completions"] == []
    assert state["v2_requests"] == []


@pytest.mark.e2e
def test_landing_chat_model_catalog_failure_uses_api_v1_fallback(
    page: Page,
    base_url: str,
    setup_servers,
):
    """A failed model list shows a non-blocking error and stays on API v1 fallback relay chat."""

    state = {"relay_requests": [], "v2_requests": [], "chat_completions": [], "next_calls": 0}
    page.route(
        "**/api/v1/models",
        lambda route: route.fulfill(
            status=503,
            headers={"Content-Type": "application/json"},
            body=json.dumps({"error": {"message": "catalog temporarily unavailable"}}),
        ),
    )
    page.route(
        "**/api/v1/relay/servers/next",
        lambda route: (
            state.__setitem__("next_calls", state["next_calls"] + 1),
            route.fulfill(
                status=200,
                headers={"Content-Type": "application/json"},
                body=json.dumps({"server_public_key": SERVER_PUBLIC_KEY_B64}),
            ),
        ),
    )
    page.route(
        "**/api/v1/relay/requests",
        lambda route: (
            state["relay_requests"].append(route.request.post_data_json),
            route.fulfill(status=200, headers={"Content-Type": "application/json"}, body=json.dumps({"message": "Request received"})),
        ),
    )
    page.route(
        "**/api/v1/relay/responses/retrieve",
        lambda route: route.fulfill(
            status=200,
            headers={"Content-Type": "application/json"},
            body=json.dumps(
                {
                    "chat_history": json.dumps(
                        {
                            "protocol": "tokenplace_api_v1_relay_e2ee",
                            "version": 1,
                            "request_id": route.request.post_data_json["request_id"],
                            "client_public_key": route.request.post_data_json["client_public_key"],
                            "api_v1_response": {"message": {"role": "assistant", "content": "Fallback model acknowledged."}},
                        }
                    ),
                    "cipherkey": "test-cipherkey",
                    "iv": "test-iv",
                }
            ),
        ),
    )
    page.route(
        "**/api/v1/chat/completions",
        lambda route: (state["chat_completions"].append(route.request.url), route.fulfill(status=500, body="no")),
    )
    page.route(
        "**/api/v2/**",
        lambda route: (state["v2_requests"].append(route.request.url), route.fulfill(status=500, body="no")),
    )

    page.goto(base_url)
    page.wait_for_load_state("networkidle")
    patch_landing_crypto_for_visible_envelopes(page)

    model_select = page.get_by_test_id("landing-model-select")
    model_select.wait_for(state="visible")
    assert model_select.input_value() == "llama-3.1-8b-instruct"
    assert "llama-3.1-8b-instruct (emergency fallback)" in model_select.locator("option").inner_text()
    assert "Could not load the API v1 model list" in page.locator(".model-error").inner_text()

    page.locator("textarea").first.fill("hello")
    wait_for_landing_send_enabled(page).click()

    page.locator(".assistant-message").last.wait_for(state="visible")
    assert state["relay_requests"], "expected the landing chat to POST the API v1 fallback relay payload"
    request_envelope = json.loads(state["relay_requests"][-1]["ciphertext"])
    assert request_envelope["api_v1_request"]["model"] == "llama-3.1-8b-instruct"
    assert state["chat_completions"] == []
    assert state["v2_requests"] == []


@pytest.mark.e2e
def test_landing_chat_shows_no_servers_available_message(
    page: Page,
    base_url: str,
    setup_servers,
):
    """Structured API v1 no-server errors should render a clear landing-chat message."""

    state = route_landing_relay_chat(page, next_status=503)

    page.goto(base_url)
    page.wait_for_load_state("networkidle")
    patch_landing_crypto_for_visible_envelopes(page)

    page.locator("textarea").first.fill("hello")
    wait_for_landing_send_enabled(page).click()

    assistant_message = page.locator(".assistant-message").last
    assistant_message.wait_for(state="visible")
    assert "No LLM servers are available right now." in assistant_message.inner_text()
    assert state["next_calls"] == 1
    assert state["relay_requests"] == []
    assert state["chat_completions"] == []


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
            "TOKENPLACE_REAL_E2E_MODEL_PATH is not configured; skipping the real relay landing-page desktop-bridge guardrail."
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

    relay_requests = []
    chat_completion_requests = []
    v2_requests = []

    def record_relay_request(route):
        relay_requests.append(route.request)
        route.continue_()

    def record_chat_completion_request(route):
        chat_completion_requests.append(route.request.url)
        route.fulfill(status=500, body="landing chat must not call chat/completions")

    def record_v2_request(route):
        v2_requests.append(route.request.url)
        route.continue_()

    page.route("**/api/v1/relay/requests", record_relay_request)
    page.route("**/api/v1/chat/completions", record_chat_completion_request)
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

        assert len(relay_requests) >= 1
        assert chat_completion_requests == []
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

        encrypted_request = relay_requests[0].post_data_json
        assert encrypted_request.get("protocol") == "tokenplace_api_v1_relay_e2ee"
        assert encrypted_request.get("version") == 1
        assert isinstance(encrypted_request.get("server_public_key"), str) and encrypted_request["server_public_key"]
        assert isinstance(encrypted_request.get("request_id"), str) and encrypted_request["request_id"]
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
