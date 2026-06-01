#!/usr/bin/env python3
"""Packaged desktop operator parity e2e against a local API v1 E2EE relay.

This regression exercises the lifecycle validated manually on Windows while using
only deterministic local processes suitable for macOS/Windows CI:

* build a packaged resource layout (including macOS .app Contents/Resources),
* start a local relay in API v1 distributed mode,
* start the packaged compute-node bridge with USE_MOCK_LLM=1,
* wait for pre-registration warm-load readiness before registration,
* send multiple encrypted API v1 chat turns through the relay,
* verify relay response retrieval and queue depth recovery,
* stop/unregister, then start the operator again and verify another turn.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import requests

REPO_ROOT_FOR_IMPORTS = Path(__file__).resolve().parents[2]
if str(REPO_ROOT_FOR_IMPORTS) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT_FOR_IMPORTS))

from api.v1.encryption import EncryptionManager
from desktop_tauri_packaged_helpers import (  # type: ignore[import-not-found]
    REPO_ROOT,
    create_macos_bundle_layout,
    create_packaged_layout,
    reserve_free_port,
    wait_for_livez,
)


LOG_DIR = REPO_ROOT / ".desktop-e2e-logs"
CHAT_TIMEOUT_SECONDS = float(os.environ.get("TOKENPLACE_DESKTOP_PARITY_CHAT_TIMEOUT", "20"))
BRIDGE_START_TIMEOUT_SECONDS = float(os.environ.get("TOKENPLACE_DESKTOP_PARITY_START_TIMEOUT", "45"))
STOP_TIMEOUT_SECONDS = float(os.environ.get("TOKENPLACE_DESKTOP_PARITY_STOP_TIMEOUT", "20"))
PUBLIC_KEY_RE = re.compile(r"LS0tLS1CRUdJTi[A-Za-z0-9+/=]{80,}")
PEM_RE = re.compile(
    r"-----BEGIN PUBLIC KEY-----.*?-----END PUBLIC KEY-----",
    flags=re.DOTALL,
)


def _redact_public_keys(text: str) -> str:
    text = PUBLIC_KEY_RE.sub("<redacted-public-key>", text)
    return PEM_RE.sub("<redacted-public-key-pem>", text)


class SanitizedProcessLogger:
    """Capture subprocess output into a redacted log while retaining a tail."""

    def __init__(self, label: str, stdout_path: Path, stderr_path: Path | None = None) -> None:
        self.label = label
        self.stdout_path = stdout_path
        self.stderr_path = stderr_path
        self.stdout_chunks: list[str] = []
        self.stderr_chunks: list[str] = []
        self.stdout_path.parent.mkdir(parents=True, exist_ok=True)
        self._stdout_handle = self.stdout_path.open("w", encoding="utf-8")
        self._stderr_handle = (
            self.stderr_path.open("w", encoding="utf-8") if self.stderr_path is not None else None
        )
        self._threads: list[threading.Thread] = []

    def attach(self, proc: subprocess.Popen[str]) -> None:
        if proc.stdout is not None:
            thread = threading.Thread(
                target=self._reader,
                args=(proc.stdout, self._stdout_handle, self.stdout_chunks),
                daemon=True,
                name=f"{self.label}-stdout-reader",
            )
            thread.start()
            self._threads.append(thread)
        if proc.stderr is not None and self._stderr_handle is not None:
            thread = threading.Thread(
                target=self._reader,
                args=(proc.stderr, self._stderr_handle, self.stderr_chunks),
                daemon=True,
                name=f"{self.label}-stderr-reader",
            )
            thread.start()
            self._threads.append(thread)

    @staticmethod
    def _reader(stream: Any, handle: Any, chunks: list[str]) -> None:
        while True:
            line = stream.readline()
            if not line:
                return
            sanitized = _redact_public_keys(str(line))
            chunks.append(sanitized)
            if len(chunks) > 800:
                del chunks[:200]
            handle.write(sanitized)
            handle.flush()

    def close(self) -> None:
        for thread in self._threads:
            thread.join(timeout=1)
        self._stdout_handle.close()
        if self._stderr_handle is not None:
            self._stderr_handle.close()


@dataclass
class BridgeRun:
    process: subprocess.Popen[str]
    output_queue: "queue.Queue[str]"
    log_path: Path
    events: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: dict[str, Any] | None = None
    output: str = ""

    def send_cancel(self) -> None:
        if self.process.poll() is not None or self.process.stdin is None:
            return
        self.process.stdin.write('{"type":"cancel"}\n')
        self.process.stdin.flush()

    def close_stdin(self) -> None:
        with contextlib.suppress(OSError):
            if self.process.stdin is not None:
                self.process.stdin.close()


def _enqueue_stream(stream: Any, output_queue: "queue.Queue[str]") -> None:
    while True:
        line = stream.readline()
        if not line:
            return
        output_queue.put(str(line))


def _drain_bridge_output(run: BridgeRun, *, duration_seconds: float = 0.0) -> list[dict[str, Any]]:
    deadline = time.time() + duration_seconds
    parsed: list[dict[str, Any]] = []
    while True:
        timeout = max(0.0, min(0.2, deadline - time.time())) if duration_seconds else 0.0
        try:
            line = run.output_queue.get(timeout=timeout)
        except queue.Empty:
            if duration_seconds and time.time() < deadline:
                continue
            break
        sanitized = _redact_public_keys(line)
        run.output += sanitized
        with run.log_path.open("a", encoding="utf-8") as handle:
            handle.write(sanitized)
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        run.events.append(payload)
        parsed.append(payload)
    return parsed


def _start_relay(port: int, run_id: str) -> tuple[subprocess.Popen[str], SanitizedProcessLogger]:
    env = os.environ.copy()
    env.update(
        {
            "USE_MOCK_LLM": "1",
            "TOKENPLACE_API_V1_ENFORCE_RELAY_DISTRIBUTED": "1",
            "TOKENPLACE_API_V1_COMPUTE_PROVIDER": "distributed",
            "TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK": "0",
            "TOKENPLACE_DISTRIBUTED_COMPUTE_URL": f"http://127.0.0.1:{port}",
            "TOKENPLACE_API_V1_DISTRIBUTED_TIMEOUT_SECONDS": str(CHAT_TIMEOUT_SECONDS),
            "TOKEN_PLACE_API_V1_RELAY_POLL_WAIT_SECONDS": "0.25",
            "TOKEN_PLACE_API_V1_RELAY_SERVER_LEASE_SECONDS": "4",
            "CONTENT_MODERATION_MODE": "off",
        }
    )
    logger = SanitizedProcessLogger(
        f"relay-{run_id}",
        LOG_DIR / f"relay-parity-{run_id}.stdout.log",
        LOG_DIR / f"relay-parity-{run_id}.stderr.log",
    )
    relay = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            str(REPO_ROOT / "relay.py"),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--use_mock_llm",
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    logger.attach(relay)
    wait_for_livez(relay, port, stdout_path=logger.stdout_path, stderr_path=logger.stderr_path)
    return relay, logger


def _start_bridge(
    bridge_script: Path,
    *,
    tmp_root: Path,
    resources_root: Path,
    relay_port: int,
    label: str,
) -> BridgeRun:
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_root / f"home-{label}"),
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(resources_root / "python"),
            "TOKEN_PLACE_PYTHON_IMPORT_ROOT": str(resources_root),
            "USE_MOCK_LLM": "1",
            "TOKENPLACE_DESKTOP_WARM_LOAD": "1",
            "TOKEN_PLACE_API_V1_WARM_LOAD_WAIT_SECONDS": "10",
            "TOKENPLACE_MAX_POLL_FAILURES": "20",
        }
    )
    Path(env["HOME"]).mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"bridge-parity-{label}.log"
    log_path.write_text("", encoding="utf-8")
    proc = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            str(bridge_script),
            "--model",
            "mock.gguf",
            "--mode",
            "auto",
            "--relay-url",
            f"http://127.0.0.1:{relay_port}",
        ],
        cwd=tmp_root,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    output_queue: queue.Queue[str] = queue.Queue()
    assert proc.stdout is not None
    threading.Thread(
        target=_enqueue_stream,
        args=(proc.stdout, output_queue),
        daemon=True,
        name=f"bridge-{label}-reader",
    ).start()
    return BridgeRun(process=proc, output_queue=output_queue, log_path=log_path)


def _wait_for_bridge_ready(run: BridgeRun, *, require_macos_layout: bool) -> dict[str, Any]:
    saw_started = False
    saw_warm_ready = False
    saw_register_after_ready = False
    last_status: dict[str, Any] | None = None
    deadline = time.time() + BRIDGE_START_TIMEOUT_SECONDS
    while time.time() < deadline:
        if run.process.poll() is not None:
            _drain_bridge_output(run)
            raise RuntimeError(f"bridge exited during startup ({run.process.returncode}): {run.output[-6000:]}")
        for payload in _drain_bridge_output(run, duration_seconds=0.2):
            if payload.get("type") == "error":
                raise RuntimeError(f"bridge error event: {payload}")
            if payload.get("type") == "started" and payload.get("running") is True:
                saw_started = True
            if payload.get("type") == "status":
                last_status = payload
                if payload.get("warm_load_state") == "ready" or payload.get("relay_runtime_state") == "ready":
                    saw_warm_ready = True
                if payload.get("registered") is True:
                    if not saw_warm_ready:
                        raise AssertionError(f"registered before warm-load ready: {payload}")
                    saw_register_after_ready = True
                    run.diagnostics = payload
                    _assert_runtime_diagnostics(payload, require_macos_layout=require_macos_layout)
                    return payload
        if saw_started and saw_warm_ready and saw_register_after_ready and last_status:
            return last_status
    raise RuntimeError(
        "bridge did not reach ready+registered status "
        f"(started={saw_started}, warm_ready={saw_warm_ready}, registered={saw_register_after_ready}); "
        f"output={run.output[-6000:]}"
    )


def _assert_runtime_diagnostics(payload: dict[str, Any], *, require_macos_layout: bool) -> None:
    for field_name in ("requested_mode", "effective_mode", "backend_selected", "backend_used", "runtime_path"):
        value = payload.get(field_name)
        assert value not in (None, "", "pending", "unknown"), f"{field_name} unresolved in {payload}"
    assert payload.get("warm_load_state") == "ready", payload
    assert payload.get("relay_runtime_state") == "ready", payload
    assert payload.get("warm_load_duration_ms") is not None, payload
    if require_macos_layout:
        backend_available = str(payload.get("backend_available", "")).lower()
        backend_selected = str(payload.get("backend_selected", "")).lower()
        backend_used = str(payload.get("backend_used", "")).lower()
        fallback_reason = payload.get("fallback_reason")
        if backend_available == "metal" or backend_selected == "metal":
            if backend_used != "metal":
                assert backend_used == "cpu", payload
                assert isinstance(fallback_reason, str) and fallback_reason, payload
        else:
            assert backend_used == "cpu", payload


def _encrypt_browser_messages(messages: list[dict[str, Any]], relay_public_key: str) -> dict[str, str]:
    request_crypto = EncryptionManager()
    encrypted = request_crypto.encrypt_message(messages, relay_public_key)
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
    return json.loads(decrypted.decode("utf-8"))


def _send_chat_turn(
    base_url: str,
    browser_crypto: EncryptionManager,
    relay_public_key: str,
    turn_index: int,
) -> dict[str, Any]:
    response = requests.post(
        f"{base_url}/api/v1/chat/completions",
        json={
            "model": "llama-3-8b-instruct",
            "encrypted": True,
            "client_public_key": browser_crypto.public_key_b64,
            "messages": _encrypt_browser_messages(
                [{"role": "user", "content": f"desktop parity turn {turn_index}"}],
                relay_public_key,
            ),
            "metadata": {
                "inference_target": "desktop_bridge_api_v1_e2ee",
                "relay_path": "api_v1_e2ee",
            },
        },
        timeout=CHAT_TIMEOUT_SECONDS + 5,
    )
    assert response.status_code == 200, response.text
    assert response.headers["X-Tokenplace-API-V1-Resolved-Provider-Path"] == "distributed"
    assert response.headers["X-Tokenplace-API-V1-Execution-Backend-Path"] == "distributed_relay_e2ee"
    assert response.headers["X-Tokenplace-API-V1-Stream-Mode"] == "non-streaming"
    completion = _decrypt_browser_response(browser_crypto, response.json())
    assert completion["choices"][0]["message"]["content"] == "Mock Response: The capital of France is Paris."
    return completion


def _wait_for_queue_depth_zero(base_url: str) -> dict[str, Any]:
    deadline = time.time() + 10
    last_payload: dict[str, Any] = {}
    while time.time() < deadline:
        diagnostics = requests.get(f"{base_url}/relay/diagnostics", timeout=5).json()
        last_payload = diagnostics
        nodes = diagnostics.get("registered_compute_nodes", [])
        if nodes and all(node.get("queue_depth") == 0 for node in nodes):
            return diagnostics
        time.sleep(0.2)
    raise AssertionError(f"queue depth did not return to zero: {last_payload}")


def _wait_for_unregistered(base_url: str) -> None:
    deadline = time.time() + 8
    last_payload: dict[str, Any] = {}
    while time.time() < deadline:
        diagnostics = requests.get(f"{base_url}/relay/diagnostics", timeout=5).json()
        last_payload = diagnostics
        if diagnostics.get("total_registered_compute_nodes") == 0:
            return
        time.sleep(0.25)
    raise AssertionError(f"operator did not unregister or expire: {last_payload}")


def _stop_bridge(run: BridgeRun, base_url: str) -> None:
    run.send_cancel()
    run.close_stdin()
    deadline = time.time() + STOP_TIMEOUT_SECONDS
    while time.time() < deadline and run.process.poll() is None:
        _drain_bridge_output(run, duration_seconds=0.2)
    if run.process.poll() is None:
        run.process.terminate()
        try:
            run.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            run.process.kill()
            run.process.wait(timeout=5)
    _drain_bridge_output(run)
    assert run.process.returncode == 0, run.output[-6000:]
    assert "desktop.compute_node_bridge.unregister.succeeded" in run.output, run.output[-6000:]
    _wait_for_unregistered(base_url)


def _exercise_operator_session(
    *,
    bridge_script: Path,
    tmp_root: Path,
    resources_root: Path,
    relay_port: int,
    label: str,
    turn_count: int,
    require_macos_layout: bool,
    first_turn_index: int,
) -> BridgeRun:
    base_url = f"http://127.0.0.1:{relay_port}"
    run = _start_bridge(
        bridge_script,
        tmp_root=tmp_root,
        resources_root=resources_root,
        relay_port=relay_port,
        label=label,
    )
    _wait_for_bridge_ready(run, require_macos_layout=require_macos_layout)
    browser_crypto = EncryptionManager()
    relay_public_key_response = requests.get(f"{base_url}/api/v1/public-key", timeout=5)
    assert relay_public_key_response.status_code == 200, relay_public_key_response.text
    relay_public_key = relay_public_key_response.json()["public_key"]
    for offset in range(turn_count):
        _send_chat_turn(base_url, browser_crypto, relay_public_key, first_turn_index + offset)
        _drain_bridge_output(run, duration_seconds=0.2)
        diagnostics = _wait_for_queue_depth_zero(base_url)
        assert diagnostics.get("total_registered_compute_nodes") == 1, diagnostics
    _drain_bridge_output(run, duration_seconds=0.5)
    assert run.output.count("desktop.compute_node_bridge.api_v1_e2ee.response_submitted") >= turn_count, run.output[-6000:]
    return run


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex[:8]
    relay_port = reserve_free_port()
    relay: subprocess.Popen[str] | None = None
    relay_logger: SanitizedProcessLogger | None = None
    with tempfile.TemporaryDirectory(prefix="token-place-relay-parity-") as tmpdir:
        tmp_root = Path(tmpdir)
        standard_bridge = create_packaged_layout(tmp_root)
        standard_resources = tmp_root / "resources"
        mac_bridge = create_macos_bundle_layout(tmp_root)
        mac_resources = tmp_root / "TokenPlace.app" / "Contents" / "Resources"
        assert mac_bridge.is_file(), mac_bridge
        assert mac_resources.is_dir(), mac_resources

        relay, relay_logger = _start_relay(relay_port, run_id)
        try:
            base_url = f"http://127.0.0.1:{relay_port}"
            first = _exercise_operator_session(
                bridge_script=mac_bridge,
                tmp_root=tmp_root,
                resources_root=mac_resources,
                relay_port=relay_port,
                label=f"macos-resources-{run_id}-first",
                turn_count=3,
                require_macos_layout=True,
                first_turn_index=0,
            )
            _stop_bridge(first, base_url)

            second = _exercise_operator_session(
                bridge_script=mac_bridge,
                tmp_root=tmp_root,
                resources_root=mac_resources,
                relay_port=relay_port,
                label=f"macos-resources-{run_id}-restart",
                turn_count=1,
                require_macos_layout=True,
                first_turn_index=100,
            )
            _stop_bridge(second, base_url)

            # Exercise the same shared harness on the generic packaged layout so
            # Windows CI covers the core lifecycle even without a .app bundle.
            generic = _exercise_operator_session(
                bridge_script=standard_bridge,
                tmp_root=tmp_root,
                resources_root=standard_resources,
                relay_port=relay_port,
                label=f"standard-resources-{run_id}",
                turn_count=1,
                require_macos_layout=False,
                first_turn_index=200,
            )
            _stop_bridge(generic, base_url)

            relay_log = ""
            if relay_logger is not None:
                relay_log = relay_logger.stdout_path.read_text(errors="replace")
                if relay_logger.stderr_path is not None:
                    relay_log += relay_logger.stderr_path.read_text(errors="replace")
            for marker in (
                "relay.api_v1.request_queued",
                "relay.api_v1.request_dispatched",
                "relay.api_v1.response_received",
                "relay.api_v1.response_retrieved",
            ):
                assert marker in relay_log, relay_log[-6000:]
        finally:
            if relay is not None and relay.poll() is None:
                relay.terminate()
                try:
                    relay.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    relay.kill()
                    relay.wait(timeout=5)
            if relay_logger is not None:
                relay_logger.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
