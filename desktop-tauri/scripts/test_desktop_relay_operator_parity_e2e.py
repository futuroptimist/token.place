#!/usr/bin/env python3
"""Packaged desktop operator relay lifecycle parity e2e.

This script exercises the same API v1 E2EE lifecycle expected from desktop
operators on Windows and macOS without external staging infrastructure:

* local relay startup with mock LLM enabled
* packaged compute-node bridge startup with mock LLM enabled
* warm-load before registration
* API v1 relay registration
* multi-turn encrypted browser chat routed through the local relay
* response submission/retrieval and queue drain
* Stop/unregister
* Start-after-Stop and another successful request

It intentionally avoids real GGUF downloads and external relay dependencies.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.v1.encryption import EncryptionManager, encryption_manager

from test_packaged_operator_e2e import (  # noqa: E402
    create_macos_bundle_layout,
    create_packaged_layout,
    run_compute_bridge_import_probe,
    run_desktop_dependency_preflight,
    run_model_bridge_inspect_probe,
    run_unified_root_import_policy_probe,
    wait_for_livez,
)

LOG_DIR = REPO_ROOT / ".desktop-e2e-logs"
CHAT_TIMEOUT_SECONDS = 15.0
REGISTRATION_TIMEOUT_SECONDS = 90.0
STOP_TIMEOUT_SECONDS = 20.0

_LONG_B64_RE = re.compile(r"(?<![A-Za-z0-9+/=])([A-Za-z0-9+/]{96,}={0,2})(?![A-Za-z0-9+/=])")
_PEM_RE = re.compile(
    r"-----BEGIN [^-]+-----.*?-----END [^-]+-----",
    re.DOTALL,
)


def reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def redact_log_text(text: str) -> str:
    """Redact full public keys/envelopes while preserving useful diagnostics."""

    text = _PEM_RE.sub("<redacted-pem>", text)
    return _LONG_B64_RE.sub("<redacted-b64>", text)


def write_redacted_log(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(redact_log_text(text), encoding="utf-8")


def read_text(path: Path, *, max_chars: int | None = None) -> str:
    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        return f"<unable to read {path}: {exc}>"
    return text if max_chars is None else text[-max_chars:]


def request_json(method: str, url: str, *, timeout: float = 5.0, **kwargs: Any) -> Any:
    response = requests.request(method, url, timeout=timeout, **kwargs)
    response.raise_for_status()
    return response.json()


def encrypt_browser_messages(messages: list[dict[str, Any]], relay_public_key: str) -> dict[str, str]:
    encrypted = encryption_manager.encrypt_message(
        messages,
        relay_public_key,
    )
    if encrypted is None:
        raise AssertionError("failed to encrypt browser messages")
    return {
        "ciphertext": encrypted["ciphertext"],
        "cipherkey": encrypted["cipherkey"],
        "iv": encrypted["iv"],
    }


def decrypt_browser_response(browser_crypto: EncryptionManager, response_body: dict[str, Any]) -> dict[str, Any]:
    encrypted = response_body["data"]
    decrypted = browser_crypto.decrypt_message(
        {
            "ciphertext": base64.b64decode(encrypted["ciphertext"]),
            "iv": base64.b64decode(encrypted["iv"]),
        },
        base64.b64decode(encrypted["cipherkey"]),
    )
    if decrypted is None:
        raise AssertionError("failed to decrypt API v1 response")
    return json.loads(decrypted.decode("utf-8"))


def assert_ciphertext_only(payload: dict[str, Any], *, forbidden_text: str) -> None:
    serialized = json.dumps(payload, sort_keys=True)
    if forbidden_text in serialized:
        raise AssertionError(f"relay payload leaked plaintext marker: {forbidden_text!r}")


@dataclass
class BridgeSession:
    bridge_script: Path
    resources_root: Path | None
    relay_url: str
    layout_label: str
    session_label: str
    mode: str = "auto"
    process: subprocess.Popen[str] | None = None
    output_queue: queue.Queue[str] = field(default_factory=queue.Queue)
    events: list[dict[str, Any]] = field(default_factory=list)
    output: str = ""
    _buffer: str = ""

    @property
    def safe_label(self) -> str:
        raw = f"{self.layout_label}-{self.session_label}".replace(" ", "_").replace("/", "_")
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", raw)

    def start(self, tmp_root: Path) -> None:
        env = packaged_env(
            tmp_root,
            self.resources_root,
            extra_env={
                "USE_MOCK_LLM": "1",
                "TOKENPLACE_API_V1_RELAY_POLL_WAIT_SECONDS": "0.05",
                "TOKENPLACE_API_V1_WARM_LOAD_WAIT_SECONDS": "20",
                "TOKENPLACE_DESKTOP_WARM_LOAD": "1",
                "TOKEN_PLACE_RUNTIME_PATH": "bridge",
            },
        )
        self.process = subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                str(self.bridge_script),
                "--model",
                "mock.gguf",
                "--mode",
                self.mode,
                "--relay-url",
                self.relay_url,
            ],
            cwd=tmp_root,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert self.process.stdout is not None
        threading.Thread(target=self._read_output, args=(self.process.stdout,), daemon=True).start()

    def _read_output(self, stream: Any) -> None:
        for chunk in iter(stream.readline, ""):
            self.output_queue.put(chunk)

    def drain(self, *, timeout: float = 0.0) -> list[dict[str, Any]]:
        drained: list[dict[str, Any]] = []
        deadline = time.monotonic() + timeout
        while True:
            wait = 0.0 if timeout <= 0 else max(0.0, min(0.25, deadline - time.monotonic()))
            try:
                chunk = self.output_queue.get(timeout=wait)
            except queue.Empty:
                break
            self.output += chunk
            self._buffer += chunk
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    self.events.append(event)
                    drained.append(event)
            if timeout > 0 and time.monotonic() >= deadline:
                break
        return drained

    def wait_for_ready_registration(self) -> dict[str, Any]:
        deadline = time.monotonic() + REGISTRATION_TIMEOUT_SECONDS
        last_status: dict[str, Any] | None = None
        saw_started = False
        while time.monotonic() < deadline:
            self.drain(timeout=0.25)
            for event in self.events:
                if event.get("type") == "error":
                    raise RuntimeError(f"bridge emitted error event: {event}")
                if event.get("type") == "started" and event.get("running") is True:
                    saw_started = True
                if event.get("type") in {"started", "status"}:
                    last_status = event
                    if event.get("registered") is True:
                        if event.get("relay_runtime_state") != "ready":
                            raise AssertionError(f"registered before ready warm-load state: {event}")
                        ready_idx = self.output.find("desktop.compute_node_bridge.model_init.ready")
                        register_idx = self.output.find("desktop.compute_node_bridge.api_v1_e2ee.register")
                        succeeded_idx = self.output.find("desktop.compute_node_bridge.registration.succeeded")
                        if ready_idx < 0:
                            raise AssertionError("bridge did not log warm-load readiness before registration")
                        if register_idx >= 0 and ready_idx > register_idx:
                            raise AssertionError("bridge attempted API v1 registration before warm-load readiness")
                        if succeeded_idx >= 0 and ready_idx > succeeded_idx:
                            raise AssertionError("bridge reported registration before warm-load readiness")
                        assert_runtime_diagnostics(event, layout_label=self.layout_label)
                        return event
            if self.process and self.process.poll() is not None:
                break
        raise RuntimeError(
            f"bridge did not register before timeout; saw_started={saw_started}; "
            f"last_status={last_status}; output={redact_log_text(self.output[-4000:])}"
        )

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None and self.process.stdin is not None:
            self.process.stdin.write('{"type":"cancel"}\n')
            self.process.stdin.flush()
        deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
        while time.monotonic() < deadline and self.process.poll() is None:
            self.drain(timeout=0.2)
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.drain(timeout=0.2)
        if self.process.returncode != 0:
            raise RuntimeError(
                f"bridge exited non-zero ({self.process.returncode}); "
                f"output={redact_log_text(self.output[-4000:])}"
            )
        if not any(event.get("type") == "stopped" and event.get("running") is False for event in self.events):
            raise AssertionError("bridge did not emit stopped/running=false status")

    def write_log(self) -> None:
        write_redacted_log(LOG_DIR / f"relay-parity-bridge-{self.safe_label}.log", self.output)
        status_lines = "\n".join(json.dumps(event, sort_keys=True) for event in self.events)
        write_redacted_log(LOG_DIR / f"relay-parity-status-events-{self.safe_label}.jsonl", status_lines + "\n")


def packaged_env(
    tmp_root: Path,
    resources_root: Path | None = None,
    *,
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    resources_root = resources_root or (tmp_root / "resources")
    home_dir = tmp_root / "home"
    home_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["PYTHONNOUSERSITE"] = "1"
    env["TOKEN_PLACE_PYTHON_IMPORT_ROOT"] = str(resources_root)
    env["PYTHONPATH"] = os.pathsep.join([str(resources_root / "python"), str(REPO_ROOT)])
    if extra_env:
        env.update(extra_env)
    return env


def assert_runtime_diagnostics(event: dict[str, Any], *, layout_label: str) -> None:
    required_fields = (
        "requested_mode",
        "effective_mode",
        "backend_available",
        "backend_selected",
        "backend_used",
        "offloaded_layers",
        "runtime_path",
        "relay_runtime_path",
        "warm_load_state",
        "warm_load_duration_ms",
    )
    missing = [field for field in required_fields if field not in event]
    if missing:
        raise AssertionError(f"missing runtime diagnostic fields for {layout_label}: {missing}; event={event}")

    for field_name in ("effective_mode", "backend_available", "backend_selected", "backend_used"):
        value = str(event.get(field_name) or "").strip().lower()
        if value in {"", "pending", "unknown"}:
            raise AssertionError(f"runtime diagnostic {field_name} stuck at {value!r}: {event}")

    if event.get("warm_load_state") != "ready":
        raise AssertionError(f"warm-load not ready after registration: {event}")
    if not isinstance(event.get("warm_load_duration_ms"), int):
        raise AssertionError(f"warm-load duration missing/non-integer: {event}")
    if event.get("runtime_path") != "bridge" or event.get("relay_runtime_path") != "bridge":
        raise AssertionError(f"unexpected runtime path diagnostics: {event}")

    if sys.platform == "darwin":
        backend_available = str(event.get("backend_available") or "").lower()
        backend_used = str(event.get("backend_used") or "").lower()
        fallback_reason = event.get("fallback_reason")
        if backend_available == "metal":
            if backend_used not in {"metal", "cpu"}:
                raise AssertionError(f"Metal probe surfaced unexpected backend_used: {event}")
            if backend_used == "cpu" and event.get("requested_mode") != "cpu" and not fallback_reason:
                raise AssertionError(f"Metal unavailable at runtime but CPU fallback was not explicit: {event}")
        elif backend_used == "cpu" and event.get("requested_mode") != "cpu" and not fallback_reason:
            raise AssertionError(f"CPU fallback must be explicit when Metal is unavailable: {event}")


def wait_for_registered_nodes(relay_url: str, *, expected: int, timeout: float = 10.0) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    last_payload: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        try:
            payload = request_json("GET", f"{relay_url}/relay/diagnostics", timeout=2.0)
            last_payload = payload
            nodes = payload.get("registered_compute_nodes", [])
            if isinstance(nodes, list) and len(nodes) == expected:
                return nodes
        except (requests.RequestException, ValueError):
            pass
        time.sleep(0.2)
    raise AssertionError(f"expected {expected} registered nodes; last diagnostics={last_payload}")


def assert_queue_depth_zero(relay_url: str) -> None:
    nodes = wait_for_registered_nodes(relay_url, expected=1, timeout=5.0)
    depths = [node.get("queue_depth") for node in nodes]
    if depths != [0]:
        raise AssertionError(f"expected queue depth to drain to 0; diagnostics={nodes}")


def send_encrypted_chat_turn(relay_url: str, *, turn_index: int) -> dict[str, Any]:
    relay_public_key = request_json("GET", f"{relay_url}/api/v1/public-key", timeout=5.0)["public_key"]
    browser_crypto = EncryptionManager()
    user_text = f"desktop parity turn {turn_index}"
    response = requests.post(
        f"{relay_url}/api/v1/chat/completions",
        json={
            "model": "llama-3-8b-instruct",
            "encrypted": True,
            "client_public_key": browser_crypto.public_key_b64,
            "messages": encrypt_browser_messages([{"role": "user", "content": user_text}], relay_public_key),
            "metadata": {
                "inference_target": "desktop_bridge_api_v1_e2ee",
                "relay_path": "api_v1_e2ee",
                "parity_turn_index": turn_index,
            },
        },
        timeout=CHAT_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        raise AssertionError(f"chat turn {turn_index} failed: {response.status_code} {response.text}")
    if response.headers.get("X-Tokenplace-API-V1-Resolved-Provider-Path") != "distributed":
        raise AssertionError(f"chat turn {turn_index} did not use distributed provider: {response.headers}")
    if response.headers.get("X-Tokenplace-API-V1-Execution-Backend-Path") != "distributed_relay_e2ee":
        raise AssertionError(f"chat turn {turn_index} did not use relay E2EE backend: {response.headers}")
    if response.headers.get("X-Tokenplace-API-V1-Stream-Mode") != "non-streaming":
        raise AssertionError(f"chat turn {turn_index} unexpectedly streamed: {response.headers}")

    body = response.json()
    if body.get("encrypted") is not True:
        raise AssertionError(f"chat turn {turn_index} response was not encrypted: {body}")
    completion = decrypt_browser_response(browser_crypto, body)
    content = completion["choices"][0]["message"]["content"]
    if "Mock Response" not in content and "Mock response" not in content:
        raise AssertionError(f"unexpected mock response for turn {turn_index}: {completion}")
    assert_ciphertext_only(body, forbidden_text=user_text)
    return completion


def copy_relay_logs(relay_stdout: Path, relay_stderr: Path, *, label: str) -> None:
    write_redacted_log(LOG_DIR / f"relay-parity-relay-{label}.stdout.log", read_text(relay_stdout))
    write_redacted_log(LOG_DIR / f"relay-parity-relay-{label}.stderr.log", read_text(relay_stderr))


def run_layout_parity(
    tmp_path: Path,
    *,
    bridge_script: Path,
    resources_root: Path | None,
    layout_label: str,
    mode: str,
    turns_before_stop: int,
) -> None:
    relay_port = reserve_free_port()
    relay_url = f"http://127.0.0.1:{relay_port}"
    safe_layout_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", layout_label)
    relay_stdout = tmp_path / f"relay-parity-{safe_layout_label}-{relay_port}.stdout.log"
    relay_stderr = tmp_path / f"relay-parity-{safe_layout_label}-{relay_port}.stderr.log"
    relay_stdout_handle = relay_stdout.open("w", encoding="utf-8")
    relay_stderr_handle = relay_stderr.open("w", encoding="utf-8")
    relay_env = os.environ.copy()
    relay_env.update(
        {
            "USE_MOCK_LLM": "1",
            "TOKENPLACE_API_V1_DISTRIBUTED_TIMEOUT_SECONDS": "10",
            "TOKENPLACE_API_V1_RELAY_POLL_WAIT_SECONDS": "0.05",
            "TOKENPLACE_API_V1_LEASE_SECONDS": "4",
            "TOKENPLACE_RELAY_INTERNAL_URL": relay_url,
        }
    )
    relay = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            str(REPO_ROOT / "relay.py"),
            "--host",
            "127.0.0.1",
            "--port",
            str(relay_port),
            "--use_mock_llm",
        ],
        cwd=REPO_ROOT,
        env=relay_env,
        stdout=relay_stdout_handle,
        stderr=relay_stderr_handle,
        text=True,
    )
    sessions: list[BridgeSession] = []
    try:
        wait_for_livez(relay, relay_port, stdout_path=relay_stdout, stderr_path=relay_stderr)

        first = BridgeSession(
            bridge_script=bridge_script,
            resources_root=resources_root,
            relay_url=relay_url,
            layout_label=layout_label,
            session_label="first_start",
            mode=mode,
        )
        sessions.append(first)
        first.start(tmp_path)
        first.wait_for_ready_registration()
        if layout_label.startswith("macOS"):
            if not (Path(resources_root or "") / "python" / "compute_node_bridge.py").is_file():
                raise AssertionError("macOS .app/Contents/Resources bridge layout was not created")
        wait_for_registered_nodes(relay_url, expected=1, timeout=5.0)

        for turn_index in range(turns_before_stop):
            send_encrypted_chat_turn(relay_url, turn_index=turn_index)
            first.drain(timeout=0.5)
            assert_queue_depth_zero(relay_url)

        first.stop()
        first.write_log()
        wait_for_registered_nodes(relay_url, expected=0, timeout=8.0)

        second = BridgeSession(
            bridge_script=bridge_script,
            resources_root=resources_root,
            relay_url=relay_url,
            layout_label=layout_label,
            session_label="start_after_stop",
            mode=mode,
        )
        sessions.append(second)
        second.start(tmp_path)
        second.wait_for_ready_registration()
        wait_for_registered_nodes(relay_url, expected=1, timeout=5.0)
        send_encrypted_chat_turn(relay_url, turn_index=turns_before_stop)
        second.drain(timeout=0.5)
        assert_queue_depth_zero(relay_url)
        second.stop()
        second.write_log()
        wait_for_registered_nodes(relay_url, expected=0, timeout=8.0)
    finally:
        for session in sessions:
            try:
                session.write_log()
            except Exception:
                pass
            if session.process is not None and session.process.poll() is None:
                session.process.kill()
        if relay.poll() is None:
            relay.terminate()
            try:
                relay.wait(timeout=5)
            except subprocess.TimeoutExpired:
                relay.kill()
                relay.wait(timeout=5)
        relay_stdout_handle.close()
        relay_stderr_handle.close()
        copy_relay_logs(relay_stdout, relay_stderr, label=safe_layout_label)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--layout",
        choices=("platform", "standard", "macos", "both"),
        default=os.environ.get("TOKENPLACE_DESKTOP_PARITY_LAYOUT", "platform"),
        help=(
            "Packaged resource layout to exercise. 'platform' uses macOS .app/Contents/Resources "
            "on darwin and standard resources elsewhere."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=("auto", "cpu", "gpu", "hybrid"),
        default=os.environ.get("TOKENPLACE_DESKTOP_PARITY_MODE", "cpu"),
        help="Compute mode to request from the packaged bridge.",
    )
    parser.add_argument(
        "--turns-before-stop",
        type=int,
        default=int(os.environ.get("TOKENPLACE_DESKTOP_PARITY_TURNS", "3")),
        help="Number of sequential encrypted chat turns before Stop.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="token-place-relay-parity-e2e-") as tmpdir:
        tmp_path = Path(tmpdir)
        standard_bridge = create_packaged_layout(tmp_path)
        standard_resources = tmp_path / "resources"
        mac_bridge = create_macos_bundle_layout(tmp_path)
        mac_resources = tmp_path / "TokenPlace.app" / "Contents" / "Resources"

        for resources_root in (standard_resources, mac_resources):
            run_desktop_dependency_preflight(tmp_path, resources_root=resources_root)
            run_unified_root_import_policy_probe(tmp_path, resources_root=resources_root)
            run_model_bridge_inspect_probe(tmp_path, resources_root=resources_root)
            run_compute_bridge_import_probe(tmp_path, resources_root=resources_root)

        if args.layout == "both":
            layout_specs = [
                (standard_bridge, standard_resources, "standard resources"),
                (mac_bridge, mac_resources, "macOS Contents/Resources"),
            ]
        elif args.layout == "macos" or (args.layout == "platform" and sys.platform == "darwin"):
            layout_specs = [(mac_bridge, mac_resources, "macOS Contents/Resources")]
        else:
            layout_specs = [(standard_bridge, standard_resources, "standard resources")]

        for bridge_script, resources_root, layout_label in layout_specs:
            run_layout_parity(
                tmp_path,
                bridge_script=bridge_script,
                resources_root=resources_root,
                layout_label=layout_label,
                mode=args.mode,
                turns_before_stop=max(args.turns_before_stop, 2),
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
