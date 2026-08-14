#!/usr/bin/env python3
"""Desktop UI end-to-end test: relay + Tauri app + operator + inference."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

import psutil

REPO_ROOT = Path(__file__).resolve().parents[2]
DESKTOP_ROOT = REPO_ROOT / "desktop-tauri"
TAURI_ROOT = DESKTOP_ROOT / "src-tauri"
WEBDRIVER_URL = "http://127.0.0.1:4444"
LOGS_DIR = REPO_ROOT / ".desktop-e2e-logs"
BOOTSTRAP_LOG = LOGS_DIR / "bootstrap.log"

# Ensure diagnostics artifact directory exists before fragile bootstrap/import steps.
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# Ensure repo-local imports work when this file is executed directly.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from selenium import webdriver
    from selenium.common.exceptions import (
        NoSuchElementException,
        NoSuchFrameException,
        StaleElementReferenceException,
        TimeoutException,
        WebDriverException,
    )
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.action_chains import ActionChains  # pragma: no cover
    from selenium.webdriver.common.keys import Keys  # pragma: no cover
    from selenium.webdriver.support.ui import WebDriverWait

    from utils.crypto_helpers import CryptoClient
    from scripts.long_context_benchmark.benchmark_harness import (
        apply_benchmark_context_tier,
        classify_benchmark_landing_state,
        observe_post_terminal,
        benchmark_operator_mode,
        OwnedProcessTreeMemorySampler,
        PACKAGED_FAILURE_REASONS,
        parse_packaged_local_telemetry,
        packaged_phase_remaining,
        prefill_cancellation_trigger_state,
        start_phase_after,
    )
except Exception as exc:
    BOOTSTRAP_LOG.write_text(
        "desktop ui e2e bootstrap failure\n"
        f"error_type={type(exc).__name__}\n"
        f"error={exc}\n",
        encoding="utf-8",
    )
    raise


def reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_http_200(url: str, timeout_seconds: float = 30.0) -> None:
    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urlopen(url, timeout=2) as response:  # nosec B310
                if response.status == 200:
                    return
        except Exception as exc:  # pragma: no cover
            last_error = exc
        time.sleep(0.25)
    raise RuntimeError(f"timeout waiting for {url}: {last_error}")



def fetch_relay_diagnostics_count(relay_url: str, *, timeout_seconds: float) -> int:
    with urlopen(f"{relay_url}/relay/diagnostics", timeout=timeout_seconds) as response:  # nosec B310
        payload = json.loads(response.read().decode("utf-8"))
    return int(payload["total_api_v1_registered_compute_nodes"])


def wait_for_relay_diagnostics_count(relay_url: str, expected_count: int, timeout_seconds: float) -> float:
    started = time.monotonic()
    deadline = started + timeout_seconds
    last_count: int | None = None
    last_error: Exception | None = None
    while True:
        now = time.monotonic()
        if now >= deadline:
            break
        remaining = deadline - now
        try:
            last_count = fetch_relay_diagnostics_count(
                relay_url,
                timeout_seconds=max(0.05, min(remaining, 0.5)),
            )
            last_error = None
        except Exception as exc:  # pragma: no cover - depends on transient relay timing
            last_error = exc
            time.sleep(0.1)
            continue
        if last_count == expected_count:
            return time.monotonic() - started
        time.sleep(0.1)
    raise AssertionError(
        f"expected relay diagnostics compute-node count {expected_count}, "
        f"got {last_count}; last_error={last_error}"
    )


def wait_for_port(
    host: str,
    port: int,
    process: subprocess.Popen[str] | None = None,
    process_label: str = "process",
    process_log: Path | None = None,
    timeout_seconds: float = 60.0,
) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if process is not None and process.poll() is not None:
            log_tail = read_tail(process_log) if process_log is not None else ""
            raise RuntimeError(
                f"{process_label} exited before opening {host}:{port}; "
                f"returncode={process.returncode}; log_tail={log_tail}"
            )
        with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
            sock.settimeout(1)
            if sock.connect_ex((host, port)) == 0:
                return
        time.sleep(0.25)
    if process is not None and process.poll() is not None:
        log_tail = read_tail(process_log) if process_log is not None else ""
        raise RuntimeError(
            f"timeout waiting for {host}:{port}; {process_label} already exited; "
            f"returncode={process.returncode}; log_tail={log_tail}"
        )
    raise RuntimeError(f"timeout waiting for {host}:{port}")


def ensure_alive(process: subprocess.Popen[str], label: str) -> None:
    if process.poll() is None:
        return
    raise RuntimeError(f"{label} exited early with code {process.returncode}")


def read_tail(path: Path) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[-4000:]


def diagnostics_message(
    message: str,
    relay_log: Path,
    driver_log: Path,
    driver: webdriver.Remote | None = None,
) -> str:
    page_source_tail = ""
    if driver is not None:
        with contextlib.suppress(Exception):
            page_source_tail = driver.page_source[-4000:]
    return (
        f"{message}; "
        f"relay_log_tail={read_tail(relay_log)}; "
        f"tauri_driver_log_tail={read_tail(driver_log)}; "
        f"page_source_tail={page_source_tail}"
    )


def assert_model_path_exists(path: str) -> None:
    if not path.strip():
        raise AssertionError("model path is empty")
    if not Path(path).expanduser().exists():
        raise AssertionError(f"model path does not exist: {path}")


def resolve_real_e2e_model_path() -> Path:
    raw_path = os.environ.get("TOKENPLACE_REAL_E2E_MODEL_PATH", "").strip()
    if not raw_path:
        raise RuntimeError(
            "TOKENPLACE_REAL_E2E_MODEL_PATH must point to the provisioned CI tiny GGUF"
        )
    model_path = Path(raw_path).expanduser()
    if not model_path.is_absolute():
        raise RuntimeError("TOKENPLACE_REAL_E2E_MODEL_PATH must be an absolute path")
    if not model_path.is_file():
        raise RuntimeError(
            "TOKENPLACE_REAL_E2E_MODEL_PATH must point to an existing regular file"
        )
    if model_path.stat().st_size == 0:
        raise RuntimeError("TOKENPLACE_REAL_E2E_MODEL_PATH must point to a non-empty GGUF")
    return model_path


def wait_for_running_stability(
    driver: webdriver.Remote, expected: str, stable_seconds: float = 2.0,
    timeout_seconds: float = 45,
) -> None:
    status_xpath = "//p[contains(.,'Running:')]//strong"
    wait = WebDriverWait(driver, timeout_seconds, poll_frequency=0.25)
    wait.until(
        lambda d: d.find_element(By.XPATH, status_xpath).text.strip().lower() == expected.lower()
    )
    deadline = time.time() + stable_seconds
    while time.time() < deadline:
        try:
            current = driver.find_element(By.XPATH, status_xpath).text.strip().lower()
        except (NoSuchElementException, StaleElementReferenceException, WebDriverException):
            time.sleep(0.2)
            continue
        if current != expected.lower():
            raise AssertionError(
                f"Running state became unstable: expected {expected!r}, observed {current!r}"
            )
        time.sleep(0.2)


def landing_compute_node_status_matches(driver: webdriver.Remote, expected: str) -> bool:
    try:
        status = driver.find_element(By.CSS_SELECTOR, ".compute-node-status-label")
        return status.text.strip() == expected
    except (NoSuchElementException, StaleElementReferenceException):
        return False


def fill_input_by_label(driver: webdriver.Remote, label_text: str, value: str) -> None:
    locator = (
        f"(//label[normalize-space()='{label_text}']/following::input[1] | "
        f"//label[normalize-space()='{label_text}']/following::textarea[1])[1]"
    )

    def _set_value(_: webdriver.Remote) -> bool:
        try:
            with contextlib.suppress(WebDriverException):
                driver.switch_to.default_content()
            element = driver.find_element(By.XPATH, locator)
            driver.execute_script(
                """
                const el = arguments[0];
                const nextValue = arguments[1];
                el.focus();
                const proto = el.tagName === 'TEXTAREA'
                  ? HTMLTextAreaElement.prototype
                  : HTMLInputElement.prototype;
                const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
                descriptor.set.call(el, nextValue);
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.dispatchEvent(new Event('blur', { bubbles: true }));
                """,
                element,
                value,
            )
            return element.get_attribute("value") == value
        except (
            NoSuchElementException,
            NoSuchFrameException,
            StaleElementReferenceException,
            WebDriverException,
        ):
            return False

    if not WebDriverWait(driver, 45, poll_frequency=0.25).until(_set_value):
        raise RuntimeError(f"failed to set input for label: {label_text}")
    input_el = driver.find_element(By.XPATH, locator)
    assert input_el.get_attribute("value") == value


def wait_for_ui_ready(driver: webdriver.Remote, timeout_seconds: float = 45.0) -> None:
    recovery_attempts = 0
    last_recovery_at = 0.0

    def _ready(d: webdriver.Remote) -> bool:
        nonlocal recovery_attempts
        nonlocal last_recovery_at
        try:
            with contextlib.suppress(WebDriverException):
                d.switch_to.default_content()
            state = d.execute_script("return document.readyState")
            if state != "complete":
                return False
            model_label_ready = bool(
                d.find_elements(By.XPATH, "//label[normalize-space()='Model GGUF path']")
            )
            relay_input_ready = bool(
                d.find_elements(
                    By.XPATH,
                    "(//label[normalize-space()='Relay URL 1']/following::input[1])[1]",
                )
            )
            runtime_path_ready = bool(
                d.find_elements(
                    By.XPATH,
                    "//div[contains(normalize-space(),'Runtime resolved path:')]/code",
                )
            )
            if model_label_ready and relay_input_ready and runtime_path_ready:
                return True

            page_source = ""
            with contextlib.suppress(WebDriverException):
                page_source = d.page_source
            if (
                recovery_attempts < 4
                and "could not connect to localhost" in page_source.lower()
                and (time.time() - last_recovery_at) >= 1.0
            ):
                recovery_attempts += 1
                last_recovery_at = time.time()
                with contextlib.suppress(WebDriverException):
                    d.get("tauri://localhost/")
                with contextlib.suppress(WebDriverException):
                    d.get("tauri://localhost/index.html")
            return False
        except (
            NoSuchFrameException,
            StaleElementReferenceException,
            WebDriverException,
        ):
            return False

    if not WebDriverWait(driver, timeout_seconds, poll_frequency=0.25).until(_ready):
        raise RuntimeError("desktop UI never became ready")


def wait_for_inference_result(driver: webdriver.Remote, timeout_seconds: float = 45.0) -> str:
    wait = WebDriverWait(driver, timeout_seconds, poll_frequency=0.25)

    def _done_or_failed(d: webdriver.Remote) -> bool:
        status = d.find_element(By.XPATH, "//p[contains(.,'Status:')]//strong").text.strip().lower()
        output = d.find_element(By.XPATH, "//pre").text.strip()
        error_text = ""
        with contextlib.suppress(NoSuchElementException):
            error_text = d.find_element(By.XPATH, "//p[starts-with(normalize-space(),'Error:')]").text.strip()
        if status == "failed" or error_text:
            raise RuntimeError(
                f"inference failed early; status={status}; error={error_text}; output={output}"
            )
        return status == "completed" and bool(output)

    wait.until(_done_or_failed)
    output_text = driver.find_element(By.XPATH, "//pre").text.strip()
    last_error_text = driver.find_element(By.XPATH, "//p[contains(.,'Last error:')]").text.strip()
    for marker in ("model path not found", "bridge failure", "no module named", "importerror"):
        if marker in output_text.lower() or marker in last_error_text.lower():
            raise AssertionError(
                f"unexpected error marker `{marker}` seen; output={output_text}; last_error={last_error_text}"
            )
    return output_text


def read_runtime_resolved_path(driver: webdriver.Remote) -> str | None:
    with contextlib.suppress(NoSuchElementException, WebDriverException):
        runtime_path = driver.find_element(
            By.XPATH,
            "//div[contains(normalize-space(),'Runtime resolved path:')]/code",
        ).text.strip()
        return runtime_path or None
    return None


def wait_for_start_operator_enabled(
    driver: webdriver.Remote,
    relay_log: Path,
    driver_log: Path,
    timeout_seconds: float = 45.0,
) -> None:
    button_xpath = "//button[.='Start operator']"
    wait = WebDriverWait(driver, timeout_seconds, poll_frequency=0.25)
    try:
        wait.until(lambda d: d.find_element(By.XPATH, button_xpath).is_enabled())
    except TimeoutException as exc:
        model_value = ""
        relay_value = ""
        status_snippet = ""
        with contextlib.suppress(Exception):
            model_value = driver.find_element(
                By.XPATH,
                "(//label[normalize-space()='Model GGUF path']/following::input[1])[1]",
            ).get_attribute("value")
        with contextlib.suppress(Exception):
            relay_value = driver.find_element(
                By.XPATH,
                "(//label[normalize-space()='Relay URL 1']/following::input[1])[1]",
            ).get_attribute("value")
        with contextlib.suppress(Exception):
            status_snippet = " | ".join(
                p.text for p in driver.find_elements(By.XPATH, "//section//p")
            )
        raise RuntimeError(
            diagnostics_message(
                (
                    "Start operator remained disabled after filling inputs; "
                    f"model_input={model_value!r}; relay_input={relay_value!r}; "
                    f"status={status_snippet!r}"
                ),
                relay_log,
                driver_log,
                driver,
            )
        ) from exc


def assert_relay_roundtrip(
    relay_url: str,
    relay_log: Path,
    driver_log: Path,
    driver: webdriver.Remote,
    *,
    prompt_text: str = "say hello from mock",
) -> None:
    client = CryptoClient(relay_url, debug=True)
    deadline = time.time() + 45
    while time.time() < deadline:
        if client.fetch_server_public_key():
            break
        time.sleep(1)
    else:
        raise RuntimeError(
            diagnostics_message("failed to fetch server public key from relay", relay_log, driver_log, driver)
        )

    response = client.send_chat_message(prompt_text, max_retries=12)
    if not response:
        raise RuntimeError(
            diagnostics_message("no relay roundtrip response returned to client", relay_log, driver_log, driver)
        )
    response_text = " ".join(
        str(message.get("content", ""))
        for message in response
        if isinstance(message, dict)
    )
    if not response_text.strip():
        raise AssertionError(
            diagnostics_message("relay roundtrip response was empty", relay_log, driver_log, driver)
        )

    relay_text = relay_log.read_text(encoding="utf-8", errors="replace")
    for marker in (
        '"http_path": "/api/v1/relay/servers/next"',
        '"http_path": "/api/v1/relay/requests"',
        '"http_path": "/api/v1/relay/responses"',
        '"http_path": "/api/v1/relay/responses/retrieve"',
    ):
        if marker not in relay_text:
            raise AssertionError(
                diagnostics_message(
                    f"relay roundtrip missing expected marker {marker}",
                    relay_log,
                    driver_log,
                    driver,
                )
            )

    last_error_text = driver.find_element(By.XPATH, "//p[contains(.,'Last error:')]").text.lower()
    for marker in ("bridge failure", "no module named", "importerror", "model path not found"):
        if marker in last_error_text:
            raise AssertionError(
                diagnostics_message(
                    f"unexpected app error marker after relay roundtrip: {marker}",
                    relay_log,
                    driver_log,
                    driver,
                )
            )


def terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # Preserve original failure reason if process refuses to exit.
            pass


def tokenizer_handoff_args(request_path: Path | None = None,
        evidence_path: Path | None = None) -> list[str]:
    """Carry the paired handoff on the application command line on Windows.

    WebView2 starts the packaged application itself, so environment inherited by
    tauri-driver is not a deterministic application handoff on Windows.
    """
    if request_path is None and evidence_path is None:
        return []
    if request_path is None or evidence_path is None:
        raise ValueError("tokenizer request and evidence paths must be paired")
    return [
        f"--token-place-long-context-benchmark-tokenizer-request={request_path}",
        f"--token-place-long-context-benchmark-tokenizer-evidence={evidence_path}",
    ]


def tokenizer_stage_path(evidence_path: Path) -> Path:
    return Path(f"{evidence_path}.stage.json")


def _write_tokenizer_stage(evidence_path: Path, category: str, stage: int) -> None:
    """Publish a bounded stage marker without copying any application data."""
    allowed = {"python_producer_not_invoked"}
    if category not in allowed:
        raise ValueError("invalid tokenizer stage category")
    output = tokenizer_stage_path(evidence_path)
    fd, temporary = tempfile.mkstemp(prefix=".tokenizer-runner-stage-", suffix=".tmp",
        dir=output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "stage": stage, "category": category}, handle,
                sort_keys=True, separators=(",", ":"))
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _read_tokenizer_stage(evidence_path: Path) -> str:
    try:
        value = json.loads(tokenizer_stage_path(evidence_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("rust_python_handoff_failed") from exc
    if (not isinstance(value, dict) or set(value) != {"version", "stage", "category"}
            or value.get("version") != 1 or not isinstance(value.get("stage"), int)
            or not isinstance(value.get("category"), str)):
        raise RuntimeError("rust_python_handoff_failed")
    return value["category"]


def tauri_driver_environment(isolated_home: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "USE_MOCK_LLM",
        "TOKEN_PLACE_PYTHON",
        "TOKEN_PLACE_SIDECAR_PYTHON",
        "PYTHONPATH",
        "TOKEN_PLACE_LONG_CONTEXT_BENCHMARK_TOKENIZER_REQUEST",
        "TOKEN_PLACE_LONG_CONTEXT_BENCHMARK_TOKENIZER_EVIDENCE",
    ):
        env.pop(key, None)
    env.update(
        {
            "HOME": str(isolated_home),
            "XDG_CONFIG_HOME": str(isolated_home / ".config"),
            "XDG_DATA_HOME": str(isolated_home / ".local/share"),
            "APPDATA": str(isolated_home / "AppData/Roaming"),
        }
    )
    return env


def start_driver(app_binary: Path, *, application_args: list[str] | None = None
        ) -> webdriver.Remote:
    options = webdriver.ChromeOptions()
    options.set_capability("browserName", "wry")
    options.set_capability(
        "tauri:options",
        {
            "application": str(app_binary),
            "args": application_args or [],
        },
    )
    return webdriver.Remote(command_executor=WEBDRIVER_URL, options=options)


def start_landing_driver() -> webdriver.Chrome:
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    return webdriver.Chrome(options=options)


def wait_for_operator_log_stop_markers(
    relay_log: Path, driver_log: Path, timeout_seconds: float = 5.0
) -> str:
    deadline = time.monotonic() + timeout_seconds
    markers = (
        "desktop.compute_node_bridge.unregister.attempted",
        "desktop.compute_node_bridge.unregister.succeeded",
        "desktop.compute_node.bridge_process_exited",
    )
    last_log = ""
    while time.monotonic() < deadline:
        last_log = read_tail(relay_log) + read_tail(driver_log)
        attempted_index = last_log.find(markers[0])
        succeeded_index = last_log.find(markers[1])
        exited_index = last_log.find(markers[2])
        if (
            attempted_index >= 0
            and succeeded_index > attempted_index
            and exited_index > succeeded_index
        ):
            exited_line = next(
                (line for line in last_log[exited_index:].splitlines() if markers[2] in line),
                "",
            )
            if "killed=false" in exited_line:
                if "desktop.compute_node.bridge_kill_requested" in last_log:
                    raise AssertionError("unexpected bridge kill request in operator log")
                return last_log
        time.sleep(0.1)
    raise AssertionError(
        "timed out waiting for ordered unregister/exit markers; "
        f"operator_log_tail={last_log}"
    )


def tauri_driver_command() -> list[str]:
    tauri_driver_bin = shutil.which("tauri-driver")
    webkit_driver_bin = shutil.which("WebKitWebDriver") or shutil.which("webkit2gtk-driver")
    if webkit_driver_bin is None:
        for candidate in (
            Path("/usr/bin/WebKitWebDriver"),
            Path("/usr/bin/webkit2gtk-driver"),
            Path("/usr/libexec/webkit2gtk-4.1/WebKitWebDriver"),
            Path("/usr/libexec/webkit2gtk-4.0/WebKitWebDriver"),
        ):
            if candidate.exists() and os.access(candidate, os.X_OK):
                webkit_driver_bin = str(candidate)
                break
    if tauri_driver_bin is not None:
        command = [tauri_driver_bin, "--port", "4444"]
        if webkit_driver_bin is not None:
            command.extend(["--native-driver", webkit_driver_bin])
        return command
    raise RuntimeError(
        "tauri-driver binary not found on PATH; install it with `cargo install tauri-driver`"
    )


def _status_value(driver: webdriver.Remote, label: str) -> str:
    return driver.find_element(
        By.XPATH, f"//p[contains(.,'{label}:')]//*[self::code or self::strong][1]"
    ).text.strip()


def _readiness_diagnostics_map(driver: webdriver.Remote) -> dict[str, str]:
    text = _status_value(driver, "Readiness diagnostics")
    return dict(item.split("=", 1) for item in text.split() if "=" in item)


def _diagnostic_bool(diagnostics: dict[str, str], key: str) -> bool:
    value = diagnostics.get(key, "").lower()
    if value not in {"true", "false"}:
        raise RuntimeError("runtime_configuration_invalid")
    return value == "true"


def _diagnostic_int(diagnostics: dict[str, str], key: str) -> int:
    value = diagnostics.get(key, "")
    if not value.isdigit():
        raise RuntimeError("runtime_configuration_invalid")
    return int(value)


def _diagnostic_float(diagnostics: dict[str, str], key: str) -> float:
    try:
        value = float(diagnostics[key])
    except (KeyError, ValueError) as exc:
        raise RuntimeError("runtime_configuration_invalid") from exc
    if not math.isfinite(value):
        raise RuntimeError("runtime_configuration_invalid")
    return value


def _normalize_profile_fallback_reason(value: str | None) -> str:
    return "none" if value in {None, "", "null"} else value


def packaged_runtime_configuration(runtime: dict[str, str], diagnostics: dict[str, str],
        requested_backend: str) -> dict[str, object]:
    """Build bounded current-worker configuration evidence from UI-safe fields only."""
    profile_key = "api_v1_readiness_qwen_64k_runtime_profile_id"
    profile_id = diagnostics.get(profile_key)
    profile_applicable = profile_id not in {None, "", "null"}
    if profile_applicable:
        attempts = diagnostics.get(
            "api_v1_readiness_qwen_64k_runtime_profile_attempt_ids", "").split(",")
        fallback_reason = _normalize_profile_fallback_reason(diagnostics.get(
            "api_v1_readiness_qwen_64k_runtime_profile_fallback_reason"))
        profile = {"selected": profile_id,
            "preferred": diagnostics["api_v1_readiness_qwen_64k_runtime_preferred_profile_id"],
            "attempted": attempts,
            "recovery_count": _diagnostic_int(diagnostics,
                "api_v1_readiness_qwen_64k_runtime_profile_recovery_count"),
            "result": diagnostics["api_v1_readiness_qwen_64k_runtime_profile_result"],
            "fallback_reason": fallback_reason}
        batch = {"requested": diagnostics["api_v1_readiness_qwen_64k_batch_profile_requested"],
            "selected": diagnostics["api_v1_readiness_qwen_64k_batch_profile_selected"],
            "n_batch": _diagnostic_int(diagnostics,
                "api_v1_readiness_qwen_64k_runtime_profile_n_batch"),
            "n_ubatch": _diagnostic_int(diagnostics,
                "api_v1_readiness_qwen_64k_runtime_profile_n_ubatch")}
        kv_cache = {"precision": diagnostics["api_v1_readiness_qwen_64k_runtime_profile_kv_precision"],
            "type_k": _diagnostic_int(diagnostics,
                "api_v1_readiness_qwen_64k_runtime_profile_type_k"),
            "type_v": _diagnostic_int(diagnostics,
                "api_v1_readiness_qwen_64k_runtime_profile_type_v"),
            "device": diagnostics["kv_cache_device"]}
        offloaded = diagnostics["offloaded_layers"]
        acceleration = {"flash_attention": _diagnostic_bool(diagnostics,
                "api_v1_readiness_qwen_64k_runtime_profile_flash_attn"),
            "kqv_offload": _diagnostic_bool(diagnostics,
                "api_v1_readiness_qwen_64k_runtime_profile_offload_kqv"),
            "offloaded_layers": int(offloaded) if offloaded.isdigit() else offloaded}
    else:
        profile = batch = kv_cache = acceleration = yarn = {
            "status": "not_applicable", "reason": "not_qwen_64k_profile"}
    if profile_applicable:
        yarn = {
            "requested_context_tokens": _diagnostic_int(diagnostics,
                "api_v1_readiness_yarn_requested_context_tokens"),
            "original_context_tokens": _diagnostic_int(diagnostics,
                "api_v1_readiness_yarn_original_context_tokens"),
            "context_multiplier": _diagnostic_float(diagnostics,
                "api_v1_readiness_yarn_context_multiplier"),
            "rope_frequency_scale": _diagnostic_float(diagnostics,
                "api_v1_readiness_yarn_rope_freq_scale"),
            "extension_factor_overridden": _diagnostic_bool(diagnostics,
                "api_v1_readiness_yarn_ext_factor_overridden"),
            "scaling_source": diagnostics["api_v1_readiness_yarn_rope_scaling_type_source"],
            "configuration_valid": _diagnostic_bool(diagnostics,
                "api_v1_readiness_yarn_configuration_valid")}
    return {"mode": {"requested": runtime["Requested mode"].lower(),
            "effective": runtime["Effective mode"].lower()},
        "backend": {"requested": requested_backend,
            "available": runtime["Backend available"].lower(),
            "selected": runtime["Backend selected"].lower(),
            "used": runtime["Backend used"].lower(),
            "fallback_reason": runtime["Fallback reason"].lower()},
        "context": {"tier": runtime["Context tier"],
            "effective_window_tokens": int(runtime["Context window"].split()[0])},
        "runtime_profile": profile, "batch_profile": batch, "kv_cache": kv_cache,
        "acceleration": acceleration, "yarn_rope": yarn}


def assert_packaged_windows_nvidia_status(
    driver: webdriver.Remote, context_tier: str, pre_start_session_id: str
) -> None:
    """Fail closed unless Rust-managed UI status proves the current session's real CUDA worker.

    Every value comes from structured fields the Rust backend populates from the
    real runtime/model-load diagnostics boundary (compute_node.rs's
    ComputeNodeStatus and its readiness_diagnostics allowlist) -- never from
    unscoped page-source or log-file substring matching, which cannot
    distinguish current-session evidence from stale or fabricated text.
    """
    session_id = _status_value(driver, "Operator session ID")
    if not session_id or session_id.lower() in {"pending", "unknown", "none"}:
        raise AssertionError("hardware status is missing a concrete operator session ID")
    if session_id == pre_start_session_id:
        raise AssertionError(
            "hardware status operator session ID did not advance for this operator start; "
            "evidence may be from a prior session"
        )
    sequence_text = _status_value(driver, "Sequence")
    if not sequence_text.isdigit() or int(sequence_text) < 1:
        raise AssertionError(f"hardware status sequence is not a positive current-session value: {sequence_text!r}")

    expected = {
        "Requested mode": "gpu",
        "Backend available": "cuda",
        "Backend selected": "cuda",
        "Backend used": "cuda",
        "Context tier": context_tier,
        "Worker state": "ready",
        "Worker alive": "yes",
        "Fallback reason": "none",
        # Launcher source's real value is "bundled" (PythonLauncherSource::BundledRuntime
        # in compute_node.rs); this proves the active launcher is the installed
        # package's own runtime, not an environment override or dev interpreter.
        "Launcher source": "bundled",
    }
    observed = {label: _status_value(driver, label).lower() for label in expected}
    for label, value in expected.items():
        if observed[label] != value:
            raise AssertionError(f"hardware status {label}={observed[label]!r}, expected {value!r}")
    if _status_value(driver, "Interpreter").lower() != "python.exe":
        raise AssertionError("hardware gate did not use the bundled Windows interpreter")
    # Registered only becomes "yes..." once the relay round-trip actually succeeds,
    # which requires relay_runtime_state to have reached ready/processing; there is
    # no separate "Relay runtime state" UI field to read directly.
    registered = _status_value(driver, "Registered").lower()
    if not registered.startswith("yes"):
        raise AssertionError(f"hardware status relay registration was not ready: {registered!r}")

    runtime_id = _status_value(driver, "Runtime ID")
    bundled_runtime_id = _status_value(driver, "Bundled runtime ID")
    if not bundled_runtime_id or bundled_runtime_id.lower() in {"", "pending", "unknown"}:
        raise AssertionError("hardware status bundled runtime ID was not concrete")
    if runtime_id != bundled_runtime_id:
        raise AssertionError(
            f"hardware status active runtime {runtime_id!r} does not match "
            f"the installed bundled runtime {bundled_runtime_id!r}"
        )

    diagnostics = _readiness_diagnostics_map(driver)
    offloaded_layers = diagnostics.get("offloaded_layers", "")
    # Explicit GPU mode requests n_gpu_layers=-1 (ModelManager._resolve_compute_plan),
    # which ModelManager surfaces as the literal sentinel "all_supported_layers" rather
    # than a layer count; a real positive count is also accepted for hybrid-style
    # partial offload. Zero, negative, unknown, and arbitrary strings are rejected.
    is_full_offload_sentinel = offloaded_layers == "all_supported_layers"
    is_positive_layer_count = offloaded_layers.lstrip("-").isdigit() and int(offloaded_layers) > 0
    if not (is_full_offload_sentinel or is_positive_layer_count):
        raise AssertionError(f"hardware status reports non-positive or unrecognized GPU offload: {offloaded_layers!r}")
    kv_cache_device = diagnostics.get("kv_cache_device", "")
    if kv_cache_device != "cuda":
        raise AssertionError(f"hardware status reports KV cache device is not CUDA: {kv_cache_device!r}")


def _write_benchmark_phase(path: Path, phase: str, started: float,
        schema_version: str, phases: tuple[str, ...], *, last_safe_phase: str,
        failure_reason: str | None = None, cleanup_succeeded: bool | None = None,
        retry_timeout_s: float = 1.0, clock=time.monotonic,
        sleeper=time.sleep, platform_name: str | None = None) -> None:
    """Atomically checkpoint an allowlisted phase without identifiers or payload data."""
    payload = {"schema_version": schema_version, "phase": phase,
        "sequence": phases.index(phase) + 1,
        "last_safe_phase": last_safe_phase, "failure_reason": failure_reason,
        "elapsed_s": round(max(0.0, clock() - started), 3),
        "cleanup_succeeded": cleanup_succeeded}
    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    deadline = clock() + max(0.0, retry_timeout_s)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(temporary_fd, 0o600)
        with os.fdopen(temporary_fd, "w", encoding="utf-8") as handle:
            temporary_fd = -1
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        while True:
            try:
                temporary.replace(path)
                return
            except OSError as exc:
                if not _is_windows_checkpoint_contention(exc, platform_name):
                    raise
                remaining = deadline - clock()
                if remaining <= 0:
                    raise RuntimeError("phase checkpoint publication failed") from None
                sleeper(min(0.01, remaining))
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        while True:
            try:
                temporary.unlink(missing_ok=True)
                break
            except OSError as exc:
                if not _is_windows_checkpoint_contention(exc, platform_name):
                    raise
                remaining = deadline - clock()
                if remaining <= 0:
                    break
                sleeper(min(0.01, remaining))


def _is_windows_sharing_violation(exc: BaseException) -> bool:
    """Recognize only Windows sharing/lock violations, not generic access denial."""
    return isinstance(exc, OSError) and getattr(exc, "winerror", None) in {32, 33}


def _is_windows_checkpoint_contention(exc: BaseException,
        platform_name: str | None = None) -> bool:
    """Recognize contention only for the checkpoint's owned atomic operation."""
    platform_name = os.name if platform_name is None else platform_name
    return (_is_windows_sharing_violation(exc)
        or (platform_name == "nt" and isinstance(exc, PermissionError)))


