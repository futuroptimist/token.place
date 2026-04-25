"""Relay + desktop bridge end-to-end regression coverage for v0.1.0 API v1 wiring."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time

import pytest
import requests

from encrypt import decrypt, encrypt, generate_keys


def _wait_for_relay(base_url: str, timeout_seconds: float = 20.0) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            response = requests.get(f"{base_url}/healthz", timeout=2)
            if response.status_code in (200, 503):
                return
        except requests.RequestException:
            pass
        time.sleep(0.25)
    raise AssertionError("relay did not become healthy in time")


def _wait_for_registered_server(base_url: str, timeout_seconds: float = 30.0) -> str:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        response = requests.get(f"{base_url}/next_server", timeout=2)
        payload = response.json()
        server_public_key = payload.get("server_public_key")
        if response.status_code == 200 and isinstance(server_public_key, str) and server_public_key:
            return server_public_key
        time.sleep(0.25)
    raise AssertionError("desktop bridge did not register with relay")


@pytest.mark.e2e
def test_relay_desktop_bridge_encrypted_round_trip_non_streaming_v1_contract():
    """Relay /faucet -> desktop bridge -> /source round-trip returns encrypted reply."""

    relay_port = 5110
    relay_url = f"http://127.0.0.1:{relay_port}"
    env = os.environ.copy()
    env["TOKEN_PLACE_ENV"] = "testing"
    env["USE_MOCK_LLM"] = "1"
    env["TOKEN_PLACE_DESKTOP_DISABLE_RUNTIME_BOOTSTRAP"] = "1"

    relay_process = subprocess.Popen(
        [sys.executable, "relay.py", "--port", str(relay_port), "--use_mock_llm"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )

    bridge_process = subprocess.Popen(
        [
            sys.executable,
            "desktop-tauri/src-tauri/python/compute_node_bridge.py",
            "--model",
            "/tmp/mock-model.gguf",
            "--mode",
            "cpu",
            "--relay-url",
            relay_url,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )

    try:
        _wait_for_relay(relay_url)
        server_public_key_b64 = _wait_for_registered_server(relay_url)
        server_public_key = base64.b64decode(server_public_key_b64)

        client_private_key, client_public_key = generate_keys()
        client_public_key_b64 = base64.b64encode(client_public_key).decode("utf-8")

        chat_history = [{"role": "user", "content": "hello relay desktop bridge"}]
        encrypted_payload, encrypted_key, iv = encrypt(
            json.dumps(chat_history).encode("utf-8"),
            server_public_key,
            use_pkcs1v15=True,
        )

        faucet_response = requests.post(
            f"{relay_url}/faucet",
            json={
                "client_public_key": client_public_key_b64,
                "server_public_key": server_public_key_b64,
                "chat_history": base64.b64encode(encrypted_payload["ciphertext"]).decode("utf-8"),
                "cipherkey": base64.b64encode(encrypted_key).decode("utf-8"),
                "iv": base64.b64encode(iv).decode("utf-8"),
            },
            timeout=5,
        )
        assert faucet_response.status_code == 200

        relay_encrypted_response = None
        for _ in range(120):
            retrieve_response = requests.post(
                f"{relay_url}/retrieve",
                json={"client_public_key": client_public_key_b64},
                timeout=2,
            )
            if retrieve_response.status_code == 429:
                time.sleep(0.5)
                continue
            assert retrieve_response.status_code == 200
            response_json = retrieve_response.json()
            if "chat_history" in response_json:
                relay_encrypted_response = response_json
                break
            time.sleep(0.25)

        assert relay_encrypted_response is not None

        decrypted_bytes = decrypt(
            {
                "ciphertext": base64.b64decode(relay_encrypted_response["chat_history"]),
                "iv": base64.b64decode(relay_encrypted_response["iv"]),
            },
            base64.b64decode(relay_encrypted_response["cipherkey"]),
            client_private_key,
        )
        decrypted_history = json.loads(decrypted_bytes.decode("utf-8"))

        assert isinstance(decrypted_history, list) and len(decrypted_history) >= 2
        assert decrypted_history[-1]["role"] == "assistant"
        assert "Mock Response" in decrypted_history[-1]["content"]
    finally:
        bridge_process.terminate()
        relay_process.terminate()
        for proc in (bridge_process, relay_process):
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
