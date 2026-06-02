#!/usr/bin/env python3
"""Desktop operator relay parity e2e for packaged Windows/macOS layouts.

This script exercises the same API v1 relay lifecycle that the desktop UI drives:
packaged-resource bridge startup, warm-load before registration, encrypted API v1
work, encrypted response submission/retrieval, Stop, and Start after Stop.  It is
intentionally self-contained and uses only a local relay plus USE_MOCK_LLM=1 so CI
does not need staging, Cloudflare, Metal/CUDA hardware, or a real GGUF model.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import requests

from api.v1.encryption import EncryptionManager
LOG_DIR = REPO_ROOT / ".desktop-e2e-logs"
REQUEST_TIMEOUT_SECONDS = 30.0
BRIDGE_READY_TIMEOUT_SECONDS = 90.0
STOP_TIMEOUT_SECONDS = 20.0
PUBLIC_KEY_RE = re.compile(r"(?<![A-Za-z0-9+/=])([A-Za-z0-9+/]{80,}={0,2})(?![A-Za-z0-9+/=])")
PRE_REGISTRATION_READY_LOG = "desktop.compute_node_bridge.model_init.ready"
PRE_REGISTRATION_REASON_LOG = "reason=pre_registration"
REGISTRATION_START_LOG = "desktop.compute_node_bridge.api_v1_e2ee.register"
REGISTRATION_SUCCEEDED_LOG = "desktop.compute_node_bridge.registration.succeeded"


# The packaged import helper lives in test_packaged_operator_e2e.py, but Python
# module names cannot contain hyphens and this script is executed by path.  Keep a
# tiny import shim instead of duplicating the packaging/bootstrap helpers.
if "desktop_tauri_packaged_helpers" not in sys.modules:
    import importlib.util

    helper_path = Path(__file__).with_name("test_packaged_operator_e2e.py")
    spec = importlib.util.spec_from_file_location("desktop_tauri_packaged_helpers", helper_path)
    assert spec is not None and spec.loader is not None
    helper_module = importlib.util.module_from_spec(spec)
    sys.modules["desktop_tauri_packaged_helpers"] = helper_module
    spec.loader.exec_module(helper_module)


# Rebind after the dynamic import above for type checkers and normal execution.
from desktop_tauri_packaged_helpers import (  # noqa: E402  # type: ignore[import-not-found]
    create_macos_bundle_layout,
    create_packaged_layout,
    reserve_free_port,
    wait_for_livez,
)


def _redact_public_keys(text: str) -> str:
    return PUBLIC_KEY_RE.sub(lambda match: f"{match.group(1)[:8]}…{match.group(1)[-4:]}", text)


def _write_log(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_redact_public_keys(text), encoding="utf-8")


def _append_log(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(_redact_public_keys(text))


def _packaged_env(tmp_root: Path, resources_root: Path, relay_url: str, *, session_id: str) -> dict[str, str]:
    home_dir = tmp_root / f"home-{session_id}"
    home_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home_dir),
            "PYTHONNOUSERSITE": "1",
            "TOKEN_PLACE_PYTHON_IMPORT_ROOT": str(resources_root),
            "PYTHONPATH": os.pathsep.join([str(resources_root / "python"), str(resources_root)]),
            "USE_MOCK_LLM": "1",
            "TOKENPLACE_DESKTOP_WARM_LOAD": "1",
            "TOKENPLACE_DESKTOP_API_V1_WARM_LOAD_WAIT_SECONDS": "30",
            "TOKENPLACE_OPERATOR_SESSION_ID": session_id,
            "TOKENPLACE_COMPUTE_NODE_SESSION_ID": session_id,
            "TOKENPLACE_API_V1_RELAY_POLL_WAIT_SECONDS": "0.05",
            "TOKENPLACE_API_V1_RELAY_LONG_POLL_SECONDS": "0.05",
            "TOKENPLACE_DISTRIBUTED_COMPUTE_URL": relay_url,
            "TOKENPLACE_API_V1_DISTRIBUTED_RELAY_URL": relay_url,
            "TOKENPLACE_API_V1_COMPUTE_PROVIDER": "distributed",
            "TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK": "0",
            "TOKENPLACE_API_V1_DISTRIBUTED_TIMEOUT_SECONDS": "20",
            "CONTENT_MODERATION_MODE": "off",
        }
    )
    return env


class BridgeProcess:
    def __init__(self, process: subprocess.Popen[str], log_path: Path) -> None:
        self.process = process
        self.log_path = log_path
        self.events: list[dict[str, Any]] = []
        self.output_lines: list[str] = []
        self._thread = threading.Thread(target=self._read_output, daemon=True)
        self._thread.start()

    def _read_output(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            _append_log(self.log_path, line)
            self.output_lines.append(line.rstrip("\n"))
            stripped = line.strip()
            if not stripped or not stripped.startswith("{"):
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                self.events.append(payload)

    def send_cancel(self) -> None:
        if self.process.poll() is not None or self.process.stdin is None:
            return
        with contextlib.suppress(OSError, BrokenPipeError):
            self.process.stdin.write('{"type":"cancel"}\n')
            self.process.stdin.flush()

    def stop(self) -> None:
        self.send_cancel()
        try:
            self.process.wait(timeout=STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


def _bridge_compute_mode() -> str:
    """Return the packaged bridge mode for the mock parity harness."""

    current_platform = platform.system()
    # Windows and macOS desktop auto/gpu modes intentionally fail closed when a
    # GPU-capable llama-cpp-python runtime is missing or CPU-only. This e2e runs
    # with USE_MOCK_LLM=1 and validates relay lifecycle parity, so hosted CI must
    # not depend on CUDA/Metal provisioning before registration can be tested.
    if current_platform in {"Windows", "Darwin"}:
        return "cpu"
    return "auto"


def _start_bridge(
    tmp_root: Path,
    bridge_script: Path,
    resources_root: Path,
    relay_url: str,
    *,
    layout_label: str,
    session_index: int,
) -> BridgeProcess:
    safe_label = layout_label.replace(" ", "-").replace("/", "-")
    session_id = f"{safe_label}-{session_index}-{int(time.time() * 1000)}"
    log_path = LOG_DIR / f"relay-operator-parity-bridge-{safe_label}-session-{session_index}.log"
    _write_log(log_path, f"# bridge layout={layout_label} session={session_id}\n")
    env = _packaged_env(tmp_root, resources_root, relay_url, session_id=session_id)
    process = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            str(bridge_script),
            "--model",
            "mock.gguf",
            "--mode",
            _bridge_compute_mode(),
            "--relay-url",
            relay_url,
        ],
        cwd=tmp_root,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    return BridgeProcess(process, log_path)


def _diagnostics(relay_url: str) -> dict[str, Any]:
    response = requests.get(f"{relay_url}/relay/diagnostics", timeout=5)
    response.raise_for_status()
    return response.json()


def _redacted_diagnostics(relay_url: str) -> dict[str, Any]:
    payload = _diagnostics(relay_url)
    for node in payload.get("registered_compute_nodes", []) or []:
        if isinstance(node, dict) and isinstance(node.get("server_public_key"), str):
            key = node["server_public_key"]
            node["server_public_key"] = f"{key[:8]}…{key[-4:]}"
    return payload


def _assert_warm_load_before_registration(bridge: BridgeProcess, *, layout_label: str) -> None:
    ready_seen = False
    for line_number, line in enumerate(list(bridge.output_lines), start=1):
        if PRE_REGISTRATION_READY_LOG in line and PRE_REGISTRATION_REASON_LOG in line:
            ready_seen = True
        if not ready_seen and (REGISTRATION_START_LOG in line or REGISTRATION_SUCCEEDED_LOG in line):
            raise AssertionError(
                f"{layout_label} registered before pre-registration warm-load was ready: "
                f"line={line_number} log={bridge.log_path} event={line}"
            )
    assert ready_seen, f"{layout_label} never logged pre-registration warm-load readiness: {bridge.log_path}"


def _wait_for_registered(bridge: BridgeProcess, relay_url: str, *, layout_label: str) -> dict[str, Any]:
    deadline = time.time() + BRIDGE_READY_TIMEOUT_SECONDS
    last_event: dict[str, Any] | None = None
    last_diag: dict[str, Any] | None = None
    while time.time() < deadline:
        if bridge.process.poll() is not None:
            raise RuntimeError(
                f"bridge exited before registration for {layout_label}: "
                f"code={bridge.process.returncode} log={bridge.log_path}"
            )
        for event in list(bridge.events):
            last_event = event
            if event.get("type") == "error":
                raise RuntimeError(f"bridge error before registration for {layout_label}: {event}")
            if event.get("registered") is True and event.get("relay_runtime_state") == "ready":
                _assert_ready_runtime_fields(event, layout_label=layout_label)
                return event
        with contextlib.suppress(Exception):
            last_diag = _redacted_diagnostics(relay_url)
        time.sleep(0.1)
    raise RuntimeError(
        f"timed out waiting for registered ready bridge for {layout_label}; "
        f"last_event={last_event}; diagnostics={last_diag}; log={bridge.log_path}"
    )


def _wait_for_no_registered_nodes(relay_url: str, *, layout_label: str) -> None:
    deadline = time.time() + 10
    last_diag: dict[str, Any] | None = None
    while time.time() < deadline:
        last_diag = _redacted_diagnostics(relay_url)
        if int(last_diag.get("total_registered_compute_nodes", 0)) == 0:
            return
        time.sleep(0.2)
    raise RuntimeError(f"registered node remained after Stop for {layout_label}: {last_diag}")


def _assert_ready_runtime_fields(event: dict[str, Any], *, layout_label: str) -> None:
    required = ("requested_mode", "effective_mode", "backend_available", "backend_selected", "backend_used")
    missing = [key for key in required if event.get(key) in (None, "", "unknown", "pending")]
    assert not missing, f"{layout_label} runtime fields are not ready: missing={missing} event={event}"
    assert event.get("warm_load_enabled") is True, event
    assert event.get("warm_load_state") == "ready", event
    assert isinstance(event.get("warm_load_duration_ms"), int), event

    if platform.system() == "Darwin" and "macOS" in layout_label:
        requested_mode = str(event.get("requested_mode") or "")
        backend_available = str(event.get("backend_available") or "")
        backend_used = str(event.get("backend_used") or "")
        fallback_reason = event.get("fallback_reason")
        if requested_mode == "cpu":
            assert backend_used == "cpu", event
            assert fallback_reason in (None, "cpu mode explicitly selected"), event
        elif backend_available == "metal":
            assert backend_used in {"metal", "cpu"}, event
            if backend_used == "cpu":
                assert fallback_reason, event
        else:
            assert backend_used == "cpu", event
            assert fallback_reason, event
            assert "CUDA/Metal" in str(fallback_reason) or "Metal" in str(fallback_reason), event


def _encrypt_messages(messages: list[dict[str, str]], relay_public_key: str) -> dict[str, str]:
    relay_crypto = EncryptionManager()
    encrypted = relay_crypto.encrypt_message(messages, relay_public_key)
    assert encrypted is not None
    return {
        "ciphertext": encrypted["ciphertext"],
        "cipherkey": encrypted["cipherkey"],
        "iv": encrypted["iv"],
    }


def _decrypt_response(client_crypto: EncryptionManager, response_body: dict[str, Any]) -> dict[str, Any]:
    encrypted = response_body["data"]
    decrypted = client_crypto.decrypt_message(
        {
            "ciphertext": base64.b64decode(encrypted["ciphertext"]),
            "iv": base64.b64decode(encrypted["iv"]),
        },
        base64.b64decode(encrypted["cipherkey"]),
    )
    assert decrypted is not None
    return json.loads(decrypted.decode("utf-8"))


def _chat_turn(relay_url: str, relay_public_key: str, *, turn: int) -> dict[str, Any]:
    client_crypto = EncryptionManager()
    user_text = f"desktop relay parity turn {turn}"
    payload = {
        "model": "llama-3-8b-instruct",
        "encrypted": True,
        "client_public_key": client_crypto.public_key_b64,
        "messages": _encrypt_messages([{"role": "user", "content": user_text}], relay_public_key),
        "metadata": {
            "inference_target": "desktop_bridge_api_v1_e2ee",
            "relay_path": "api_v1_e2ee",
            "parity_turn": turn,
        },
    }
    response = requests.post(
        f"{relay_url}/api/v1/chat/completions",
        json=payload,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    assert response.status_code == 200, response.text
    assert response.headers.get("X-Tokenplace-API-V1-Resolved-Provider-Path") == "distributed"
    assert response.headers.get("X-Tokenplace-API-V1-Execution-Backend-Path") == "distributed_relay_e2ee"
    assert response.headers.get("X-Tokenplace-API-V1-Stream-Mode") == "non-streaming"
    body = response.json()
    assert body.get("encrypted") is True, body
    completion = _decrypt_response(client_crypto, body)
    assert completion.get("object") == "chat.completion", completion
    content = completion["choices"][0]["message"]["content"]
    assert isinstance(content, str) and content, completion
    assert user_text not in json.dumps(body, sort_keys=True)
    return completion


def _wait_for_queue_depth_zero(relay_url: str, *, layout_label: str) -> None:
    deadline = time.time() + 10
    last_diag: dict[str, Any] | None = None
    while time.time() < deadline:
        last_diag = _redacted_diagnostics(relay_url)
        nodes = last_diag.get("registered_compute_nodes", []) or []
        if nodes and all(int(node.get("queue_depth", -1)) == 0 for node in nodes):
            return
        time.sleep(0.1)
    raise RuntimeError(f"queue depth did not return to zero for {layout_label}: {last_diag}")


def _read_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _assert_relay_observed_api_v1_success(relay_stdout: Path, stdout_handle: Any, *, min_turns: int) -> None:
    stdout_handle.flush()
    logs = _read_file(relay_stdout)
    assert logs.count("relay.api_v1.request_queued") >= min_turns, logs[-4000:]
    assert logs.count("relay.api_v1.response_received") >= min_turns, logs[-4000:]
    assert logs.count("relay.api_v1.response_retrieved") >= min_turns, logs[-4000:]


def _run_operator_session(
    tmp_root: Path,
    bridge_script: Path,
    resources_root: Path,
    relay_url: str,
    relay_public_key: str,
    *,
    layout_label: str,
    session_index: int,
    turns: int,
) -> BridgeProcess:
    bridge = _start_bridge(
        tmp_root,
        bridge_script,
        resources_root,
        relay_url,
        layout_label=layout_label,
        session_index=session_index,
    )
    try:
        _wait_for_registered(bridge, relay_url, layout_label=layout_label)
        _assert_warm_load_before_registration(bridge, layout_label=layout_label)
        for offset in range(turns):
            _chat_turn(relay_url, relay_public_key, turn=(session_index * 10) + offset)
            _wait_for_queue_depth_zero(relay_url, layout_label=layout_label)
    except Exception:
        bridge.stop()
        raise
    return bridge


def _run_layout_parity(
    tmp_root: Path,
    bridge_script: Path,
    resources_root: Path,
    relay_url: str,
    relay_stdout: Path,
    relay_stdout_handle: Any,
    *,
    layout_label: str,
) -> None:
    public_key_response = requests.get(f"{relay_url}/api/v1/public-key", timeout=5)
    public_key_response.raise_for_status()
    relay_public_key = public_key_response.json()["public_key"]

    bridge = _run_operator_session(
        tmp_root,
        bridge_script,
        resources_root,
        relay_url,
        relay_public_key,
        layout_label=layout_label,
        session_index=1,
        turns=3,
    )
    bridge.stop()
    assert bridge.process.returncode == 0, f"bridge Stop exited {bridge.process.returncode}: {bridge.log_path}"
    _wait_for_no_registered_nodes(relay_url, layout_label=layout_label)

    restarted = _run_operator_session(
        tmp_root,
        bridge_script,
        resources_root,
        relay_url,
        relay_public_key,
        layout_label=layout_label,
        session_index=2,
        turns=1,
    )
    restarted.stop()
    assert restarted.process.returncode == 0, f"bridge restart exited {restarted.process.returncode}: {restarted.log_path}"
    _wait_for_no_registered_nodes(relay_url, layout_label=layout_label)
    _assert_relay_observed_api_v1_success(relay_stdout, relay_stdout_handle, min_turns=4)


def _start_relay(relay_port: int, stdout_path: Path, stderr_path: Path) -> tuple[subprocess.Popen[str], Any, Any]:
    env = os.environ.copy()
    relay_url = f"http://127.0.0.1:{relay_port}"
    env.update(
        {
            "USE_MOCK_LLM": "1",
            "CONTENT_MODERATION_MODE": "off",
            "TOKENPLACE_API_V1_ENFORCE_RELAY_DISTRIBUTED": "1",
            "TOKENPLACE_API_V1_COMPUTE_PROVIDER": "distributed",
            "TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK": "0",
            "TOKENPLACE_DISTRIBUTED_COMPUTE_URL": relay_url,
            "TOKENPLACE_API_V1_DISTRIBUTED_RELAY_URL": relay_url,
            "TOKENPLACE_API_V1_DISTRIBUTED_TIMEOUT_SECONDS": "20",
            "TOKENPLACE_API_V1_RELAY_POLL_WAIT_SECONDS": "0.05",
            "TOKENPLACE_API_V1_RELAY_LONG_POLL_SECONDS": "0.05",
            "PYTHONUNBUFFERED": "1",
        }
    )
    stdout_handle = stdout_path.open("w", encoding="utf-8", buffering=1)
    stderr_handle = stderr_path.open("w", encoding="utf-8", buffering=1)
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
        env=env,
        stdout=stdout_handle,
        stderr=stderr_handle,
        text=True,
    )
    return relay, stdout_handle, stderr_handle


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="token-place-relay-parity-e2e-") as tmpdir:
        tmp_path = Path(tmpdir)
        standard_bridge = create_packaged_layout(tmp_path)
        standard_resources = tmp_path / "resources"
        mac_bridge = create_macos_bundle_layout(tmp_path)
        mac_resources = tmp_path / "TokenPlace.app" / "Contents" / "Resources"
        assert mac_bridge == mac_resources / "python" / "compute_node_bridge.py"
        assert (mac_resources / "python" / "model_bridge.py").is_file()
        assert (mac_resources / "config.py").is_file()

        relay_port = reserve_free_port()
        relay_url = f"http://127.0.0.1:{relay_port}"
        relay_stdout = LOG_DIR / "relay-operator-parity-relay.stdout.log"
        relay_stderr = LOG_DIR / "relay-operator-parity-relay.stderr.log"
        relay, relay_stdout_handle, relay_stderr_handle = _start_relay(
            relay_port, relay_stdout, relay_stderr
        )
        try:
            wait_for_livez(
                relay,
                relay_port,
                stdout_path=relay_stdout,
                stderr_path=relay_stderr,
            )
            _run_layout_parity(
                tmp_path,
                standard_bridge,
                standard_resources,
                relay_url,
                relay_stdout,
                relay_stdout_handle,
                layout_label="standard resources",
            )
            _run_layout_parity(
                tmp_path,
                mac_bridge,
                mac_resources,
                relay_url,
                relay_stdout,
                relay_stdout_handle,
                layout_label="macOS Contents/Resources",
            )
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
            # Redact any full public keys Flask diagnostics may have emitted.
            _write_log(relay_stdout, _read_file(relay_stdout))
            _write_log(relay_stderr, _read_file(relay_stderr))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