def _remove_owned_path(path: Path, deadline: float, *, directory: bool = False,
        clock=time.monotonic, sleeper=time.sleep) -> bool:
    """Remove one owner-created path, retrying only transient sharing denials."""
    while True:
        try:
            if directory:
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
            return True
        except FileNotFoundError:
            return True
        except OSError as exc:
            if not _is_windows_sharing_violation(exc):
                raise
            remaining = deadline - clock()
            if remaining <= 0:
                return False
            sleeper(min(0.01, remaining))


def _cleanup_owned_process_tree(process: subprocess.Popen, remaining: Callable[[], float]) -> bool:
    """Boundedly stop and observe the exact PID-owned descendant tree."""
    cleanup_ok = True
    try:
        root = psutil.Process(process.pid)
        owned_processes = [*root.children(recursive=True), root]
    except psutil.NoSuchProcess:
        owned_processes = []
    except (psutil.AccessDenied, psutil.ZombieProcess):
        owned_processes = []
        cleanup_ok = False
    for owned in owned_processes:
        if remaining() <= 0:
            cleanup_ok = False
            break
        try:
            owned.terminate()
        except psutil.NoSuchProcess:
            pass
        except Exception:
            cleanup_ok = False
    alive = owned_processes
    allowance = remaining()
    if allowance <= 0 and alive:
        cleanup_ok = False
    elif alive:
        try:
            _, alive = psutil.wait_procs(alive, timeout=allowance)
        except Exception:
            cleanup_ok = False
    for owned in alive:
        if remaining() <= 0:
            cleanup_ok = False
            break
        try:
            owned.kill()
        except psutil.NoSuchProcess:
            pass
        except Exception:
            cleanup_ok = False
    allowance = remaining()
    if allowance <= 0 and alive:
        cleanup_ok = False
    elif alive:
        try:
            _, alive = psutil.wait_procs(alive, timeout=allowance)
        except Exception:
            cleanup_ok = False
    cleanup_ok = cleanup_ok and not alive
    allowance = remaining()
    if allowance <= 0:
        cleanup_ok = False
    else:
        try:
            process.wait(timeout=allowance)
        except Exception:
            cleanup_ok = False
    return cleanup_ok


