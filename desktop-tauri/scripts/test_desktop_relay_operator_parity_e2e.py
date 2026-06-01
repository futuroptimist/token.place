#!/usr/bin/env python3
"""Desktop relay operator parity e2e for packaged bridge lifecycle.

Runs a local relay and a packaged-layout compute-node bridge with USE_MOCK_LLM=1,
then proves warm-load, registration, API v1 E2EE work processing, multi-turn
chat stability, Stop, unregister/disappearance, Start after Stop, and status
runtime diagnostics without using external relays or real GGUF models.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import platform
import re
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from api.v1.encryption import EncryptionManager  # noqa: E402
from test_packaged_operator_e2e import (  # noqa: E402
    create_macos_bundle_layout,
    reserve_free_port,
    wait_for_livez,
    _packaged_env,
    _tail_text,
)

LOG_DIR = REPO_ROOT / ".desktop-e2e-logs"
REQUEST_TIMEOUT_SECONDS = 8.0
CHAT_TIMEOUT_SECONDS = 20.0
BRIDGE_READY_TIMEOUT_SECONDS = 45.0
STOP_TIMEOUT_SECONDS = 20.0

SENSITIVE_STATUS_KEYS = {"server_public_key", "client_public_key", "public_key"}


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("[redacted]" if key in SENSITIVE_STATUS_KEYS else _redact_value(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, str) and len(value) > 96 and "BEGIN PUBLIC KEY" in value:
        return f"{value[:24]}...[redacted]...{value[-16:]}"
    return value


def _write_json_log(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_redact_value(payload), indent=2, sort_keys=True), encoding="utf-8")


def _redact_log_text(text: str) -> str:
    text = re.sub(r'(?i)("(?:server_public_key|client_public_key|public_key)"\s*:\s*")[^"]+"', r'\1[redacted]"', text)
    text = re.sub(r'(?i)((?:server_public_key|client_public_key|public_key)=)([^\s,}]+)', r'\1[redacted]', text)
    return text


def _persist_redacted_log(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(_redact_log_text(source.read_text(errors="replace")), encoding="utf-8")


def _enqueue_stream(stream: Any, output_queue: "queue.Queue[tuple[str, str]]", label: str) -> None:
    while True:
        line = stream.readline()
        if line == "":
            return
        output_queue.put((label, line))


class BridgeSession:
    def __init__(
        self,
        *,
        tmp_root: Path,
        resources_root: Path,
        bridge_script: Path,
        relay_url: str,
        session_label: str,
        mode: str,
    ) -> None:
        self.session_label = session_label
        self.stdout_log = LOG_DIR / f"relay-parity-bridge-{session_label}.stdout.log"
        self.stderr_log = LOG_DIR / f"relay-parity-bridge-{session_label}.stderr.log"
        self.status_log = LOG_DIR / f"relay-parity-status-events-{session_label}.json"
        env = _packaged_env(
            tmp_root,
            resources_root,
            extra_env={
                "USE_MOCK_LLM": "1",
                "TOKENPLACE_DESKTOP_WARM_LOAD": "1",
                "TOKENPLACE_API_V1_DISTRIBUTED_TIMEOUT_SECONDS": str(CHAT_TIMEOUT_SECONDS),
                "TOKEN_PLACE_API_V1_RELAY_POLL_WAIT_SECONDS": "0.2",
                "TOKEN_PLACE_API_V1_RELAY_SERVER_LEASE_SECONDS": "5",
                "TOKENPLACE_COMPUTE_NODE_SESSION_ID": f"parity-{session_label}",
            },
        )
        self.process = subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                str(bridge_script),
                "--model",
                "mock.gguf",
                "--mode",
                mode,
                "--relay-url",
                relay_url,
            ],
            cwd=tmp_root,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        assert self.process.stdout is not None
        assert self.process.stderr is not None
        self._queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._stdout_lines: list[str] = []
        self._stderr_lines: list[str] = []
        self.events: list[dict[str, Any]] = []
        threading.Thread(target=_enqueue_stream, args=(self.process.stdout, self._queue, "stdout"), daemon=True).start()
        threading.Thread(target=_enqueue_stream, args=(self.process.stderr, self._queue, "stderr"), daemon=True).start()

    def _record_line(self, label: str, line: str) -> None:
        if label == "stdout":
            self._stdout_lines.append(line)
            with contextlib.suppress(json.JSONDecodeError):
                payload = json.loads(line)
                if isinstance(payload, dict):
                    self.events.append(payload)
        else:
            self._stderr_lines.append(line)

    def drain(self, *, timeout: float = 0.0) -> None:
        deadline = time.time() + timeout
        while True:
            wait = max(0.0, min(0.1, deadline - time.time())) if timeout else 0.0
            try:
                label, line = self._queue.get(timeout=wait)
            except queue.Empty:
                return
            self._record_line(label, line)
            if timeout and time.time() >= deadline:
                timeout = 0.0

    def wait_for_ready_registration(self) -> dict[str, Any]:
        deadline = time.time() + BRIDGE_READY_TIMEOUT_SECONDS
        last_event: dict[str, Any] | None = None
        while time.time() < deadline:
            self.drain(timeout=0.25)
            stderr_so_far = "".join(self._stderr_lines)
            saw_warm_ready_before_registration = (
                "desktop.compute_node_bridge.model_init.ready reason=pre_registration" in stderr_so_far
                and "desktop.compute_node_bridge.registration.gate_wait_done" in stderr_so_far
                and "desktop.compute_node_bridge.api_v1_e2ee.register" in stderr_so_far
                and stderr_so_far.index("desktop.compute_node_bridge.registration.gate_wait_done")
                < stderr_so_far.index("desktop.compute_node_bridge.api_v1_e2ee.register")
            )
            for event in self.events:
                if event.get("type") == "error":
                    raise RuntimeError(f"bridge emitted error before readiness: {event}")
                if event.get("registered") is True and event.get("relay_runtime_state") == "ready":
                    assert saw_warm_ready_before_registration, self.tail()
                    last_event = event
            if last_event is not None:
                return last_event
            if self.process.poll() is not None:
                break
        raise RuntimeError(f"bridge did not reach ready registered state; tail={self.tail()}")

    def stop(self) -> None:
        if self.process.poll() is not None:
            return
        assert self.process.stdin is not None
        self.process.stdin.write('{"type":"cancel"}\n')
        self.process.stdin.flush()
        try:
            self.process.wait(timeout=STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.drain(timeout=0.5)

    def persist_logs(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.stdout_log.write_text("".join(self._stdout_lines), encoding="utf-8")
        self.stderr_log.write_text("".join(self._stderr_lines), encoding="utf-8")
        _write_json_log(self.status_log, self.events)

    def tail(self) -> str:
        return "\n".join((self._stdout_lines + self._stderr_lines)[-80:])


def _fetch_json(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    response = requests.request(method, url, timeout=REQUEST_TIMEOUT_SECONDS, **kwargs)
    response.raise_for_status()
    payload = response.json()
    assert isinstance(payload, dict), payload
    return payload


def _encrypt_browser_messages(messages: list[dict[str, Any]], server_public_key: str) -> dict[str, str]:
    encrypted = EncryptionManager().encrypt_message(messages, server_public_key)
    assert encrypted is not None
    return {
        "ciphertext": encrypted["ciphertext"],
        "cipherkey": encrypted["cipherkey"],
        "iv": encrypted["iv"],
    }


def _decrypt_browser_response(browser_crypto: EncryptionManager, response_body: dict[str, Any]) -> dict[str, Any]:
    encrypted = response_body["data"]
    decrypted = browser_crypto.decrypt_message(
        {
            "ciphertext": base64.b64decode(encrypted["ciphertext"]),
            "iv": base64.b64decode(encrypted["iv"]),
        },
        base64.b64decode(encrypted["cipherkey"]),
    )
    assert decrypted is not None
    payload = json.loads(decrypted.decode("utf-8"))
    assert isinstance(payload, dict)
    return payload


def _send_encrypted_chat_turn(relay_url: str, server_public_key: str, turn_index: int) -> dict[str, Any]:
    browser_crypto = EncryptionManager()
    prompt = f"desktop relay parity turn {turn_index}"
    response = requests.post(
        f"{relay_url}/api/v1/chat/completions",
        json={
            "model": "llama-3-8b-instruct",
            "encrypted": True,
            "client_public_key": browser_crypto.public_key_b64,
            "messages": _encrypt_browser_messages([{"role": "user", "content": prompt}], server_public_key),
            "metadata": {
                "inference_target": "desktop_bridge_api_v1_e2ee",
                "relay_path": "api_v1_e2ee",
                "parity_turn": turn_index,
            },
        },
        timeout=CHAT_TIMEOUT_SECONDS,
    )
    assert response.status_code == 200, response.text
    assert response.headers["X-Tokenplace-API-V1-Resolved-Provider-Path"] == "distributed"
    assert response.headers["X-Tokenplace-API-V1-Execution-Backend-Path"] == "distributed_relay_e2ee"
    assert response.headers["X-Tokenplace-API-V1-Stream-Mode"] == "non-streaming"
    body = response.json()
    assert body.get("encrypted") is True, body
    completion = _decrypt_browser_response(browser_crypto, body)
    content = completion["choices"][0]["message"]["content"]
    assert content == "Mock Response: The capital of France is Paris.", completion
    return completion


def _wait_for_single_registered_node(relay_url: str) -> dict[str, Any]:
    deadline = time.time() + 10
    last_diag: dict[str, Any] | None = None
    while time.time() < deadline:
        diagnostics = _fetch_json("GET", f"{relay_url}/relay/diagnostics")
        last_diag = diagnostics
        nodes = diagnostics.get("registered_compute_nodes")
        if isinstance(nodes, list) and len(nodes) == 1:
            node = nodes[0]
            assert node.get("queue_depth") == 0, diagnostics
            return node
        time.sleep(0.2)
    raise AssertionError(f"expected one registered node; diagnostics={last_diag}")


def _wait_for_no_registered_nodes(relay_url: str) -> None:
    deadline = time.time() + 10
    last_diag: dict[str, Any] | None = None
    while time.time() < deadline:
        diagnostics = _fetch_json("GET", f"{relay_url}/relay/diagnostics")
        last_diag = diagnostics
        nodes = diagnostics.get("registered_compute_nodes")
        if isinstance(nodes, list) and len(nodes) == 0:
            return
        time.sleep(0.2)
    raise AssertionError(f"expected operator unregister/disappearance after Stop; diagnostics={last_diag}")


def _assert_queue_depth_zero(relay_url: str) -> None:
    diagnostics = _fetch_json("GET", f"{relay_url}/relay/diagnostics")
    nodes = diagnostics.get("registered_compute_nodes")
    assert isinstance(nodes, list) and len(nodes) == 1, diagnostics
    assert nodes[0].get("queue_depth") == 0, diagnostics


def _assert_runtime_status(event: dict[str, Any], *, expect_macos_resources: bool) -> None:
    assert event.get("runtime_path") == "bridge", event
    assert event.get("relay_runtime_path") == "bridge", event
    assert event.get("warm_load_state") == "ready", event
    assert event.get("warm_load_duration_ms") is not None, event
    for key in ("requested_mode", "effective_mode", "backend_available", "backend_selected", "backend_used"):
        value = event.get(key)
        assert value not in (None, "", "pending", "unknown"), event
    if expect_macos_resources:
        assert event.get("requested_mode") == "auto", event
        backend_available = event.get("backend_available")
        backend_used = event.get("backend_used")
        fallback_reason = event.get("fallback_reason")
        if backend_available == "metal":
            assert backend_used in {"metal", "cpu"}, event
            if backend_used == "cpu":
                assert fallback_reason, event
        else:
            assert backend_available == "cpu", event
            assert backend_used == "cpu", event
            assert fallback_reason, event


def _start_relay(port: int, tmp_root: Path) -> tuple[subprocess.Popen[str], Path, Path, Any, Any]:
    relay_stdout = tmp_root / f"relay-parity-local-relay-{port}.stdout.raw.log"
    relay_stderr = tmp_root / f"relay-parity-local-relay-{port}.stderr.raw.log"
    relay_stdout_handle = relay_stdout.open("w", encoding="utf-8")
    relay_stderr_handle = relay_stderr.open("w", encoding="utf-8")
    relay_url = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env.update(
        {
            "USE_MOCK_LLM": "1",
            "CONTENT_MODERATION_MODE": "off",
            "TOKENPLACE_API_V1_COMPUTE_PROVIDER": "distributed",
            "TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK": "0",
            "TOKENPLACE_DISTRIBUTED_COMPUTE_URL": relay_url,
            "TOKENPLACE_API_V1_DISTRIBUTED_TIMEOUT_SECONDS": str(CHAT_TIMEOUT_SECONDS),
            "TOKEN_PLACE_API_V1_RELAY_POLL_WAIT_SECONDS": "0.2",
            "TOKEN_PLACE_API_V1_RELAY_SERVER_LEASE_SECONDS": "5",
            "TOKEN_PLACE_API_V1_IN_FLIGHT_TTL_SECONDS": "5",
        }
    )
    relay = subprocess.Popen(  # noqa: S603
        [sys.executable, str(REPO_ROOT / "relay.py"), "--host", "127.0.0.1", "--port", str(port), "--use_mock_llm"],
        cwd=REPO_ROOT,
        env=env,
        stdout=relay_stdout_handle,
        stderr=relay_stderr_handle,
        text=True,
    )
    wait_for_livez(relay, port, stdout_path=relay_stdout, stderr_path=relay_stderr)
    return relay, relay_stdout, relay_stderr, relay_stdout_handle, relay_stderr_handle


def _run_parity_sequence(tmp_root: Path, relay_url: str, resources_root: Path, bridge_script: Path) -> None:
    assert resources_root.name == "Resources"
    assert resources_root.parent.name == "Contents"
    assert bridge_script.exists(), bridge_script
    assert bridge_script.parent == resources_root / "python"

    server_public_key = _fetch_json("GET", f"{relay_url}/api/v1/public-key")["public_key"]
    mode = "auto" if platform.system() == "Darwin" else "cpu"

    first = BridgeSession(
        tmp_root=tmp_root,
        resources_root=resources_root,
        bridge_script=bridge_script,
        relay_url=relay_url,
        session_label="first",
        mode=mode,
    )
    second: BridgeSession | None = None
    try:
        ready_event = first.wait_for_ready_registration()
        _assert_runtime_status(ready_event, expect_macos_resources=platform.system() == "Darwin")
        _wait_for_single_registered_node(relay_url)
        for turn_index in range(3):
            _send_encrypted_chat_turn(relay_url, server_public_key, turn_index)
            _assert_queue_depth_zero(relay_url)
        first.stop()
        assert first.process.returncode == 0, first.tail()
        _wait_for_no_registered_nodes(relay_url)

        second = BridgeSession(
            tmp_root=tmp_root,
            resources_root=resources_root,
            bridge_script=bridge_script,
            relay_url=relay_url,
            session_label="restart",
            mode=mode,
        )
        restart_ready_event = second.wait_for_ready_registration()
        _assert_runtime_status(restart_ready_event, expect_macos_resources=platform.system() == "Darwin")
        _wait_for_single_registered_node(relay_url)
        _send_encrypted_chat_turn(relay_url, server_public_key, 99)
        _assert_queue_depth_zero(relay_url)
        second.stop()
        assert second.process.returncode == 0, second.tail()
        _wait_for_no_registered_nodes(relay_url)
    finally:
        first.stop()
        first.persist_logs()
        if second is not None:
            second.stop()
            second.persist_logs()


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="token-place-relay-parity-e2e-") as tmpdir:
        tmp_root = Path(tmpdir)
        bridge_script = create_macos_bundle_layout(tmp_root)
        resources_root = tmp_root / "TokenPlace.app" / "Contents" / "Resources"
        relay_port = reserve_free_port()
        relay, relay_stdout, relay_stderr, relay_stdout_handle, relay_stderr_handle = _start_relay(relay_port, tmp_root)
        try:
            _run_parity_sequence(tmp_root, f"http://127.0.0.1:{relay_port}", resources_root, bridge_script)
        finally:
            if relay.poll() is None:
                relay.terminate()
                try:
                    relay.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    relay.kill()
                    relay.wait(timeout=5)
            relay_stdout_handle.close()
            relay_stderr_handle.close()
            redacted_stdout = LOG_DIR / f"relay-parity-local-relay-{relay_port}.stdout.log"
            redacted_stderr = LOG_DIR / f"relay-parity-local-relay-{relay_port}.stderr.log"
            _persist_redacted_log(relay_stdout, redacted_stdout)
            _persist_redacted_log(relay_stderr, redacted_stderr)
            summary = {
                "relay_stdout_tail": _redact_log_text(_tail_text(relay_stdout)),
                "relay_stderr_tail": _redact_log_text(_tail_text(relay_stderr)),
                "platform": platform.platform(),
                "resources_root": str(resources_root),
            }
            _write_json_log(LOG_DIR / "relay-parity-summary.json", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