def _quit_webdriver(session, timeout_s: float) -> bool:
    """Attempt to release a WebDriver even when configuring its timeout fails."""
    succeeded = True
    try:
        session.set_script_timeout(timeout_s)
    except Exception:
        succeeded = False
    try:
        session.quit()
    except Exception:
        succeeded = False
    return succeeded


def _wait_for_packaged_setup_condition(browser: webdriver.Chrome, setup_remaining,
        predicate, failure_reason: str, fail_closed):
    """Wait within setup and preserve the readiness-specific failure category."""
    try:
        return WebDriverWait(browser, setup_remaining(), poll_frequency=0.05).until(predicate)
    except (TimeoutException, RuntimeError):
        fail_closed(failure_reason)


def _prepare_packaged_landing_page(browser: webdriver.Chrome, setup_remaining,
        fail_closed, context_tier: str) -> None:
    """Verify the landing-page prerequisites within the setup allowance."""
    checks = (
        ("return Boolean(document.querySelector('#app').__vue__)", "vue_not_ready"),
        ("const v=document.querySelector('#app').__vue__; return Boolean(v.hasClientKeypair);",
            "client_keypair_not_ready"),
        ("const v=document.querySelector('#app').__vue__; return Boolean(v.modelsLoaded && v.selectedModel);",
            "model_selection_not_ready"),
    )
    for script, reason in checks:
        _wait_for_packaged_setup_condition(browser, setup_remaining,
            lambda d, probe=script: d.execute_script(probe), reason, fail_closed)
    setup_remaining()
    if apply_benchmark_context_tier(browser, context_tier) != context_tier:
        fail_closed("requested_context_tier_not_applied")


def _validate_packaged_failure_reason(reason: str) -> str:
    if reason not in PACKAGED_FAILURE_REASONS:
        raise RuntimeError("invalid packaged failure reason")
    return reason


def _enter_packaged_prompt(field, prompt: str, *, action_factory=ActionChains) -> None:
    """Type through the textarea, encoding embedded newlines as Shift+Enter."""
    lines = prompt.split("\n")
    for index, line in enumerate(lines):
        if line:
            field.send_keys(line)
        if index < len(lines) - 1:
            (action_factory(field.parent).key_down(Keys.SHIFT).send_keys(Keys.ENTER)
                .key_up(Keys.SHIFT).perform())


def _populate_and_submit_packaged_prompt(browser: webdriver.Chrome, prompt: str,
        setup_remaining, fail_closed, write_phase, *, clock=time.monotonic,
        action_factory=ActionChains, before_submit=None) -> float:
    """Populate exactly, then check eligibility and explicitly submit."""
    setup_remaining()
    field = browser.find_element(By.CSS_SELECTOR, ".message-input")
    setup_remaining()
    _enter_packaged_prompt(field, prompt, action_factory=action_factory)
    setup_remaining()
    populated = browser.execute_script(
        "return document.querySelector('#app').__vue__.newMessage;")
    if populated != prompt:
        fail_closed("message_input_not_populated")
    send_button = _wait_for_packaged_setup_condition(
        browser, setup_remaining,
        lambda d: (button if (button := d.find_element(
            By.CSS_SELECTOR, ".send-button")).is_enabled() else False),
        "send_button_not_enabled", fail_closed)
    write_phase("landing_page_ready")
    setup_remaining()
    if before_submit is not None:
        before_submit()
    started = clock()
    send_button.click()
    write_phase("request_active")
    return started


def _read_primary_tokenizer_observation(evidence_path: Path, runtime_identity: str,
        fixture_sha256: str) -> dict[str, object]:
    try:
        observation = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("authoritative_target_depth_unavailable") from exc
    if (not isinstance(observation, dict)
            or observation.get("runtime_identity") != runtime_identity
            or observation.get("fixture_sha256") != fixture_sha256):
        raise RuntimeError("authoritative_target_depth_mismatched")
    return observation


def run_long_context_packaged_mode(request_path: Path, evidence_path: Path,
        phase_status_path: Path, app_binary: Path) -> int:
    """Drive a packaged app and the existing landing-page API v1 E2EE client."""
    request = json.loads(request_path.read_text(encoding="utf-8"))
    phase_schema_version = request["phase_status_version"]
    phase_values = request["phase_status_phases"]
    if (not isinstance(phase_schema_version, str) or not phase_schema_version
            or not isinstance(phase_values, list) or not phase_values
            or any(not isinstance(value, str) or not value for value in phase_values)
            or len(set(phase_values)) != len(phase_values)):
        raise RuntimeError("packaged phase contract malformed")
    phases = tuple(phase_values)
    runner_started = time.monotonic()
    last_safe_phase = "runner_startup"
    failure_reason: str | None = None
    def write_phase(phase: str) -> None:
        nonlocal last_safe_phase
        last_safe_phase = phase
        _write_benchmark_phase(phase_status_path, phase, runner_started,
            phase_schema_version, phases, last_safe_phase=last_safe_phase)
    def fail_closed(reason: str) -> None:
        nonlocal failure_reason
        failure_reason = _validate_packaged_failure_reason(reason)
        raise RuntimeError(reason)
    setup_deadline = runner_started + float(request["setup_timeout_s"])
    def setup_remaining() -> float:
        remaining = setup_deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("packaged setup timeout")
        return remaining
    write_phase("runner_startup")
    cleanup_timeout = float(request["cleanup_timeout_s"])
    driver_log_fd, driver_log_name = tempfile.mkstemp(prefix="long-context-tauri-driver-", suffix=".log")
    os.close(driver_log_fd)
    driver_log = Path(driver_log_name)
    isolated_home = Path(tempfile.mkdtemp(prefix="long-context-desktop-home-"))
    tokenizer_dir = Path(tempfile.mkdtemp(prefix="long-context-tokenizer-observation-"))
    tokenizer_request = tokenizer_dir / "request.json"
    tokenizer_evidence = tokenizer_dir / "evidence.json"
    tokenizer_request.write_text(json.dumps({
        "fixture_sha256": request["manifest"]["fixture_sha256"],
        "target_prefix_utf8_bytes": {name: target["target_prefix_utf8_bytes"]
            for name, target in request["manifest"]["targets"].items()},
    }), encoding="utf-8")
    if hasattr(os, "chmod"):
        os.chmod(tokenizer_request, 0o600)
    env = tauri_driver_environment(isolated_home)
    driver_log_handle = driver_log.open("w", encoding="utf-8")
    process: subprocess.Popen[str] | None = None
    driver: webdriver.Remote | None = None
    browser: webdriver.Chrome | None = None
    cleanup_ok = True
    primary_failed = False
    try:
        setup_remaining()
        process = subprocess.Popen(tauri_driver_command(), cwd=TAURI_ROOT, env=env,
            stdout=driver_log_handle, stderr=subprocess.STDOUT, text=True)  # noqa: S603
        memory_sampler = OwnedProcessTreeMemorySampler(process.pid)
        wait_for_port("127.0.0.1", 4444, process, "tauri-driver", driver_log,
            min(90, setup_remaining()))
        write_phase("webdriver_ready")
        setup_remaining()
        driver = start_driver(
            app_binary.resolve(strict=True),
            application_args=tokenizer_handoff_args(tokenizer_request, tokenizer_evidence),
        )
        wait_for_ui_ready(driver, timeout_seconds=setup_remaining())
        write_phase("desktop_ready")
        setup_remaining()
        fill_input_by_label(driver, "Model GGUF path", str(Path(request["model"]).resolve(strict=True)))
        setup_remaining()
        fill_input_by_label(driver, "Relay URL 1", request["relay_url"])
        setup_remaining()
        mode = driver.find_element(By.XPATH, "//label[normalize-space()='Compute mode']/following::select[1]")
        compute_mode = benchmark_operator_mode(request["backend"])
        driver.execute_script("arguments[0].value=arguments[1]; arguments[0].dispatchEvent(new Event('change',{bubbles:true}));", mode, compute_mode)
        tier = driver.find_element(By.XPATH, "//select[@aria-label='Context tier']")
        driver.execute_script("arguments[0].value=arguments[1]; arguments[0].dispatchEvent(new Event('change',{bubbles:true}));", tier, request["context_tier"])
        wait_for_start_operator_enabled(driver, driver_log, driver_log,
            timeout_seconds=setup_remaining())
        setup_remaining()
        driver.find_element(By.XPATH, "//button[.='Start operator']").click()
        wait_for_running_stability(driver, "yes", stable_seconds=3,
            timeout_seconds=setup_remaining())
        WebDriverWait(driver, setup_remaining()).until(
            lambda d: _status_value(d, "Registered").lower().startswith("yes"))
        write_phase("operator_ready")

        if _read_tokenizer_stage(tokenizer_evidence) != "python_handoff_received":  # pragma: no cover - exercised by packaged-app E2E
            fail_closed("rust_python_handoff_failed")  # pragma: no cover - exercised by packaged-app E2E

        runtime = {label: _status_value(driver, label) for label in
            ("App version", "Build ID", "Runtime ID", "Bundled runtime ID", "Launcher source",
             "Requested mode", "Effective mode", "Backend available", "Backend selected",
             "Backend used", "Fallback reason", "Context tier", "Context window")}
        diagnostics = _readiness_diagnostics_map(driver)
        runtime_configuration = packaged_runtime_configuration(runtime, diagnostics, request["backend"])
        if (runtime["Launcher source"].lower() != "bundled"):
            raise RuntimeError("packaged launcher attestation failed")
        if runtime["Backend selected"].lower() != request["backend"] or runtime["Backend used"].lower() != request["backend"]:
            raise RuntimeError("packaged backend attestation failed")

        setup_remaining()
        browser = start_landing_driver()
        browser.set_page_load_timeout(setup_remaining())
        browser.set_script_timeout(setup_remaining())
        browser.get(request["relay_url"])
        _prepare_packaged_landing_page(
            browser, setup_remaining, fail_closed, request["context_tier"])
        if not memory_sampler.sample():
            raise RuntimeError("memory_sample_unavailable")
        browser.set_script_timeout(setup_remaining())
        browser.execute_script("""
            const v = document.querySelector('#app').__vue__;
            const original = v.encrypt.bind(v);
            v.encrypt = async function(plaintext, ...args) {
                const envelope = JSON.parse(plaintext);
                if (envelope.protocol === 'tokenplace_api_v1_relay_e2ee') {
                    const options = envelope.api_v1_request?.options;
                    const allowed = ['max_tokens', 'temperature', 'top_p', 'seed'];
                    if (!options || typeof options !== 'object' || Array.isArray(options)) {
                        this.__longContextBenchmarkGenerationSettings = null;
                    } else {
                        const supplied = {};
                        for (const key of Object.keys(options)) {
                            if (allowed.includes(key)) supplied[key] = options[key];
                            else supplied.__unsupported__ = key;
                        }
                        this.__longContextBenchmarkGenerationSettings = {
                            supplied,
                            omitted_runtime_default: allowed.filter(key => !(key in options)).sort()
                        };
                    }
                }
                return original(plaintext, ...args);
            };
            const originalDecrypt = v.decrypt.bind(v);
            v.decrypt = async function(...args) {
                const plaintext = await originalDecrypt(...args);
                try {
                    const envelope = JSON.parse(plaintext);
                    const response = envelope?.api_v1_response;
                    if (envelope?.protocol === 'tokenplace_api_v1_relay_e2ee' &&
                            response && typeof response === 'object' && !Array.isArray(response)) {
                        const usage = response.usage;
                        const choice = Array.isArray(response.choices) ? response.choices[0] : null;
                        this.__longContextBenchmarkFinalMetadata = {
                            prompt_tokens: usage?.prompt_tokens,
                            completion_tokens: usage?.completion_tokens,
                            finish_reason: response.finish_reason ?? choice?.finish_reason
                        };
                    }
                } catch (_error) {
                    // Progress envelopes and non-JSON plaintext are intentionally ignored.
                }
                return plaintext;
            };
        """)
        # The child writes directly to this file descriptor. Capture the exact byte
        # boundary immediately before submission.
        driver_log_boundary = 0  # pragma: no cover - exercised by packaged-app E2E
        def capture_driver_log_boundary() -> None:
            nonlocal driver_log_boundary
            _write_tokenizer_stage(tokenizer_evidence, "python_producer_not_invoked", 35)  # pragma: no cover - exercised by packaged-app E2E
            driver_log_boundary = driver_log.stat().st_size
        started = _populate_and_submit_packaged_prompt(browser, request["prompt"],
            setup_remaining, fail_closed, write_phase,
            before_submit=capture_driver_log_boundary)
        progress: list[dict[str, object]] = []
        while time.monotonic() - started < float(request["request_timeout_s"]):
            memory_sampler.sample()
            state = browser.execute_script(
                "const v=document.querySelector('#app').__vue__; return {p:v.relayProgress,h:v.chatHistory,"
                "b:v.isGeneratingResponse,t:v.selectedContextTier};")
            event = state.get("p")
            if isinstance(event, dict) and (not progress or event.get("sequence") != progress[-1].get("sequence")):
                progress.append(event)
            lifecycle, response_text = classify_benchmark_landing_state(state)
            if lifecycle == "completed":
                break
            if lifecycle == "failed":
                raise RuntimeError("packaged_response_error")
            time.sleep(0.05)
        else:
            raise RuntimeError("packaged request timeout")
        ended = time.monotonic()
        write_phase("response_received")
        with driver_log.open("rb") as telemetry_handle:
            telemetry_handle.seek(driver_log_boundary)
            primary_log_slice = telemetry_handle.read().decode("utf-8", errors="replace")
        local_telemetry = parse_packaged_local_telemetry(primary_log_slice)
        # Retain only allowlisted final metadata at the decryption boundary.
        response_metadata = browser.execute_script(
            "return document.querySelector('#app').__vue__.__longContextBenchmarkFinalMetadata;")
        cancellation_recovery = None
        finalization_deadline = time.monotonic() + float(request["finalization_timeout_s"])
        def finalization_remaining() -> float:
            remaining = finalization_deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("packaged evidence finalization timeout")
            return remaining
        finalization_remaining()
        if not progress or not isinstance(response_text, str):
            raise RuntimeError("required encrypted progress or response evidence missing")
        browser.set_script_timeout(finalization_remaining())
        generation_settings = browser.execute_script(
            "return document.querySelector('#app').__vue__.__longContextBenchmarkGenerationSettings;")
        if not isinstance(generation_settings, dict):
            raise RuntimeError("generation_settings_unavailable")
        known_sequence = int(progress[-1]["sequence"])
        def post_terminal_poll() -> dict[str, object] | None:
            nonlocal known_sequence
            state = browser.execute_script(
                "const v=document.querySelector('#app').__vue__; return {p:v.relayProgress,h:v.chatHistory,b:v.isGeneratingResponse};")
            event = state.get("p")
            if isinstance(event, dict) and isinstance(event.get("sequence"), int) and event["sequence"] > known_sequence:
                known_sequence = event["sequence"]
                return event
            lifecycle, later_response = classify_benchmark_landing_state(state)
            if lifecycle == "completed" and later_response != response_text:
                return {"kind": "unexpected_result"}
            if lifecycle == "failed":
                return {"kind": "unexpected_failure"}
            return None
        finalization_remaining()
        post_terminal = [item for item in observe_post_terminal(post_terminal_poll,
            window_s=min(0.1, finalization_remaining())) if item is not None]
        finalization_remaining()
        stage_category = _read_tokenizer_stage(tokenizer_evidence)  # pragma: no cover - exercised by packaged-app E2E
        if stage_category != "authoritative_evidence_published":  # pragma: no cover - exercised by packaged-app E2E
            fail_closed(stage_category if stage_category in PACKAGED_FAILURE_REASONS  # pragma: no cover - exercised by packaged-app E2E
                else "python_producer_not_invoked")
        tokenizer_observation = _read_primary_tokenizer_observation(
            tokenizer_evidence, runtime["Runtime ID"], request["manifest"]["fixture_sha256"])
        finalization_remaining()
        memory_sampler.sample()
        memory_evidence = memory_sampler.summary()
        finalization_remaining()
        # All packaged requests share this evidence path, so freeze the primary
        # snapshots before cancellation/recovery traffic can overwrite them.
        if request.get("cancellation_validation"):
            write_phase("cancellation_validation")
            cancellation_deadline = time.monotonic() + float(request["cancellation_timeout_s"])
            cancellation_recovery, finalization_deadline = start_phase_after(
                lambda: run_long_context_cancellation_recovery(
                    browser, driver, request, cancellation_deadline),
                float(request["finalization_timeout_s"]))
        finalization_remaining()
        write_phase("evidence_finalization")
        digest = hashlib.sha256()
        with Path(request["model"]).open("rb") as model_handle:
            for chunk in iter(lambda: model_handle.read(1024 * 1024), b""):
                digest.update(chunk)
                finalization_remaining()
        evidence = {"app_identity": runtime["App version"], "build_identity": runtime["Build ID"],
            "runtime_identity": runtime["Runtime ID"], "bundled_runtime_identity": runtime["Bundled runtime ID"],
            "backend_requested": request["backend"],
            "backend_selected": runtime["Backend selected"].lower(),
            "backend_used": runtime["Backend used"].lower(), "model_fingerprint": digest.hexdigest(),
            "authoritative_prompt_tokens": tokenizer_observation["total_prompt_tokens"],
            "local_telemetry": local_telemetry, "progress_events": progress,
            "response_metadata": response_metadata,
            "authoritative_tokenizer_evidence": tokenizer_observation,
            "kv_applicability": tokenizer_observation.get("kv_applicability"),
            "kv_estimate": tokenizer_observation.get("kv_estimator"),
            "kv_runtime": tokenizer_observation.get("kv_runtime"),
            "atomic_response_completed": True,
            "post_terminal_observations": post_terminal, "response_text": response_text,
            "generation_settings": generation_settings,
            "memory": memory_evidence,
            "runtime_configuration": runtime_configuration,
            "request_duration_s": ended - started}
        if cancellation_recovery is not None:
            evidence["cancellation_recovery"] = cancellation_recovery
        finalization_remaining()
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
        finalization_remaining()
        os.chmod(evidence_path, 0o600)
        return 0
    except Exception:
        primary_failed = True
        if failure_reason is None:
            failure_reason = "packaged_runner_failure"
        raise
    finally:
        cleanup_deadline = time.monotonic() + cleanup_timeout
        def cleanup_remaining() -> float:
            return max(0.0, cleanup_deadline - time.monotonic())
        def cleanup_allowance() -> float | None:
            remaining = cleanup_remaining()
            return remaining if remaining > 0 else None
        checkpoint_error = None
        try:
            _write_benchmark_phase(phase_status_path, "cleanup", runner_started,
                phase_schema_version, phases, last_safe_phase=last_safe_phase,
                failure_reason=failure_reason, cleanup_succeeded=False,
                retry_timeout_s=min(cleanup_remaining(), 1.0))
        except Exception as exc:
            checkpoint_error = exc
        if browser is not None:
            allowance = cleanup_allowance()
            cleanup_ok = (allowance is not None
                and _quit_webdriver(browser, allowance) and cleanup_ok)
        if driver is not None:
            allowance = cleanup_allowance()
            cleanup_ok = (allowance is not None
                and _quit_webdriver(driver, allowance) and cleanup_ok)
        if process is not None:
            cleanup_ok = (_cleanup_owned_process_tree(process, cleanup_remaining)
                and cleanup_ok)
        try:
            driver_log_handle.close()
        except Exception:
            cleanup_ok = False
        for owned_path, is_directory in (
                (isolated_home, True), (tokenizer_dir, True), (driver_log, False)):
            try:
                cleanup_ok = (_remove_owned_path(owned_path, cleanup_deadline,
                    directory=is_directory) and cleanup_ok)
            except OSError:
                cleanup_ok = False
        cleanup_ok = cleanup_ok and checkpoint_error is None
        if not cleanup_ok and not primary_failed:
            failure_reason = "cleanup_failure"
        final_checkpoint_error = None
        try:
            _write_benchmark_phase(phase_status_path, "cleanup", runner_started,
                phase_schema_version, phases, last_safe_phase=last_safe_phase,
                failure_reason=failure_reason, cleanup_succeeded=cleanup_ok,
                retry_timeout_s=max(0.0, cleanup_deadline - time.monotonic()))
        except Exception as exc:
            final_checkpoint_error = exc
        cleanup_failure = (not cleanup_ok or checkpoint_error is not None
            or final_checkpoint_error is not None)
        if final_checkpoint_error is not None:
            print("cleanup phase checkpoint failed", file=sys.stderr)
        if cleanup_failure and not primary_failed:
            raise RuntimeError("cleanup_failure")


def _long_context_followup_request(browser: webdriver.Chrome, timeout_s: float,
        remaining: Callable[[], float]) -> tuple[bool, float]:
    """Exercise the ordinary encrypted request lifecycle without retaining its plaintext result."""
    browser.execute_script("const v=document.querySelector('#app').__vue__; v.chatHistory=[];")
    field = browser.find_element(By.CSS_SELECTOR, ".message-input")
    field.clear()
    field.send_keys("Reply with exactly OK")
    started = time.monotonic()
    browser.find_element(By.CSS_SELECTOR, ".send-button").click()
    wait = WebDriverWait(browser, min(timeout_s, remaining()), poll_frequency=0.05)
    state = wait.until(lambda d: (lambda s: s if classify_benchmark_landing_state(s)[0] != "running" else False)(
        d.execute_script("const v=document.querySelector('#app').__vue__; return {p:v.relayProgress,h:v.chatHistory,b:v.isGeneratingResponse};")))
    lifecycle, _response = classify_benchmark_landing_state(state)
    return lifecycle == "completed", time.monotonic() - started


def run_long_context_cancellation_recovery(browser: webdriver.Chrome, driver: webdriver.Remote,
        request: dict[str, object], cancellation_deadline: float) -> dict[str, object]:
    """Physically cancel prefill/generation requests, recover, then restart the operator."""
    config = request["cancellation"]
    assert isinstance(config, dict)
    timeout_s = float(request["request_timeout_s"])
    observation_s = float(config["observation_window_s"])
    recovery_s = float(config["recovery_timeout_s"])
    def cancellation_remaining(cap: float | None = None) -> float:
        return packaged_phase_remaining(cancellation_deadline,
            "packaged cancellation validation timeout", cap=cap)
    scenarios: list[dict[str, object]] = []
    for phase in ("prefill", "generating"):
        cancellation_remaining()
        browser.execute_script("const v=document.querySelector('#app').__vue__; v.chatHistory=[];")
        field = browser.find_element(By.CSS_SELECTOR, ".message-input")
        field.clear()
        field.send_keys(str(request["prompt"]))
        browser.find_element(By.CSS_SELECTOR, ".send-button").click()
        deadline = time.monotonic() + min(timeout_s, cancellation_remaining())
        threshold = int(config["generation_tokens"]) if phase == "generating" else config.get("prefill_tokens")
        trigger_count = -1
        last_sequence = -1
        authoritative_total = None
        while time.monotonic() < deadline:
            state = browser.execute_script(
                "const v=document.querySelector('#app').__vue__; return {p:v.relayProgress,b:v.isGeneratingResponse};")
            event = state.get("p") if isinstance(state, dict) else None
            if isinstance(event, dict):
                total = event.get("total_prompt_tokens")
                if not isinstance(total, int) or isinstance(total, bool) or total <= 0:
                    raise RuntimeError("cancellation_trigger_missed")
                if authoritative_total is None:
                    authoritative_total = total
                elif total != authoritative_total:
                    raise RuntimeError("cancellation_trigger_missed")
                if phase == "prefill" and threshold is None and isinstance(total, int):
                    threshold = max(1, int(total * float(config["prefill_fraction"])))
                count = event.get("processed_prompt_tokens") if phase == "prefill" else event.get("generated_tokens")
                trigger_state = (prefill_cancellation_trigger_state(count, threshold, total)
                    if phase == "prefill" and event.get("phase") == phase else None)
                if trigger_state in {"completed", "invalid"}:
                    raise RuntimeError("cancellation_trigger_missed")
                if event.get("phase") == phase and isinstance(count, int) and isinstance(threshold, int) and (
                        trigger_state == "trigger" if phase == "prefill" else count >= threshold):
                    trigger_count = count
                    last_sequence = int(event.get("sequence", -1))
                    break
                if (phase == "prefill" and event.get("phase") == "generating") or state.get("b") is False:
                    raise RuntimeError("cancellation_trigger_missed")
            time.sleep(min(0.01, cancellation_remaining()))
        if trigger_count < 0 or not isinstance(threshold, int) or authoritative_total is None:
            raise RuntimeError("cancellation_trigger_missed")
        triggered = time.monotonic()
        browser.set_script_timeout(cancellation_remaining(recovery_s))
        acknowledgement = browser.execute_async_script("""
            const done=arguments[arguments.length-1]; const v=document.querySelector('#app').__vue__;
            const active=v.activeRelayRequest; const pending=v.cancelRelayRequest('requester_cancelled');
            v.terminateRelayRequestLocally(active);
            Promise.resolve(pending).then((result) => { v.clearActiveRelayRequest(active?.requestId); done(result); })
                .catch(() => { v.clearActiveRelayRequest(active?.requestId); done(null); });
        """)
        attempted = isinstance(acknowledgement, dict) and acknowledgement.get("attempted") is True
        acknowledged = isinstance(acknowledgement, dict) and acknowledgement.get("confirmed") is True
        stale = late = 0
        active_after = False
        quiet_started = time.monotonic()
        quiet_deadline = quiet_started + min(observation_s, cancellation_remaining())
        while time.monotonic() < quiet_deadline:
            state = browser.execute_script(
                "const v=document.querySelector('#app').__vue__; return {p:v.relayProgress,b:v.isGeneratingResponse,a:Boolean(v.activeRelayRequest),h:v.chatHistory};")
            event = state.get("p") if isinstance(state, dict) else None
            if isinstance(event, dict) and int(event.get("sequence", -1)) > last_sequence:
                stale += 1
            lifecycle, _response = classify_benchmark_landing_state(state)
            if lifecycle == "completed": late += 1
            active_after = bool(state.get("a") or state.get("b"))
            time.sleep(min(0.01, cancellation_remaining()))
        quiescence_s = time.monotonic() - quiet_started
        cleanup_s = time.monotonic() - triggered
        followup_ok, followup_s = _long_context_followup_request(
            browser, recovery_s, cancellation_remaining)
        scenarios.append({"phase": phase, "trigger_observed": True, "trigger_count": trigger_count,
            "threshold": threshold, "total_prompt_tokens": authoritative_total,
            "attempted": attempted, "acknowledged": acknowledged,
            "cleanup_s": cleanup_s, "quiescence_s": quiescence_s,
            "stale_progress_count": stale, "late_result_count": late,
            "active_after_quiescence": active_after, "followup_ok": followup_ok,
            "followup_s": followup_s})
    old_session = _status_value(driver, "Operator session ID")
    restarted = time.monotonic()
    driver.find_element(By.XPATH, "//button[.='Stop operator']").click()
    WebDriverWait(driver, cancellation_remaining(recovery_s)).until(
        lambda d: d.find_element(By.XPATH, "//button[.='Start operator']").is_enabled())
    stop_confirmed = _status_value(driver, "Worker alive").lower() != "yes"
    driver.find_element(By.XPATH, "//button[.='Start operator']").click()
    wait_for_running_stability(driver, "yes", stable_seconds=1,
        timeout_seconds=cancellation_remaining(recovery_s))
    WebDriverWait(driver, cancellation_remaining(recovery_s)).until(
        lambda d: _status_value(d, "Registered").lower().startswith("yes"))
    new_session = _status_value(driver, "Operator session ID")
    restart_s = time.monotonic() - restarted
    followup_ok, followup_s = _long_context_followup_request(
        browser, recovery_s, cancellation_remaining)
    return {"scenarios": scenarios, "operator_lifecycle": {"stop_confirmed": stop_confirmed,
        "restart_ready": True, "session_changed": bool(old_session and new_session != old_session),
        "restart_s": restart_s, "post_restart_followup_ok": followup_ok,
        "post_restart_followup_s": followup_s}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packaged-windows-nvidia-hardware", action="store_true")
    parser.add_argument("--app-binary", type=Path)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--context-tier", choices=("8k-fast", "64k-full"), default="8k-fast")
    parser.add_argument("--benchmark-request", type=Path)
    parser.add_argument("--benchmark-evidence", type=Path)
    parser.add_argument("--benchmark-phase-status", type=Path)
    args = parser.parse_args(argv)
    if args.benchmark_request or args.benchmark_evidence or args.benchmark_phase_status:
        if not (args.benchmark_request and args.benchmark_evidence
                and args.benchmark_phase_status and args.app_binary):
            parser.error("long-context benchmark mode requires request, evidence, phase status, and app binary")
        return run_long_context_packaged_mode(args.benchmark_request, args.benchmark_evidence,
            args.benchmark_phase_status, args.app_binary)
    hardware_mode = args.packaged_windows_nvidia_hardware
    if hardware_mode and (args.app_binary is None or args.model is None):
        parser.error("packaged Windows NVIDIA mode requires --app-binary and --model")
    relay_port = reserve_free_port()
    relay_url = f"http://127.0.0.1:{relay_port}"

    logs_dir = LOGS_DIR
    relay_log = logs_dir / "relay.log"
    driver_log = logs_dir / "tauri-driver.log"

    env = os.environ.copy()
    if hardware_mode:
        for key in (
            "USE_MOCK_LLM", "TOKEN_PLACE_PYTHON", "TOKEN_PLACE_SIDECAR_PYTHON",
            "PYTHONPATH", "TOKEN_PLACE_DESKTOP_ENABLE_RUNTIME_BOOTSTRAP",
        ):
            env.pop(key, None)
    else:
        env["USE_MOCK_LLM"] = "1"
    # This harness is a confirmed DevSourceTree launch, so provide the explicit
    # interpreter override required by the fail-closed launcher policy without
    # restoring PATH probing for packaged/runtime launches.
    if not hardware_mode:
        env["TOKEN_PLACE_PYTHON"] = sys.executable
        env["TOKEN_PLACE_SIDECAR_PYTHON"] = sys.executable
    env["TOKEN_PLACE_API_V1_RELAY_SERVER_LEASE_SECONDS"] = "120"
    if not hardware_mode:
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            f"{REPO_ROOT}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else str(REPO_ROOT)
        )
    isolated_home = Path(tempfile.mkdtemp(prefix="token-place-desktop-e2e-home-"))
    env["HOME"] = str(isolated_home)
    env["XDG_CONFIG_HOME"] = str(isolated_home / ".config")
    env["XDG_DATA_HOME"] = str(isolated_home / ".local" / "share")
    env["APPDATA"] = str(isolated_home / "AppData" / "Roaming")
    Path(env["XDG_CONFIG_HOME"]).mkdir(parents=True, exist_ok=True)
    Path(env["XDG_DATA_HOME"]).mkdir(parents=True, exist_ok=True)
    Path(env["APPDATA"]).mkdir(parents=True, exist_ok=True)

    relay = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            str(REPO_ROOT / "relay.py"),
            "--host",
            "127.0.0.1",
            "--port",
            str(relay_port),
            *([] if hardware_mode else ["--use_mock_llm"]),
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=relay_log.open("w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
        text=True,
    )

    tauri_driver = subprocess.Popen(  # noqa: S603
        tauri_driver_command(),
        # Keep cwd aligned with src-tauri so runtime asset resolution for ../dist works
        # when the app starts under tauri-driver in CI.
        cwd=TAURI_ROOT,
        env=env,
        stdout=driver_log.open("w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
        text=True,
    )

    driver: webdriver.Remote | None = None
    landing_driver: webdriver.Chrome | None = None
    model_path = args.model.resolve(strict=True) if hardware_mode else resolve_real_e2e_model_path()
    try:
        wait_for_http_200(f"{relay_url}/livez")
        ensure_alive(relay, "relay")

        wait_for_port(
            "127.0.0.1",
            4444,
            process=tauri_driver,
            process_label="tauri-driver",
            process_log=driver_log,
            timeout_seconds=90,
        )
        ensure_alive(tauri_driver, "tauri-driver")

        suffix = ".exe" if sys.platform == "win32" else ""
        app_binary = args.app_binary.resolve(strict=True) if hardware_mode else (
            TAURI_ROOT / "target" / "debug" / f"token-place-desktop-tauri{suffix}"
        )
        if not app_binary.exists():
            raise RuntimeError(f"missing desktop binary: {app_binary}")

        driver = start_driver(app_binary)
        wait = WebDriverWait(driver, 45)
        wait_for_ui_ready(driver)

        runtime_resolved_path = read_runtime_resolved_path(driver)
        initial_model_value = driver.find_element(
            By.XPATH,
            "(//label[normalize-space()='Model GGUF path']/following::input[1])[1]",
        ).get_attribute("value")
        assert initial_model_value == "", (
            f"expected first-launch model path to be blank; got {initial_model_value!r}"
        )
        if runtime_resolved_path:
            # Capture for diagnostics, but keep the provisioned path authoritative.
            print(f"Runtime resolved path (not used as primary test path): {runtime_resolved_path}")
        fill_input_by_label(driver, "Model GGUF path", str(model_path))
        model_input = driver.find_element(
            By.XPATH,
            "(//label[normalize-space()='Model GGUF path']/following::input[1])[1]",
        )
        assert model_input.get_attribute("value") == str(model_path)
        assert_model_path_exists(str(model_path))
        fill_input_by_label(driver, "Relay URL 1", relay_url)

        if hardware_mode:
            mode_select = driver.find_element(By.XPATH, "//label[normalize-space()='Compute mode']/following::select[1]")
            driver.execute_script(
                "arguments[0].value='gpu'; arguments[0].dispatchEvent(new Event('change',{bubbles:true}));",
                mode_select,
            )
            tier_select = driver.find_element(By.XPATH, "//select[@aria-label='Context tier']")
            driver.execute_script(
                "arguments[0].value=arguments[1]; arguments[0].dispatchEvent(new Event('change',{bubbles:true}));",
                tier_select,
                args.context_tier,
            )

        wait_for_start_operator_enabled(driver, relay_log, driver_log)
        pre_start_session_id = _status_value(driver, "Operator session ID") if hardware_mode else ""
        driver.find_element(By.XPATH, "//button[.='Start operator']").click()

        wait_for_running_stability(driver, "yes", stable_seconds=3.0)
        # Multi-relay UI labels registered operators as `yes (N/M relays)`.
        # Treat any label beginning with `yes` as the ready state while
        # preserving the existing single-relay `yes` match.
        registered_ready_xpath = (
            "//p[contains(.,'Registered:')]"
            "//strong[starts-with(normalize-space(), 'yes')]"
        )
        wait.until(lambda d: d.find_element(By.XPATH, registered_ready_xpath))
        if hardware_mode:
            assert_packaged_windows_nvidia_status(driver, args.context_tier, pre_start_session_id)
        wait_for_relay_diagnostics_count(relay_url, 1, timeout_seconds=5.0)
        operator_log = read_tail(relay_log) + read_tail(driver_log)
        assert "lease_seconds=120" in operator_log
        landing_driver = start_landing_driver()
        landing_driver.get(relay_url)
        WebDriverWait(landing_driver, 4).until(
            lambda d: landing_compute_node_status_matches(d, "Live compute nodes: 1")
        )

        prompt = driver.find_element(
            By.XPATH,
            "//label[normalize-space()='Prompt']/following-sibling::textarea[1]",
        )
        inference_prompt = (
            "Return a short hardware acceptance response." if hardware_mode else "say hello from mock"
        )
        prompt.send_keys(inference_prompt)
        wait.until(
            lambda d: d.find_element(By.XPATH, "//button[.='Start local inference']").is_enabled()
        )
        driver.find_element(By.XPATH, "//button[.='Start local inference']").click()

        output_text = wait_for_inference_result(driver)
        assert output_text, "inference output is empty"

        last_error_text = driver.find_element(By.XPATH, "//p[contains(.,'Last error:')]").text
        lowered_last_error = last_error_text.lower()
        for marker in (
            "bridge failure",
            "unsupported operand",
            "no module named",
            "modulenotfounderror",
            "importerror",
            "model path not found",
        ):
            assert marker not in lowered_last_error, (
                f"Last error contains forbidden marker `{marker}`: {last_error_text}"
            )
        assert_relay_roundtrip(
            relay_url,
            relay_log,
            driver_log,
            driver,
            prompt_text=inference_prompt,
        )
        if hardware_mode:
            combined_log = (read_tail(relay_log) + read_tail(driver_log)).lower()
            if "use_mock_llm=1" in combined_log or "mock inference" in combined_log:
                raise AssertionError("hardware encrypted inference used mock inference")
            if inference_prompt.lower() in combined_log:
                raise AssertionError("plaintext hardware prompt appeared in relay-owned logs")

        stop_clicked_at = time.monotonic()
        driver.find_element(By.XPATH, "//button[.='Stop operator']").click()
        diagnostics_helper_seconds = wait_for_relay_diagnostics_count(
            relay_url, 0, timeout_seconds=2.0
        )
        diagnostics_zero_observed_at = time.monotonic()
        stop_to_diagnostics_seconds = diagnostics_zero_observed_at - stop_clicked_at
        assert stop_to_diagnostics_seconds <= 2.0, (
            "expected Stop click to raw diagnostics zero within 2.0s; "
            f"observed {stop_to_diagnostics_seconds:.3f}s "
            f"(helper polling duration {diagnostics_helper_seconds:.3f}s)"
        )

        WebDriverWait(landing_driver, 2.5).until(
            lambda d: landing_compute_node_status_matches(d, "Live compute nodes: 0")
        )
        widget_zero_at = time.monotonic()
        diagnostics_to_widget_seconds = widget_zero_at - diagnostics_zero_observed_at
        assert diagnostics_to_widget_seconds <= 2.5, (
            "expected already-open landing widget to reach zero within 2.5s of diagnostics; "
            f"observed {diagnostics_to_widget_seconds:.3f}s"
        )

        operator_log = wait_for_operator_log_stop_markers(relay_log, driver_log)
        assert "desktop.compute_node.bridge_process_exited operator_session_id=" in operator_log

        print(
            "desktop_operator_stop_latency "
            f"stop_to_diagnostics_seconds={stop_to_diagnostics_seconds:.3f} "
            f"diagnostics_to_widget_seconds={diagnostics_to_widget_seconds:.3f}"
        )
    except TimeoutException as exc:
        raise RuntimeError(diagnostics_message("desktop UI e2e timed out", relay_log, driver_log, driver)) from exc
    except AssertionError as exc:
        raise RuntimeError(
            diagnostics_message(f"desktop UI e2e assertion failed: {exc}", relay_log, driver_log, driver)
        ) from exc
    except WebDriverException as exc:
        raise RuntimeError(
            diagnostics_message(f"desktop UI e2e webdriver failure: {exc}", relay_log, driver_log, driver)
        ) from exc
    finally:
        # A cleanup failure here must never replace/mask a primary test
        # failure already propagating out of the try block above.
        if landing_driver is not None:
            with contextlib.suppress(Exception):
                landing_driver.quit()
        if driver is not None:
            with contextlib.suppress(Exception):
                driver.quit()
        with contextlib.suppress(Exception):
            terminate_process(tauri_driver)
        with contextlib.suppress(Exception):
            terminate_process(relay)
        shutil.rmtree(isolated_home, ignore_errors=True)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        BOOTSTRAP_LOG.write_text(
            "desktop ui e2e top-level failure\n"
            f"error_type={type(exc).__name__}\n"
            f"error={exc}\n",
            encoding="utf-8",
        )
        raise
