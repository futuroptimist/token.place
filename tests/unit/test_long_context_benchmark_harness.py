import ast
import copy
import json
import os
import subprocess
import sys
import textwrap
import time
from types import ModuleType, SimpleNamespace
from pathlib import Path

import pytest

from scripts.long_context_benchmark import benchmark_harness as h

RUNNER_SOURCE = Path(__file__).parents[2] / "desktop-tauri/scripts/test_desktop_operator_ui_e2e.py"


class _WebDriverException(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.msg = message


class NoSuchElementException(_WebDriverException):
    pass


class StaleElementReferenceException(_WebDriverException):
    pass


class InvalidArgumentException(_WebDriverException):
    pass


class SessionNotCreatedException(_WebDriverException):
    pass


class ReadTimeoutError(Exception):
    """Dependency-free stand-in for urllib3.exceptions.ReadTimeoutError."""


class ConnectTimeoutError(Exception):
    pass


class NewConnectionError(Exception):
    pass


class ProtocolError(Exception):
    pass


def test_desktop_runner_imports_trusted_urllib3_transport_exceptions():
    tree = ast.parse(RUNNER_SOURCE.read_text(encoding="utf-8"))
    transport_import = next(node for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "urllib3.exceptions")
    namespace = {}
    exec(compile(ast.Module(body=[transport_import], type_ignores=[]),
        str(RUNNER_SOURCE), "exec"), namespace)

    assert {name: value.__module__ for name, value in namespace.items()
        if name in {"ConnectTimeoutError", "NewConnectionError", "ProtocolError",
            "ReadTimeoutError"}} == {
        "ConnectTimeoutError": "urllib3.exceptions",
        "NewConnectionError": "urllib3.exceptions",
        "ProtocolError": "urllib3.exceptions",
        "ReadTimeoutError": "urllib3.exceptions",
    }


@pytest.fixture
def desktop_runner():
    tree = ast.parse(RUNNER_SOURCE.read_text(encoding="utf-8"))
    assignments = {
        "NATIVE_WEBDRIVER_URL", "WEBDRIVER_DIAGNOSTIC_SCHEMA_VERSION",
        "WEBDRIVER_COMPATIBILITY_RESULTS", "WEBDRIVER_EXCEPTION_FAMILIES",
        "WEBDRIVER_PROCESS_POSTURES", "WEBDRIVER_SESSION_ELAPSED_BUCKETS",
        "WEBDRIVER_TARGET_CATEGORIES", "WEBDRIVER_READINESS_CATEGORIES",
        "OPERATOR_START_DIAGNOSTIC_ALLOWLISTS",
        "OPERATOR_START_DIAGNOSTIC_DEFAULTS",
        "NATIVE_STARTUP_DIAGNOSTIC_ALLOWLISTS", "NATIVE_STARTUP_DIAGNOSTIC_DEFAULTS",
        "PACKAGED_STARTUP_DIAGNOSTIC_ALLOWLISTS",
        "PACKAGED_STARTUP_DIAGNOSTIC_DEFAULTS",
    }
    names = {"_wait_for_packaged_setup_condition", "_prepare_packaged_landing_page",
        "_validate_packaged_failure_reason", "_enter_packaged_prompt",
        "_populate_and_submit_packaged_prompt", "_is_windows_sharing_violation", "_write_benchmark_phase",
        "_is_windows_checkpoint_contention",
        "_remove_owned_path", "_cleanup_owned_process_tree", "_quit_webdriver",
        "_read_primary_tokenizer_observation", "tokenizer_handoff_args",
        "tokenizer_stage_path", "_write_tokenizer_stage", "_read_tokenizer_stage",
        "_validate_operator_tokenizer_handoff", "_rearm_tokenizer_stage",
        "_validate_final_tokenizer_stage",
        "tauri_driver_environment", "tauri_driver_command", "wait_for_webdriver_ready",
        "start_driver", "wait_for_webview2_devtools", "launch_webview2_application",
        "wait_for_ui_ready",
        "wait_for_post_start_operator_state", "require_clean_relay_registration_baseline",
        "_classify_webdriver_session_failure", "_webdriver_process_posture",
        "_webdriver_session_elapsed_bucket", "_write_webdriver_diagnostic",
        "_read_operator_start_diagnostic", "_read_native_startup_diagnostic",
        "_read_packaged_startup_diagnostic", "_status_value",
        "main"}
    functions = [node for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name in names)
        or (isinstance(node, (ast.Assign, ast.AnnAssign))
            and any(isinstance(target, ast.Name) and target.id in assignments
                for target in (node.targets if isinstance(node, ast.Assign)
                    else [node.target])))
        ]
    module = ModuleType("desktop_runner_under_test")
    namespace = module.__dict__
    namespace.update({"webdriver": SimpleNamespace(Chrome=object, Remote=object,
        ChromeOptions=object), "ActionChains": object,
        "time": time, "By": SimpleNamespace(CSS_SELECTOR="css", XPATH="xpath"),
        "os": os, "json": json, "tempfile": __import__("tempfile"),
        "argparse": __import__("argparse"),
        "shutil": __import__("shutil"), "Path": Path,
        "subprocess": subprocess, "contextlib": __import__("contextlib"),
        "Callable": __import__("typing").Callable,
        "psutil": __import__("psutil"),
        "Keys": SimpleNamespace(SHIFT="SHIFT", ENTER="ENTER"),
        "TimeoutException": TimeoutError, "RuntimeError": RuntimeError,
        "NoSuchElementException": NoSuchElementException,
        "NoSuchFrameException": _WebDriverException,
        "StaleElementReferenceException": StaleElementReferenceException,
        "WebDriverException": _WebDriverException,
        "WebDriverWait": object, "WEBDRIVER_URL": "http://127.0.0.1:4444",
        "NATIVE_WEBDRIVER_URL": "http://127.0.0.1:4445",
        "reserve_free_port": lambda: 49152,
        "terminate_process": lambda _process: None,
        "urlopen": None,
        "PACKAGED_FAILURE_REASONS": h.PACKAGED_FAILURE_REASONS,
        "PACKAGED_PHASES": h.PACKAGED_PHASES,
        "SessionNotCreatedException": SessionNotCreatedException,
        "InvalidArgumentException": InvalidArgumentException,
        "ReadTimeoutError": ReadTimeoutError, "ConnectTimeoutError": ConnectTimeoutError,
        "NewConnectionError": NewConnectionError, "ProtocolError": ProtocolError,
        "WEBDRIVER_DIAGNOSTIC_SCHEMA_VERSION": "packaged-webdriver-diagnostic-v7",
        "WEBDRIVER_COMPATIBILITY_RESULTS": frozenset({"match", "mismatch", "unknown"}),
        "WEBDRIVER_EXCEPTION_FAMILIES": frozenset({"read_timeout", "connection_failure",
            "capability_rejection", "driver_version_mismatch", "application_startup_failure",
            "tauri_driver_exit", "unknown"}),
        "WEBDRIVER_PROCESS_POSTURES": frozenset({"tauri_driver_exited", "native_driver_only",
            "application_present", "webview_descendants_present", "unknown"}),
        "WEBDRIVER_SESSION_ELAPSED_BUCKETS": frozenset({"under_5_seconds",
            "5_to_29_seconds", "30_to_89_seconds", "90_seconds_or_more", "unknown"}),
        "WEBDRIVER_TARGET_CATEGORIES": frozenset({"attachable_target", "no_target", "unknown"}),
        "WEBDRIVER_READINESS_CATEGORIES": frozenset({"ready", "no_window_handle",
            "wrong_handle", "missing_shell", "missing_required_controls",
            "initialization_pending", "application_initialization_failed",
            "webdriver_failure", "unknown"}),
        "OPERATOR_START_DIAGNOSTIC_ALLOWLISTS": {
            "start_handler_state": frozenset({"not_entered", "entered"}),
            "invocation_state": frozenset({"not_started", "pending", "resolved", "rejected"}),
            "native_event_observation": frozenset({"none", "running_received", "running_accepted", "running_rejected"}),
            "polling_observation": frozenset({"none", "not_running", "running_accepted", "running_rejected", "command_failed"}),
            "render_state": frozenset({"not_running", "running", "running_regressed"}),
        },
        "OPERATOR_START_DIAGNOSTIC_DEFAULTS": {
            "start_handler_state": "not_entered", "invocation_state": "not_started",
            "native_event_observation": "none", "polling_observation": "none",
            "render_state": "not_running",
        },
        "LOGS_DIR": Path.cwd() / ".desktop-e2e-logs",
        "apply_benchmark_context_tier": h.apply_benchmark_context_tier,
        "generate_fixture": h.generate_fixture,
        "invoke_packaged_runtime_adapter": h.invoke_packaged_runtime_adapter})
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(RUNNER_SOURCE), "exec"), namespace)
    return module


def test_webdriver_readiness_requires_valid_native_status(desktop_runner, monkeypatch):
    desktop_runner.os = SimpleNamespace(name="posix")
    responses = iter([
        OSError("hostile refused C:\\private\\prompt"),
        {"value": {"ready": False, "message": "hostile MODEL_SENTINEL"}},
        {"value": "malformed C:\\private\\path"},
        {"value": {"ready": True}},
    ])
    calls = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps(self.payload).encode()

    def open_status(url, timeout):
        calls.append((url, timeout))
        result = next(responses)
        if isinstance(result, Exception):
            raise result
        return Response(result)

    monkeypatch.setattr(desktop_runner, "urlopen", open_status)
    monkeypatch.setattr(desktop_runner.time, "sleep", lambda _seconds: None)
    desktop_runner.wait_for_webdriver_ready(
        SimpleNamespace(poll=lambda: None), timeout_seconds=10)
    assert len(calls) == 4
    assert all(url == "http://127.0.0.1:4444/status" for url, _ in calls)


@pytest.mark.parametrize(("process_exit", "expected"), [
    (7, "tauri_driver_exited"),
    (None, "webdriver_transport_failure"),
])
def test_webdriver_readiness_failure_is_bounded(
        desktop_runner, monkeypatch, process_exit, expected):
    clock = SimpleNamespace(value=-1.0)
    def monotonic():
        clock.value += 1.0
        return clock.value
    monkeypatch.setattr(desktop_runner.time, "monotonic", monotonic)
    monkeypatch.setattr(desktop_runner.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(desktop_runner, "urlopen", lambda *_args, **_kwargs: None)
    with pytest.raises(RuntimeError, match=f"^{expected}$") as raised:
        desktop_runner.wait_for_webdriver_ready(
            SimpleNamespace(poll=lambda: process_exit), timeout_seconds=0.5)
    assert "private" not in str(raised.value)


def test_webdriver_readiness_detects_driver_exit_after_transient_status(
        desktop_runner, monkeypatch):
    polls = iter([None, 9])
    monkeypatch.setattr(desktop_runner, "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("private response")))
    monkeypatch.setattr(desktop_runner.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="^tauri_driver_exited$") as raised:
        desktop_runner.wait_for_webdriver_ready(
            SimpleNamespace(poll=lambda: next(polls)), timeout_seconds=10)

    assert "private" not in str(raised.value)


def test_packaged_tokenizer_handoff_uses_paired_application_arguments(desktop_runner, tmp_path):
    request = tmp_path / "request with spaces.json"
    evidence = tmp_path / "evidence with spaces.json"
    assert desktop_runner.tokenizer_handoff_args(request, evidence) == [
        f"--token-place-long-context-benchmark-tokenizer-request={request}",
        f"--token-place-long-context-benchmark-tokenizer-evidence={evidence}",
    ]
    assert desktop_runner.tokenizer_handoff_args() == []
    with pytest.raises(ValueError, match="must be paired"):
        desktop_runner.tokenizer_handoff_args(request, None)
    with pytest.raises(ValueError, match="must be paired"):
        desktop_runner.tokenizer_handoff_args(None, evidence)


def test_start_driver_passes_resolved_application_and_tokenizer_arguments(
        desktop_runner, tmp_path):
    desktop_runner.os = SimpleNamespace(name="posix")
    remote_calls = []

    def fake_remote(**kwargs):
        remote_calls.append(kwargs)
        return "driver"

    desktop_runner.webdriver = SimpleNamespace(Remote=fake_remote)
    application = (tmp_path / "application with spaces.exe").resolve()
    request = tmp_path / "request with spaces.json"
    evidence = tmp_path / "evidence with spaces.json"
    arguments = desktop_runner.tokenizer_handoff_args(request, evidence)

    assert desktop_runner.start_driver(application, application_args=arguments) == "driver"
    options = remote_calls[0]["options"]
    assert options.to_capabilities() == {
        "browserName": "wry",
        "tauri:options": {
            "application": str(application),
            "args": [
                f"--token-place-long-context-benchmark-tokenizer-request={request}",
                f"--token-place-long-context-benchmark-tokenizer-evidence={evidence}",
            ],
        },
    }
    assert "goog:chromeOptions" not in options.to_capabilities()
    assert all(not argument.startswith("--edge-webview-switches=")
        for argument in options.to_capabilities()["tauri:options"]["args"])
    assert options._ignore_local_proxy is False
    assert remote_calls == [{
        "command_executor": "http://127.0.0.1:4444",
        "options": remote_calls[0]["options"],
    }]


def test_start_driver_uses_exact_windows_native_webview2_capabilities(
        desktop_runner, tmp_path):
    remote_calls = []
    desktop_runner.webdriver = SimpleNamespace(
        Remote=lambda **kwargs: remote_calls.append(kwargs) or "driver")
    desktop_runner.os = SimpleNamespace(name="nt")
    application = (tmp_path / "current head.exe").resolve()
    arguments = ["--request=private value", "--evidence=private value"]

    assert desktop_runner.start_driver(
        application, application_args=arguments,
        debugger_address="127.0.0.1:49152") == "driver"
    options = remote_calls[0]["options"]
    assert remote_calls[0]["command_executor"] == "http://127.0.0.1:4445"
    assert options.to_capabilities() == {
        "browserName": "webview2",
        "ms:edgeChromium": True,
        "ms:edgeOptions": {"debuggerAddress": "127.0.0.1:49152"},
    }
    assert "binary" not in options.to_capabilities()["ms:edgeOptions"]
    assert "args" not in options.to_capabilities()["ms:edgeOptions"]
    assert "webviewOptions" not in options.to_capabilities()["ms:edgeOptions"]
    assert "tauri:options" not in options.to_capabilities()
    assert "goog:chromeOptions" not in options.to_capabilities()


def test_webview2_devtools_waits_for_owned_application(desktop_runner, monkeypatch):
    responses = iter([
        OSError("hostile C:\\private\\prompt"),
        {"Browser": "WebView2", "webSocketDebuggerUrl": "ws://browser"},
        [{"type": "page", "webSocketDebuggerUrl": ""}],
        [{"type": "browser", "webSocketDebuggerUrl": "ws://browser"}],
        [{"type": "page", "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/page/id"}],
    ])
    requested = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(self.payload).encode()

    def open_devtools(url, **_kwargs):
        requested.append(url)
        result = next(responses)
        if isinstance(result, Exception):
            raise result
        return Response(result)

    monkeypatch.setattr(desktop_runner, "urlopen", open_devtools)
    monkeypatch.setattr(desktop_runner.time, "sleep", lambda _seconds: None)
    desktop_runner.wait_for_webview2_devtools(
        SimpleNamespace(poll=lambda: None), 49152, 5)
    assert requested == ["http://127.0.0.1:49152/json/list"] * 5


def test_webview2_packaged_launch_supplies_switch_and_waits_for_target(
        desktop_runner, monkeypatch, tmp_path):
    application = tmp_path / "installed app.exe"
    application.write_text("candidate")
    process = SimpleNamespace(pid=123, poll=lambda: None)
    launches = []
    waits = []
    monkeypatch.setattr(desktop_runner.subprocess, "Popen",
        lambda args, **kwargs: launches.append((args, kwargs)) or process)
    monkeypatch.setattr(desktop_runner, "wait_for_webview2_devtools",
        lambda *args: waits.append(args))

    launched, debugger_address = desktop_runner.launch_webview2_application(
        application, {"BASE": "kept"}, object(), 12.5)

    assert launched is process
    assert debugger_address == "127.0.0.1:49152"
    assert launches[0][0] == [str(application.resolve()),
        "--edge-webview-switches=--remote-debugging-port=49152"]
    assert launches[0][1]["env"] == {
        "BASE": "kept", "TAURI_AUTOMATION": "true",
        "TAURI_WEBVIEW_AUTOMATION": "true"}
    assert waits == [(process, 49152, 12.5)]


@pytest.mark.parametrize("failure", [
    OSError("application did not start"),
    RuntimeError("webdriver_transport_failure"),
])
def test_webview2_packaged_launch_fails_closed_and_terminates_started_process(
        desktop_runner, monkeypatch, tmp_path, failure):
    application = tmp_path / "installed app.exe"
    application.write_text("candidate")
    process = SimpleNamespace(pid=123, poll=lambda: None)
    terminated = []
    if isinstance(failure, OSError):
        monkeypatch.setattr(desktop_runner.subprocess, "Popen",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(failure))
    else:
        monkeypatch.setattr(desktop_runner.subprocess, "Popen",
            lambda *_args, **_kwargs: process)
        monkeypatch.setattr(desktop_runner, "wait_for_webview2_devtools",
            lambda *_args: (_ for _ in ()).throw(failure))
    monkeypatch.setattr(desktop_runner, "terminate_process",
        lambda owned: terminated.append(owned))

    expected = ("webdriver_application_startup_failed"
        if isinstance(failure, OSError) else "webdriver_transport_failure")
    with pytest.raises(RuntimeError, match=expected):
        desktop_runner.launch_webview2_application(
            application, {}, object(), 1)
    assert terminated == ([] if isinstance(failure, OSError) else [process])


def test_packaged_hardware_main_attaches_and_cleans_up_owned_application():
    tree = ast.parse(RUNNER_SOURCE.read_text(encoding="utf-8"))
    main = next(node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main")
    calls = [node for node in ast.walk(main) if isinstance(node, ast.Call)]
    launch = next(call for call in calls
        if isinstance(call.func, ast.Name)
        and call.func.id == "launch_webview2_application")
    start = next(call for call in calls
        if isinstance(call.func, ast.Name) and call.func.id == "start_driver")
    cleanup = next(call for call in calls
        if isinstance(call.func, ast.Name) and call.func.id == "terminate_process"
        and call.args and isinstance(call.args[0], ast.Name)
        and call.args[0].id == "application_process")

    assert any(keyword.arg == "debugger_address"
        and isinstance(keyword.value, ast.Name)
        and keyword.value.id == "debugger_address" for keyword in start.keywords)
    assignment = next(node for node in ast.walk(main)
        if isinstance(node, ast.Assign) and node.value is launch)
    assert [target.id for target in assignment.targets[0].elts] == [
        "application_process", "debugger_address"]
    assert cleanup is not None


def test_ui_ready_selects_application_handle_without_optional_artifact_panel(
        desktop_runner):
    class Wait:
        def __init__(self, driver, timeout, poll_frequency):
            self.driver = driver

        def until(self, condition):
            return condition(self.driver) or condition(self.driver)

    class SwitchTo:
        def __init__(self, driver):
            self.driver = driver

        def window(self, handle):
            self.driver.current = handle

        def default_content(self):
            return None

    class Driver:
        current = None
        handle_reads = 0

        def __init__(self):
            self.switch_to = SwitchTo(self)

        @property
        def window_handles(self):
            self.handle_reads += 1
            return [] if self.handle_reads == 1 else ["unrelated", "application"]

        def execute_script(self, _script):
            if "applicationInitialization" in _script:
                return "ready"
            return ("token.place desktop MVP"
                if "document.title" in _script and self.current == "application"
                else "unrelated" if "document.title" in _script else "complete")

        def find_elements(self, _by, locator):
            if self.current != "application":
                return []
            if "Runtime resolved path" in locator:
                raise AssertionError("optional artifact panel must not be queried")
            return [object()]

    desktop_runner.WebDriverWait = Wait
    driver = Driver()
    desktop_runner.wait_for_ui_ready(driver, timeout_seconds=3)
    assert driver.current == "application"


@pytest.mark.parametrize(("handles", "expected"), [
    ([], "no_window_handle"),
    (["unrelated"], "wrong_handle"),
])
def test_ui_ready_failure_is_bounded_and_preserves_deadline(
        desktop_runner, handles, expected):
    waits = []

    class Wait:
        def __init__(self, driver, timeout, poll_frequency):
            waits.append((timeout, poll_frequency))
            self.driver = driver

        def until(self, condition):
            condition(self.driver)
            return False

    driver = SimpleNamespace(
        window_handles=handles,
        switch_to=SimpleNamespace(window=lambda _handle: None, default_content=lambda: None),
        execute_script=lambda script: (
            "ready" if "applicationInitialization" in script
            else "unrelated" if "document.title" in script else "complete"),
        find_elements=lambda *_args: [],
    )
    desktop_runner.WebDriverWait = Wait
    with pytest.raises(RuntimeError, match=f"^{expected}$"):
        desktop_runner.wait_for_ui_ready(driver, timeout_seconds=7.5)
    assert waits == [(7.5, 0.25)]


@pytest.mark.parametrize(("available", "expected"), [
    (set(), "missing_shell"),
    ({"shell"}, "missing_required_controls"),
])
def test_ui_ready_classifies_application_shell_and_controls(
        desktop_runner, available, expected):
    class Wait:
        def __init__(self, driver, timeout, poll_frequency):
            self.driver = driver

        def until(self, condition):
            condition(self.driver)
            return False

    def find_elements(_by, locator):
        if "//h1" in locator:
            return [object()] if "shell" in available else []
        if "Model GGUF path" in locator:
            return [object()] if "model" in available else []
        if "Relay URL 1" in locator:
            return [object()] if "relay" in available else []
        return []

    desktop_runner.WebDriverWait = Wait
    driver = SimpleNamespace(
        window_handles=["application"],
        switch_to=SimpleNamespace(window=lambda _handle: None, default_content=lambda: None),
        execute_script=lambda script: (
            "ready" if "applicationInitialization" in script
            else "token.place desktop MVP" if "document.title" in script else "complete"),
        find_elements=find_elements,
    )
    with pytest.raises(RuntimeError, match=f"^{expected}$"):
        desktop_runner.wait_for_ui_ready(driver, timeout_seconds=2)


@pytest.mark.parametrize("failure_point", [
    "document_loading",
    "window_switch",
    "window_handles",
])
def test_ui_ready_classifies_transient_webdriver_failures(
        desktop_runner, failure_point):
    class Wait:
        def __init__(self, driver, timeout, poll_frequency):
            self.driver = driver

        def until(self, condition):
            condition(self.driver)
            return False

    class Driver:
        switch_to = SimpleNamespace(
            window=lambda _handle: None,
            default_content=lambda: None,
        )

        @property
        def window_handles(self):
            if failure_point == "window_handles":
                raise desktop_runner.WebDriverException("private window sentinel")
            return ["application"]

        def execute_script(self, script):
            if failure_point == "window_switch":
                raise desktop_runner.WebDriverException("private session sentinel")
            if "applicationInitialization" in script:
                return "ready"
            return "loading" if "readyState" in script else "token.place desktop MVP"

        def find_elements(self, *_args):
            raise AssertionError("controls must not be queried before document readiness")

    desktop_runner.WebDriverWait = Wait
    expected = ("wrong_handle" if failure_point == "document_loading"
        else "webdriver_failure")
    with pytest.raises(RuntimeError, match=f"^{expected}$") as exc_info:
        desktop_runner.wait_for_ui_ready(Driver(), timeout_seconds=2)
    assert "private" not in str(exc_info.value)


@pytest.mark.parametrize(("initialization", "expected"), [
    ("pending", "initialization_pending"),
    ("failed", "application_initialization_failed"),
])
def test_ui_ready_requires_completed_application_initialization(
        desktop_runner, initialization, expected):
    class Wait:
        def __init__(self, driver, timeout, poll_frequency):
            self.driver = driver

        def until(self, condition):
            return condition(self.driver)

    def execute_script(script):
        if "applicationInitialization" in script:
            return initialization
        return "token.place desktop MVP" if "document.title" in script else "complete"

    desktop_runner.WebDriverWait = Wait
    driver = SimpleNamespace(
        window_handles=["application"],
        switch_to=SimpleNamespace(window=lambda _handle: None, default_content=lambda: None),
        execute_script=execute_script,
        find_elements=lambda *_args: [object()],
    )
    with pytest.raises(RuntimeError, match=f"^{expected}$"):
        desktop_runner.wait_for_ui_ready(driver, timeout_seconds=2)


@pytest.mark.parametrize(("poll", "expected"), [
    (7, "webdriver_application_startup_failed"),
    (None, "webdriver_transport_failure"),
])
def test_webview2_devtools_failure_is_bounded(
        desktop_runner, monkeypatch, poll, expected):
    clock = SimpleNamespace(value=-1.0)
    def monotonic():
        clock.value += 1.0
        return clock.value
    monkeypatch.setattr(desktop_runner.time, "monotonic", monotonic)
    monkeypatch.setattr(desktop_runner.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(desktop_runner, "urlopen", lambda *_args, **_kwargs: None)
    with pytest.raises(RuntimeError, match=f"^{expected}$"):
        desktop_runner.wait_for_webview2_devtools(
            SimpleNamespace(poll=lambda: poll), 49152, 0.5)


def test_tauri_driver_command_selects_explicit_windows_native_driver(
        desktop_runner, monkeypatch, tmp_path):
    edge_dir = tmp_path / "edge driver"
    edge_dir.mkdir()
    edge_driver = edge_dir / "msedgedriver.exe"
    edge_driver.write_bytes(b"driver")
    monkeypatch.setenv("EDGEWEBDRIVER", str(edge_dir))
    monkeypatch.setenv("TOKEN_PLACE_BROWSER_DRIVER_COMPATIBILITY", "match")
    desktop_runner.os = SimpleNamespace(name="nt", environ=os.environ, access=os.access, X_OK=os.X_OK)
    monkeypatch.setattr(desktop_runner.shutil, "which",
        lambda name: "tauri-driver.exe" if name == "tauri-driver" else None)

    assert desktop_runner.tauri_driver_command() == [
        "tauri-driver.exe", "--port", "4444", "--native-port", "4445", "--native-driver",
        str(edge_driver.resolve()),
    ]


def test_webdriver_readiness_uses_explicit_windows_native_endpoint(
        desktop_runner, monkeypatch):
    requested = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"value":{"ready":true}}'

    desktop_runner.os = SimpleNamespace(name="nt")
    monkeypatch.setattr(desktop_runner, "urlopen",
        lambda url, **_kwargs: requested.append(url) or Response())
    desktop_runner.wait_for_webdriver_ready(
        SimpleNamespace(poll=lambda: None), timeout_seconds=1)

    assert requested == ["http://127.0.0.1:4445/status"]


def test_tauri_driver_command_requires_exported_verified_windows_path(
        desktop_runner, monkeypatch, tmp_path):
    edge_driver = tmp_path / "msedgedriver.exe"
    edge_driver.write_bytes(b"driver")
    monkeypatch.delenv("EDGEWEBDRIVER", raising=False)
    monkeypatch.setenv("TOKEN_PLACE_BROWSER_DRIVER_COMPATIBILITY", "match")
    desktop_runner.os = SimpleNamespace(name="nt", environ=os.environ, access=os.access, X_OK=os.X_OK)
    monkeypatch.setattr(desktop_runner.shutil, "which", lambda name: {
        "tauri-driver": "tauri-driver.exe",
        "msedgedriver.exe": str(edge_driver),
    }.get(name))

    with pytest.raises(RuntimeError, match="^native_driver_unavailable$"):
        desktop_runner.tauri_driver_command()


def test_tauri_driver_command_fails_bounded_when_windows_driver_missing(
        desktop_runner, monkeypatch, tmp_path):
    private = str(tmp_path / "private-edge-location")
    monkeypatch.setenv("EDGEWEBDRIVER", private)
    monkeypatch.setenv("TOKEN_PLACE_BROWSER_DRIVER_COMPATIBILITY", "match")
    desktop_runner.os = SimpleNamespace(name="nt", environ=os.environ, access=os.access, X_OK=os.X_OK)
    monkeypatch.setattr(desktop_runner.shutil, "which",
        lambda name: "tauri-driver.exe" if name == "tauri-driver" else None)

    with pytest.raises(RuntimeError, match="^native_driver_unavailable$") as raised:
        desktop_runner.tauri_driver_command()
    assert private not in str(raised.value)


@pytest.mark.parametrize("compatibility", [None, "unknown", "mismatch", "hostile-private-path"])
def test_tauri_driver_command_fails_closed_without_verified_compatibility(
        desktop_runner, monkeypatch, tmp_path, compatibility):
    driver = tmp_path / "msedgedriver.exe"
    driver.write_bytes(b"driver")
    monkeypatch.setenv("EDGEWEBDRIVER", str(driver))
    if compatibility is None:
        monkeypatch.delenv("TOKEN_PLACE_BROWSER_DRIVER_COMPATIBILITY", raising=False)
    else:
        monkeypatch.setenv("TOKEN_PLACE_BROWSER_DRIVER_COMPATIBILITY", compatibility)
    desktop_runner.os = SimpleNamespace(
        name="nt", environ=os.environ, access=os.access, X_OK=os.X_OK)

    with pytest.raises(RuntimeError, match="^native_driver_unavailable$") as raised:
        desktop_runner.tauri_driver_command()
    assert str(tmp_path) not in str(raised.value)


@pytest.mark.parametrize(("exception", "process_exit", "expected"), [
    (SessionNotCreatedException("This version of msedgedriver only supports Microsoft Edge version 1"),
        None, "webdriver_driver_version_mismatch"),
    (InvalidArgumentException("hostile C:\\private\\prompt capability"),
        None, "webdriver_capabilities_rejected"),
    (Exception("ignored"), 1, "tauri_driver_exited"),
    (SessionNotCreatedException("application failed to launch C:\\private\\model"),
        None, "webdriver_application_startup_failed"),
    (SessionNotCreatedException("cannot find Microsoft Edge binary C:\\private\\model"),
        None, "webdriver_application_startup_failed"),
])
def test_webdriver_session_failure_categories_are_bounded(
        desktop_runner, exception, process_exit, expected):
    category, state, _family = desktop_runner._classify_webdriver_session_failure(
        exception, SimpleNamespace(poll=lambda: process_exit))
    assert category == expected
    assert state == ("exited" if process_exit is not None else "running")
    assert "private" not in category


def test_webdriver_session_failure_transport_and_safe_fallback(desktop_runner):
    transport = SimpleNamespace(msg="connection refused C:\\private\\prompt")
    assert desktop_runner._classify_webdriver_session_failure(
        transport, SimpleNamespace(poll=lambda: None)) == (
            "webdriver_transport_failure", "running", "connection_failure")
    hostile = SimpleNamespace(msg="C:\\private\\prompt MODEL_SENTINEL")
    assert desktop_runner._classify_webdriver_session_failure(
        hostile, SimpleNamespace(poll=lambda: None))[0] == "webdriver_session_creation_failed"


@pytest.mark.parametrize("exception_type", [
    ConnectTimeoutError,
    NewConnectionError,
    ProtocolError,
])
def test_webdriver_session_dependency_transport_failures_are_bounded(
        desktop_runner, exception_type):
    failure = exception_type("hostile C:\\private\\prompt MODEL_SENTINEL")

    assert desktop_runner._classify_webdriver_session_failure(
        failure, SimpleNamespace(poll=lambda: None)) == (
            "webdriver_transport_failure", "running", "connection_failure")


def test_webdriver_session_timeout_is_bounded_and_redacted(desktop_runner):
    private = "C:\\private\\application.exe --secret-prompt"
    timeout = SimpleNamespace(msg=f"HTTP connection read timed out while opening {private}")
    category, state, family = desktop_runner._classify_webdriver_session_failure(
        timeout, SimpleNamespace(poll=lambda: None))
    assert (category, state, family) == (
        "webdriver_transport_failure", "running", "read_timeout")
    assert private not in category


@pytest.mark.parametrize("exception", [
    ReadTimeoutError("hostile C:\\private\\model --secret-prompt"),
    Exception("HTTP connection read timed out at C:\\private\\application.exe"),
])
def test_webdriver_read_timeout_without_msg_is_classified(desktop_runner, exception):
    assert not hasattr(exception, "msg")
    assert desktop_runner._classify_webdriver_session_failure(
        exception, SimpleNamespace(poll=lambda: None)) == (
            "webdriver_transport_failure", "running", "read_timeout")


def test_real_urllib3_read_timeout_without_msg_is_classified(desktop_runner):
    from urllib3.exceptions import ReadTimeoutError as Urllib3ReadTimeoutError

    timeout = Urllib3ReadTimeoutError(None, None, "C:\\private\\prompt timed out")
    assert not hasattr(timeout, "msg")
    desktop_runner.ReadTimeoutError = Urllib3ReadTimeoutError
    assert desktop_runner._classify_webdriver_session_failure(
        timeout, SimpleNamespace(poll=lambda: None)) == (
            "webdriver_transport_failure", "running", "read_timeout")


def test_webdriver_process_posture_and_elapsed_are_bounded(
        desktop_runner, monkeypatch, tmp_path):
    application_path = (tmp_path / "private application.exe").resolve()

    class Child:
        def __init__(self, executable, children=()):
            self.executable = executable
            self._children = list(children)

        def exe(self):
            return str(self.executable)

        def children(self, recursive):
            assert recursive is True
            return self._children

    webview = Child(tmp_path / "private WebView sentinel.exe")
    application = Child(application_path, [webview])
    native = Child(tmp_path / "private native driver.exe")
    monkeypatch.setattr(desktop_runner.psutil, "Process",
        lambda _pid: SimpleNamespace(children=lambda recursive: [native, application]))
    process = SimpleNamespace(pid=17, poll=lambda: None)

    assert desktop_runner._webdriver_process_posture(process, application_path) == (
        "webview_descendants_present")
    assert [desktop_runner._webdriver_session_elapsed_bucket(value) for value in
        (1, 5, 30, 90, "hostile")] == [
            "under_5_seconds", "5_to_29_seconds", "30_to_89_seconds",
            "90_seconds_or_more", "unknown"]


def test_webdriver_process_posture_handles_bounded_edge_cases(
        desktop_runner, monkeypatch, tmp_path):
    app = tmp_path / "private application.exe"
    process = SimpleNamespace(pid=17, poll=lambda: 1)
    assert desktop_runner._webdriver_process_posture(process, app) == "tauri_driver_exited"

    process.poll = lambda: None
    monkeypatch.setattr(desktop_runner.psutil, "Process",
        lambda _pid: SimpleNamespace(children=lambda recursive: []))
    assert desktop_runner._webdriver_process_posture(process, app) == "unknown"

    native = SimpleNamespace(exe=lambda: str(tmp_path / "private native driver.exe"))
    monkeypatch.setattr(desktop_runner.psutil, "Process",
        lambda _pid: SimpleNamespace(children=lambda recursive: [native]))
    assert desktop_runner._webdriver_process_posture(process, app) == "native_driver_only"

    unreadable = SimpleNamespace(exe=lambda: (_ for _ in ()).throw(
        desktop_runner.psutil.Error("private process sentinel")))
    application = SimpleNamespace(exe=lambda: str(app.resolve()),
        children=lambda recursive: (_ for _ in ()).throw(
            desktop_runner.psutil.Error("private child sentinel")))
    monkeypatch.setattr(desktop_runner.psutil, "Process",
        lambda _pid: SimpleNamespace(children=lambda recursive: [unreadable, application]))
    assert desktop_runner._webdriver_process_posture(process, app) == "application_present"

    monkeypatch.setattr(desktop_runner.psutil, "Process",
        lambda _pid: (_ for _ in ()).throw(desktop_runner.psutil.Error("private sentinel")))
    assert desktop_runner._webdriver_process_posture(process, app) == "unknown"


def test_webdriver_changed_safety_branches_are_bounded(
        desktop_runner, monkeypatch, tmp_path):
    desktop_runner.os = SimpleNamespace(name="nt")

    def remote(**kwargs):
        kwargs["options"].to_capabilities()

    desktop_runner.webdriver = SimpleNamespace(Remote=remote)
    with pytest.raises(RuntimeError, match="^webdriver_application_startup_failed$"):
        desktop_runner.start_driver(tmp_path / "private application.exe")

    edge_driver = tmp_path / "msedgedriver.exe"
    edge_driver.write_bytes(b"driver")
    desktop_runner.os = SimpleNamespace(name="nt", environ={
        "TOKEN_PLACE_BROWSER_DRIVER_COMPATIBILITY": "match",
        "EDGEWEBDRIVER": str(edge_driver),
    })
    monkeypatch.setattr(desktop_runner.shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError, match="tauri-driver binary not found"):
        desktop_runner.tauri_driver_command()

    process = SimpleNamespace(pid=17, poll=lambda: None)
    application_process = SimpleNamespace(pid=23, poll=lambda: None)
    application = SimpleNamespace(children=lambda recursive: [object()])
    monkeypatch.setattr(desktop_runner.psutil, "Process", lambda _pid: application)
    assert desktop_runner._webdriver_process_posture(
        process, tmp_path / "private application.exe", application_process
    ) == "webview_descendants_present"
    application.children = lambda recursive: (_ for _ in ()).throw(
        desktop_runner.psutil.Error("private child sentinel"))
    assert desktop_runner._webdriver_process_posture(
        process, tmp_path / "private application.exe", application_process
    ) == "application_present"


def test_webdriver_session_diagnostic_artifact_is_fixed_schema_and_sanitized(
        desktop_runner, tmp_path):
    desktop_runner.LOGS_DIR = tmp_path
    desktop_runner._write_webdriver_diagnostic(
        "C:\\private\\prompt", "MODEL_SENTINEL", "SECRET_EXCEPTION",
        "read_timeout", "application_present", "30_to_89_seconds",
        "C:\\private\\target", "SECRET_READINESS")
    artifact = tmp_path / "packaged-webdriver-diagnostic.json"
    assert json.loads(artifact.read_text()) == {
        "schema_version": "packaged-webdriver-diagnostic-v7",
        "browser_driver_compatibility": "unknown",
        "tauri_driver_state": "unknown",
        "webdriver_failure_category": "webdriver_session_creation_failed",
        "exception_family": "read_timeout",
        "process_posture": "application_present",
        "session_elapsed_bucket": "30_to_89_seconds",
        "target_category": "unknown",
        "readiness_category": "unknown",
        "operator_progress": "not_started",
        "start_handler_state": "not_entered",
        "invocation_state": "not_started",
        "native_event_observation": "none",
        "polling_observation": "none",
        "render_state": "not_running",
        "native_startup_phase": "not_started",
        "native_startup_outcome": "not_started",
        "native_startup_failure_category": "none",
        **desktop_runner.PACKAGED_STARTUP_DIAGNOSTIC_DEFAULTS,
    }
    assert not list(tmp_path.glob("*.tmp"))
    assert "private" not in artifact.read_text()
    assert "SENTINEL" not in artifact.read_text()


def test_webdriver_diagnostic_clamps_invalid_v3_enums(desktop_runner, tmp_path):
    desktop_runner.LOGS_DIR = tmp_path
    hostile = "C:\\private\\application.exe --secret-prompt MODEL_SENTINEL"

    desktop_runner._write_webdriver_diagnostic(
        "match", "running", "none", hostile, hostile, hostile,
        "attachable_target", "ready")

    artifact = json.loads(
        (tmp_path / "packaged-webdriver-diagnostic.json").read_text())
    assert artifact == {
        "schema_version": "packaged-webdriver-diagnostic-v7",
        "browser_driver_compatibility": "match",
        "tauri_driver_state": "running",
        "webdriver_failure_category": "none",
        "exception_family": "unknown",
        "process_posture": "unknown",
        "session_elapsed_bucket": "unknown",
        "target_category": "attachable_target",
        "readiness_category": "ready",
        "operator_progress": "not_started",
        "start_handler_state": "not_entered",
        "invocation_state": "not_started",
        "native_event_observation": "none",
        "polling_observation": "none",
        "render_state": "not_running",
        "native_startup_phase": "not_started",
        "native_startup_outcome": "not_started",
        "native_startup_failure_category": "none",
        **desktop_runner.PACKAGED_STARTUP_DIAGNOSTIC_DEFAULTS,
    }
    assert "private" not in json.dumps(artifact)
    assert "SENTINEL" not in json.dumps(artifact)


def test_webdriver_diagnostic_clamps_hostile_operator_progress(desktop_runner, tmp_path):
    desktop_runner.LOGS_DIR = tmp_path
    desktop_runner._write_webdriver_diagnostic(
        "match", "running", "none", target_category="attachable_target",
        readiness_category="ready", operator_progress="C:\\private\\prompt SECRET")
    artifact = json.loads(
        (tmp_path / "packaged-webdriver-diagnostic.json").read_text())
    assert artifact["operator_progress"] == "not_started"
    assert "private" not in json.dumps(artifact)
    assert "SECRET" not in json.dumps(artifact)


def test_operator_start_diagnostic_collects_only_allowlisted_dom_values(desktop_runner):
    values = {
        "data-operator-start-handler": "entered",
        "data-operator-start-invocation": "pending",
        "data-operator-start-native-event": "running_accepted",
        "data-operator-start-polling": "running_rejected",
        "data-operator-start-render": "running_regressed",
    }
    shell = SimpleNamespace(get_attribute=lambda name: values[name])
    driver = SimpleNamespace(find_element=lambda *_args: shell)

    assert desktop_runner._read_operator_start_diagnostic(driver) == {
        "start_handler_state": "entered",
        "invocation_state": "pending",
        "native_event_observation": "running_accepted",
        "polling_observation": "running_rejected",
        "render_state": "running_regressed",
    }

    hostile = "C:\\private\\prompt SECRET raw exception"
    values.update({name: hostile for name in values})
    assert desktop_runner._read_operator_start_diagnostic(driver) == {
        "start_handler_state": "not_entered",
        "invocation_state": "not_started",
        "native_event_observation": "none",
        "polling_observation": "none",
        "render_state": "not_running",
    }


@pytest.mark.parametrize("driver", [
    None,
    SimpleNamespace(find_element=lambda *_args: (_ for _ in ()).throw(
        NoSuchElementException("private missing shell /secret/path"))),
    SimpleNamespace(find_element=lambda *_args: (_ for _ in ()).throw(
        StaleElementReferenceException("private stale shell /secret/path"))),
    SimpleNamespace(find_element=lambda *_args: (_ for _ in ()).throw(
        _WebDriverException("private webdriver failure /secret/path"))),
])
def test_operator_start_diagnostic_defaults_without_available_shell(
        desktop_runner, driver):
    diagnostic = desktop_runner._read_operator_start_diagnostic(driver)

    assert diagnostic == {
        "start_handler_state": "not_entered",
        "invocation_state": "not_started",
        "native_event_observation": "none",
        "polling_observation": "none",
        "render_state": "not_running",
    }
    assert "private" not in json.dumps(diagnostic)


def test_native_startup_diagnostic_collects_and_clamps_fixed_values(desktop_runner):
    values = {
        "data-native-startup-phase": "running_status_publication",
        "data-native-startup-outcome": "publication_suppressed",
        "data-native-startup-failure": "bridge_exited_before_startup_event",
    }
    shell = SimpleNamespace(get_attribute=lambda name: values[name])
    driver = SimpleNamespace(find_element=lambda *_args: shell)

    assert desktop_runner._read_native_startup_diagnostic(driver) == {
        "native_startup_phase": "running_status_publication",
        "native_startup_outcome": "publication_suppressed",
        "native_startup_failure_category": "bridge_exited_before_startup_event",
    }

    hostile = "C:\\private\\model.gguf prompt SECRET raw exception"
    values.update({name: hostile for name in values})
    assert desktop_runner._read_native_startup_diagnostic(driver) == {
        "native_startup_phase": "not_started",
        "native_startup_outcome": "not_started",
        "native_startup_failure_category": "none",
    }


@pytest.mark.parametrize("driver", [
    None,
    SimpleNamespace(find_element=lambda *_args: (_ for _ in ()).throw(
        NoSuchElementException("private missing shell /secret/path"))),
])
def test_native_startup_diagnostic_defaults_without_available_shell(
        desktop_runner, driver):
    assert desktop_runner._read_native_startup_diagnostic(driver) == {
        "native_startup_phase": "not_started",
        "native_startup_outcome": "not_started",
        "native_startup_failure_category": "none",
    }


@pytest.mark.parametrize("failure", [
    StaleElementReferenceException("private stale shell /secret/path"),
    _WebDriverException("private webdriver failure /secret/path"),
])
def test_native_startup_diagnostic_clamps_webdriver_failures(
        desktop_runner, failure):
    driver = SimpleNamespace(
        find_element=lambda *_args: (_ for _ in ()).throw(failure))

    diagnostic = desktop_runner._read_native_startup_diagnostic(driver)

    assert diagnostic == {
        "native_startup_phase": "not_started",
        "native_startup_outcome": "not_started",
        "native_startup_failure_category": "none",
    }
    assert "private" not in json.dumps(diagnostic)


def test_native_startup_diagnostic_is_preserved_without_private_data(desktop_runner, tmp_path):
    desktop_runner.LOGS_DIR = tmp_path
    desktop_runner._write_webdriver_diagnostic(
        "match", "running", "operator_running_not_reached",
        native_startup_diagnostic={
            "native_startup_phase": "startup_task_failed",
            "native_startup_outcome": "failed",
            "native_startup_failure_category": "child_spawn_failed",
            "private": "C:\\private\\model.gguf prompt SECRET raw exception",
        })
    artifact_text = (tmp_path / "packaged-webdriver-diagnostic.json").read_text()
    artifact = json.loads(artifact_text)
    assert artifact["native_startup_phase"] == "startup_task_failed"
    assert artifact["native_startup_outcome"] == "failed"
    assert artifact["native_startup_failure_category"] == "child_spawn_failed"
    assert "private" not in artifact_text
    assert "SECRET" not in artifact_text


def _packaged_status_driver(values):
    def find_element(_by, locator):
        label = next((label for label in values if f"'{label}:'" in locator), None)
        if label is None:
            raise NoSuchElementException("private unknown status C:\\secret")
        return SimpleNamespace(text=values[label])
    return SimpleNamespace(find_element=find_element)


@pytest.mark.parametrize(("operator", "native", "statuses", "readiness", "boundary"), [
    ({"start_handler_state": "not_entered", "invocation_state": "not_started"},
        {}, {}, "ready", "handler_not_entered"),
    ({"start_handler_state": "entered", "invocation_state": "pending"},
        {}, {}, "ready", "invocation_pending"),
    ({"start_handler_state": "entered", "invocation_state": "rejected"},
        {}, {}, "ready", "invocation_rejected"),
    ({"start_handler_state": "entered", "invocation_state": "resolved"},
        {"native_startup_phase": "not_started"}, {}, "ready", "native_not_reached"),
    ({"start_handler_state": "entered", "invocation_state": "resolved"},
        {"native_startup_phase": "session_reserved"}, {}, "ready",
        "native_preparation_not_reached"),
    ({"start_handler_state": "entered", "invocation_state": "resolved"},
        {"native_startup_phase": "bridge_launch_prepared"}, {}, "ready",
        "bridge_launch_not_reached"),
    ({"start_handler_state": "entered", "invocation_state": "resolved"},
        {"native_startup_phase": "bridge_attached"},
        {"Relay runtime state": "warming"}, "ready", "warm_load_pending"),
    ({"start_handler_state": "entered", "invocation_state": "resolved"},
        {"native_startup_phase": "bridge_attached"},
        {"Relay runtime state": "ready"}, "ready", "warm_load_ready"),
    ({"start_handler_state": "entered", "invocation_state": "resolved"},
        {"native_startup_phase": "startup_task_failed"},
        {"Relay runtime state": "failed", "Last worker error code": "warm_load_timeout"},
        "ready", "warm_load_timed_out"),
    ({"start_handler_state": "entered", "invocation_state": "resolved"},
        {"native_startup_phase": "startup_task_failed"},
        {"Relay runtime state": "failed"}, "ready", "warm_load_failed"),
    ({"start_handler_state": "entered", "invocation_state": "resolved"},
        {"native_startup_phase": "bridge_attached",
         "native_startup_failure_category": "bridge_exited_before_startup_event"},
        {"Last worker exit code": "0"}, "ready", "bridge_exited_clean"),
    ({"start_handler_state": "entered", "invocation_state": "resolved"},
        {"native_startup_phase": "bridge_attached",
         "native_startup_failure_category": "bridge_exited_before_startup_event"},
        {"Last worker exit code": "-1073741819"}, "ready", "bridge_exited_nonzero"),
    ({"start_handler_state": "entered", "invocation_state": "resolved"},
        {"native_startup_phase": "bridge_attached"}, {},
        "application_initialization_failed", "readiness_rejected"),
    ({"start_handler_state": "entered", "invocation_state": "resolved"},
        {"native_startup_phase": "running_status_publication"},
        {"Relay runtime state": "processing", "Registered": "no (0/1 relays)"},
        "ready", "registration_not_reached"),
    ({"start_handler_state": "entered", "invocation_state": "resolved"},
        {"native_startup_phase": "running_status_publication"},
        {"Relay runtime state": "ready", "Registered": "yes (1/1 relays)"},
        "ready", "registered"),
])
def test_packaged_startup_boundaries_are_bounded(
        desktop_runner, operator, native, statuses, readiness, boundary):
    diagnostic = desktop_runner._read_packaged_startup_diagnostic(
        _packaged_status_driver(statuses), operator, native, readiness)

    assert diagnostic["startup_boundary"] == boundary
    assert all(diagnostic[field] in allowed
        for field, allowed in desktop_runner.PACKAGED_STARTUP_DIAGNOSTIC_ALLOWLISTS.items())


def test_packaged_startup_diagnostic_clamps_private_malformed_values(
        desktop_runner, tmp_path):
    hostile = "C:\\private\\model.gguf prompt SECRET raw exception"
    diagnostic = desktop_runner._read_packaged_startup_diagnostic(
        _packaged_status_driver({
            "Startup phase": hostile,
            "Provisioning state": hostile,
            "Relay runtime state": hostile,
            "Worker state": hostile,
            "Last worker error code": hostile,
            "Last worker exit code": hostile,
            "Registered": hostile,
        }), {"start_handler_state": hostile, "invocation_state": hostile}, {}, "ready")
    assert diagnostic == {
        **desktop_runner.PACKAGED_STARTUP_DIAGNOSTIC_DEFAULTS,
        "startup_boundary": "handler_not_entered",
        "bridge_exit_posture": "unknown",
        "relay_polling_state": "unknown",
        "registration_state": "unknown",
    }

    desktop_runner.LOGS_DIR = tmp_path
    desktop_runner._write_webdriver_diagnostic(
        "match", "running", "operator_running_not_reached",
        packaged_startup_diagnostic={field: hostile for field in diagnostic})
    artifact_text = (tmp_path / "packaged-webdriver-diagnostic.json").read_text()
    artifact = json.loads(artifact_text)
    assert {field: artifact[field] for field in diagnostic} == \
        desktop_runner.PACKAGED_STARTUP_DIAGNOSTIC_DEFAULTS
    assert "private" not in artifact_text
    assert "SECRET" not in artifact_text


def test_packaged_startup_projects_worker_polling_and_registration(desktop_runner):
    before_polling = desktop_runner._read_packaged_startup_diagnostic(
        _packaged_status_driver({
            "Startup phase": "warm_load", "Provisioning state": "provisioning",
            "Relay runtime state": "warming", "Worker state": "provisioning",
            "Last worker error code": "none", "Last worker exit code": "none",
            "Registered": "no (0/1 relays)",
        }), {"start_handler_state": "entered", "invocation_state": "resolved"},
        {"native_startup_phase": "bridge_attached"})
    assert before_polling == {
        "startup_boundary": "warm_load_pending",
        "startup_phase": "warm_load",
        "runtime_provisioning_state": "provisioning",
        "warm_load_state": "pending",
        "worker_state": "provisioning",
        "worker_error_code": "none",
        "bridge_exit_posture": "not_observed",
        "relay_polling_state": "not_started",
        "registration_state": "not_reached",
    }

    after_polling = desktop_runner._read_packaged_startup_diagnostic(
        _packaged_status_driver({
            "Startup phase": "ready", "Provisioning state": "ready",
            "Relay runtime state": "processing", "Worker state": "ready",
            "Last worker error code": "none", "Last worker exit code": "none",
            "Registered": "yes (1/1 relays)",
        }), {"start_handler_state": "entered", "invocation_state": "resolved"},
        {"native_startup_phase": "running_status_publication"})
    assert after_polling["relay_polling_state"] == "started"
    assert after_polling["registration_state"] == "registered"
    assert after_polling["startup_boundary"] == "registered"


def test_packaged_startup_uses_authoritative_registration_when_ui_lags(desktop_runner):
    diagnostic = desktop_runner._read_packaged_startup_diagnostic(
        _packaged_status_driver({
            "Startup phase": "ready", "Provisioning state": "provisioning",
            "Relay runtime state": "ready", "Worker state": "ready",
            "Last worker error code": "none", "Last worker exit code": "none",
            "Registered": "no (0/1 relays)",
        }), {"start_handler_state": "entered", "invocation_state": "resolved"},
        {"native_startup_phase": "bridge_attached"}, relay_observation="registered")

    assert diagnostic["runtime_provisioning_state"] == "provisioning"
    assert diagnostic["worker_state"] == "ready"
    assert diagnostic["relay_polling_state"] == "started"
    assert diagnostic["registration_state"] == "registered"
    assert diagnostic["startup_boundary"] == "registered"


def test_packaged_startup_authoritative_negative_overrides_stale_ui(desktop_runner):
    diagnostic = desktop_runner._read_packaged_startup_diagnostic(
        _packaged_status_driver({
            "Relay runtime state": "ready", "Registered": "yes (1/1 relays)"}),
        {"start_handler_state": "entered", "invocation_state": "resolved"},
        {"native_startup_phase": "running_status_publication"},
        relay_observation="not_reached")
    assert diagnostic["relay_polling_state"] == "started"
    assert diagnostic["registration_state"] == "not_reached"
    assert diagnostic["startup_boundary"] == "registration_not_reached"


def test_operator_start_diagnostic_is_preserved_in_failure_artifact(desktop_runner, tmp_path):
    desktop_runner.LOGS_DIR = tmp_path
    diagnostic = {
        "start_handler_state": "entered",
        "invocation_state": "rejected",
        "native_event_observation": "running_received",
        "polling_observation": "command_failed",
        "render_state": "not_running",
        "private_field": "C:\\private\\model.gguf prompt SECRET",
    }
    desktop_runner._write_webdriver_diagnostic(
        "match", "running", "operator_running_not_reached",
        operator_start_diagnostic=diagnostic)

    artifact_text = (tmp_path / "packaged-webdriver-diagnostic.json").read_text()
    artifact = json.loads(artifact_text)
    assert {field: artifact[field] for field in diagnostic if field != "private_field"} == {
        field: value for field, value in diagnostic.items() if field != "private_field"
    }
    assert "private" not in artifact_text
    assert "SECRET" not in artifact_text


def test_post_start_operator_state_records_running_and_registration(desktop_runner):
    progress = []
    relay_observations = []
    desktop_runner._read_operator_start_diagnostic = lambda _driver: {
        "start_handler_state": "entered", "invocation_state": "resolved"}
    desktop_runner.wait_for_running_stability = lambda *_args, **_kwargs: None
    desktop_runner._status_value = lambda _driver, label: "yes" if label == "Registered" else "no"
    desktop_runner.fetch_relay_diagnostics_count = lambda *_args, **_kwargs: 1

    class Wait:
        def __init__(self, _driver, timeout, **_kwargs):
            assert 0 < timeout <= 9

        def until(self, predicate):
            assert predicate(object()) is True

    desktop_runner.WebDriverWait = Wait
    desktop_runner.wait_for_post_start_operator_state(
        object(), lambda: 9, progress.append, pytest.fail, "https://relay.example",
        relay_observations.append)

    assert progress == ["operator_running", "operator_registered"]
    assert relay_observations == ["polled", "registered"]


def test_clean_relay_baseline_rejects_preexisting_unrelated_node(desktop_runner):
    desktop_runner.fetch_relay_diagnostics_count = lambda *_args, **_kwargs: 4
    def fail_closed(reason):
        raise RuntimeError(reason) from None
    observations = []
    with pytest.raises(RuntimeError, match="^operator_registration_not_reached$"):
        desktop_runner.require_clean_relay_registration_baseline(
            "https://relay.example", timeout_seconds=0.5, fail_closed=fail_closed,
            record_relay_observation=observations.append)
    assert observations == ["polled", "not_reached"]


def test_relay_baseline_retries_transient_failure(desktop_runner):
    results = iter((TimeoutError("transient"), 0))
    def fetch(*_args, **_kwargs):
        result = next(results)
        if isinstance(result, Exception):
            raise result
        return result
    desktop_runner.fetch_relay_diagnostics_count = fetch
    observations = []
    baseline = desktop_runner.require_clean_relay_registration_baseline(
        "https://relay.example", timeout_seconds=1, fail_closed=pytest.fail,
        record_relay_observation=observations.append)
    assert baseline == 0
    assert observations == ["polled", "polled"]


def test_post_start_operator_state_accepts_terminal_authoritative_registration(
        desktop_runner, monkeypatch):
    progress = []
    relay_observations = []
    clock = [100.0]
    monkeypatch.setattr(desktop_runner.time, "monotonic", lambda: clock[0])
    desktop_runner._read_operator_start_diagnostic = lambda _driver: {
        "start_handler_state": "entered", "invocation_state": "resolved"}
    desktop_runner.wait_for_running_stability = lambda *_args, **_kwargs: None
    fetches = []
    counts = iter((0, 1))
    def fetch(_relay_url, timeout_seconds):
        fetches.append((clock[0], timeout_seconds))
        return next(counts)
    desktop_runner.fetch_relay_diagnostics_count = fetch

    class Wait:
        calls = 0

        def __init__(self, _driver, _timeout, **_kwargs):
            pass

        def until(self, predicate):
            type(self).calls += 1
            if self.calls == 1:
                assert predicate(object()) is True
                return True
            assert predicate(object()) is False
            clock[0] = 100.5
            raise TimeoutError("ordinary polling expired")
    desktop_runner.WebDriverWait = Wait
    desktop_runner.wait_for_post_start_operator_state(
        object(), lambda: 9, progress.append, pytest.fail, "https://relay.example",
        relay_observations.append)
    assert progress == ["operator_running", "operator_registered"]
    assert relay_observations == ["polled", "polled", "registered"]
    assert fetches[0][0] < fetches[1][0]
    assert 0 < fetches[1][1] <= 101.0 - fetches[1][0]


@pytest.mark.parametrize("terminal_result", [0, RuntimeError(
    "https://hostile.example/private prompt-secret")])
def test_post_start_operator_state_terminal_authoritative_failure_is_sanitized(
        desktop_runner, monkeypatch, terminal_result):
    observations = []
    progress = []
    clock = [100.0]
    monkeypatch.setattr(desktop_runner.time, "monotonic", lambda: clock[0])
    desktop_runner._read_operator_start_diagnostic = lambda _driver: {
        "start_handler_state": "entered", "invocation_state": "resolved"}
    desktop_runner.wait_for_running_stability = lambda *_args, **_kwargs: None
    fetches = []

    def fetch(_relay_url, timeout_seconds):
        fetches.append((clock[0], timeout_seconds))
        if len(fetches) == 1:
            return 0
        if isinstance(terminal_result, Exception):
            raise terminal_result
        return terminal_result

    desktop_runner.fetch_relay_diagnostics_count = fetch

    class Wait:
        calls = 0

        def __init__(self, _driver, _timeout, **_kwargs):
            pass

        def until(self, predicate):
            type(self).calls += 1
            if self.calls == 1:
                assert predicate(object()) is True
                return True
            assert predicate(object()) is False
            clock[0] = 100.5
            raise TimeoutError("https://hostile.example/ordinary prompt-secret")

    desktop_runner.WebDriverWait = Wait

    def fail_closed(reason):
        raise RuntimeError(reason) from None

    with pytest.raises(RuntimeError, match="^operator_registration_not_reached$") as raised:
        desktop_runner.wait_for_post_start_operator_state(
            object(), lambda: 1, progress.append, fail_closed, "https://hostile.example",
            observations.append)

    assert len(fetches) == 2
    assert fetches[0][0] < fetches[1][0]
    assert 0 < fetches[1][1] <= 101.0 - fetches[1][0]
    assert observations == ["polled", "polled", "not_reached"]
    assert progress == ["operator_running"]
    assert "hostile.example" not in str(raised.value)
    assert "prompt-secret" not in str(raised.value)


def test_post_start_operator_state_retries_transient_relay_failure(desktop_runner):
    progress = []
    observations = []
    desktop_runner._read_operator_start_diagnostic = lambda _driver: {
        "start_handler_state": "entered", "invocation_state": "resolved"}
    desktop_runner.wait_for_running_stability = lambda *_args, **_kwargs: None
    results = iter((TimeoutError("hostile relay detail"), 1))
    def fetch(*_args, **_kwargs):
        result = next(results)
        if isinstance(result, Exception):
            raise result
        return result
    desktop_runner.fetch_relay_diagnostics_count = fetch

    class Wait:
        calls = 0
        def __init__(self, _driver, _timeout, **_kwargs):
            pass
        def until(self, predicate):
            type(self).calls += 1
            if self.calls == 1:
                assert predicate(object()) is True
                return True
            assert predicate(object()) is False
            assert predicate(object()) is True
            return True

    desktop_runner.WebDriverWait = Wait
    desktop_runner.wait_for_post_start_operator_state(
        object(), lambda: 9, progress.append, pytest.fail, "https://hostile.example",
        observations.append)
    assert progress == ["operator_running", "operator_registered"]
    assert observations == ["polled", "polled", "registered"]


def test_post_start_operator_state_distinguishes_running_failure(desktop_runner):
    private_error = "C:\\private\\model.gguf prompt-secret"
    desktop_runner._read_operator_start_diagnostic = lambda _driver: {
        "start_handler_state": "entered", "invocation_state": "resolved"}
    class Wait:
        def __init__(self, _driver, _timeout, **_kwargs):
            pass

        def until(self, predicate):
            assert predicate(object()) is True
            return True

    desktop_runner.WebDriverWait = Wait
    desktop_runner.wait_for_running_stability = lambda *_args, **_kwargs: (
        (_ for _ in ()).throw(RuntimeError(private_error)))
    failures = []

    def fail_closed(reason):
        failures.append(reason)
        raise RuntimeError(reason) from None

    with pytest.raises(RuntimeError, match="^operator_running_not_reached$") as raised:
        desktop_runner.wait_for_post_start_operator_state(
            object(), lambda: 9, pytest.fail, fail_closed, "https://relay.example",
            lambda _observation: None)

    assert failures == ["operator_running_not_reached"]
    assert private_error not in str(raised.value)


def test_post_start_operator_state_distinguishes_registration_failure(desktop_runner):
    progress = []
    desktop_runner._read_operator_start_diagnostic = lambda _driver: {
        "start_handler_state": "entered", "invocation_state": "resolved"}
    desktop_runner.wait_for_running_stability = lambda *_args, **_kwargs: None

    class Wait:
        calls = 0

        def __init__(self, _driver, _timeout, **_kwargs):
            pass

        def until(self, predicate):
            type(self).calls += 1
            if self.calls == 1:
                assert predicate(object()) is True
                return True
            raise RuntimeError("https://private.example prompt-secret")

    desktop_runner.WebDriverWait = Wait
    desktop_runner.fetch_relay_diagnostics_count = lambda *_args, **_kwargs: 0

    def fail_closed(reason):
        raise RuntimeError(reason) from None

    with pytest.raises(RuntimeError, match="^operator_registration_not_reached$") as raised:
        desktop_runner.wait_for_post_start_operator_state(
            object(), lambda: 9, progress.append, fail_closed, "https://relay.example",
            lambda _observation: None)

    assert progress == ["operator_running"]
    assert "private.example" not in str(raised.value)


def test_post_start_operator_state_rejects_optimistic_running_without_active_attempt(
        desktop_runner):
    desktop_runner._read_operator_start_diagnostic = lambda _driver: {
        "start_handler_state": "not_entered", "invocation_state": "not_started"}
    desktop_runner.wait_for_running_stability = lambda *_args, **_kwargs: pytest.fail(
        "stale Running state must not be inspected before active-attempt evidence")

    class Wait:
        def __init__(self, _driver, _timeout, **_kwargs):
            pass

        def until(self, predicate):
            assert predicate(object()) is False
            raise RuntimeError("bounded active-attempt wait expired")

    desktop_runner.WebDriverWait = Wait

    def fail_closed(reason):
        raise RuntimeError(reason) from None

    with pytest.raises(RuntimeError, match="^operator_running_not_reached$"):
        desktop_runner.wait_for_post_start_operator_state(
            object(), lambda: 9, pytest.fail, fail_closed, "https://relay.example",
            lambda _observation: None)


def test_post_start_operator_state_reports_missing_active_attempt_before_running(
        desktop_runner):
    desktop_runner._read_operator_start_diagnostic = lambda _driver: {
        "start_handler_state": "not_entered", "invocation_state": "not_started"}
    desktop_runner.wait_for_running_stability = lambda *_args, **_kwargs: None
    failures = []

    class Wait:
        calls = 0

        def __init__(self, _driver, _timeout, **_kwargs):
            pass

        def until(self, predicate):
            type(self).calls += 1
            if self.calls == 1:
                assert predicate(object()) is False
                raise RuntimeError("private stale-state detail")
            return True

    desktop_runner.WebDriverWait = Wait
    desktop_runner._status_value = lambda _driver, _label: "yes"
    desktop_runner.wait_for_post_start_operator_state(
        object(), lambda: 9, lambda _progress: None, failures.append,
        "https://relay.example", lambda _observation: None)

    assert failures == ["operator_running_not_reached"]


def test_tauri_driver_environment_removes_poisoned_tokenizer_handoff(
        desktop_runner, monkeypatch, tmp_path):
    keys = (
        "TOKEN_PLACE_LONG_CONTEXT_BENCHMARK_TOKENIZER_REQUEST",
        "TOKEN_PLACE_LONG_CONTEXT_BENCHMARK_TOKENIZER_EVIDENCE",
        "TOKEN_PLACE_PYTHON",
        "TOKEN_PLACE_SIDECAR_PYTHON",
    )
    for key in keys:
        monkeypatch.setenv(key, f"poisoned {key}")

    isolated_home = tmp_path / "isolated home"
    env = desktop_runner.tauri_driver_environment(isolated_home)

    assert all(key not in env for key in keys)
    assert env["HOME"] == str(isolated_home)
    assert env["XDG_CONFIG_HOME"] == str(isolated_home / ".config")
    assert env["XDG_DATA_HOME"] == str(isolated_home / ".local/share")
    assert env["APPDATA"] == str(isolated_home / "AppData/Roaming")
    assert env["WEBVIEW2_USER_DATA_FOLDER"] == str(
        (isolated_home / "WebView2").resolve(strict=True))
    assert all(path.is_dir() for path in (
        isolated_home,
        isolated_home / ".config",
        isolated_home / ".local/share",
        isolated_home / "AppData/Roaming",
        isolated_home / "WebView2",
    ))




















def test_tokenizer_stage_is_bounded_atomic_and_rearmed(desktop_runner, tmp_path):
    evidence = tmp_path / "sentinel-private-path-payload.json"
    desktop_runner._write_tokenizer_stage(evidence, "python_producer_not_invoked", 35)
    stage = desktop_runner.tokenizer_stage_path(evidence)
    assert json.loads(stage.read_text(encoding="utf-8")) == {
        "version": 1, "stage": 35, "category": "python_producer_not_invoked"}
    assert "sentinel-private-path-payload" not in stage.read_text(encoding="utf-8")
    assert desktop_runner._read_tokenizer_stage(evidence) == "python_producer_not_invoked"
    assert not list(tmp_path.glob(".tokenizer-runner-stage-*.tmp"))


def test_tokenizer_stage_rejects_non_allowlisted_category(desktop_runner, tmp_path):
    with pytest.raises(ValueError, match="invalid tokenizer stage category"):
        desktop_runner._write_tokenizer_stage(
            tmp_path / "evidence.json", "sentinel-private-category", 35)
    assert not list(tmp_path.iterdir())


def test_tokenizer_stage_rejects_missing_file(desktop_runner, tmp_path):
    with pytest.raises(RuntimeError, match="application_arguments_absent"):
        desktop_runner._read_tokenizer_stage(tmp_path / "missing-evidence.json")


@pytest.mark.parametrize("value", [[], "malformed", None, 7, {"version": 1}])
def test_tokenizer_stage_rejects_non_object_or_malformed_json(
        desktop_runner, tmp_path, value):
    evidence = tmp_path / "evidence.json"
    desktop_runner.tokenizer_stage_path(evidence).write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(RuntimeError, match="application_arguments_malformed"):
        desktop_runner._read_tokenizer_stage(evidence)


@pytest.mark.parametrize("stage", [True, 29, 31])
def test_tokenizer_stage_rejects_bool_or_mismatched_stage(desktop_runner, tmp_path, stage):
    evidence = tmp_path / "evidence.json"
    desktop_runner.tokenizer_stage_path(evidence).write_text(json.dumps({
        "version": 1, "stage": stage, "category": "python_handoff_received"}),
        encoding="utf-8")
    with pytest.raises(RuntimeError, match="application_arguments_malformed"):
        desktop_runner._read_tokenizer_stage(evidence)


def test_tokenizer_stage_runner_boundaries_are_directly_enforced(desktop_runner, tmp_path):
    evidence = tmp_path / "evidence.json"
    stage_path = desktop_runner.tokenizer_stage_path(evidence)

    def publish(category, stage):
        stage_path.write_text(json.dumps({
            "version": 1, "stage": stage, "category": category,
        }), encoding="utf-8")

    def fail_closed(reason):
        raise RuntimeError(reason)

    for category, stage, expected in (
        ("application_arguments_absent", 0, "application_arguments_absent"),
        ("application_arguments_malformed", 10, "application_arguments_malformed"),
        ("application_arguments_accepted", 10, "rust_python_handoff_failed"),
    ):
        publish(category, stage)
        with pytest.raises(RuntimeError, match=expected):
            desktop_runner._validate_operator_tokenizer_handoff(evidence, fail_closed)

    publish("python_handoff_received", 30)
    desktop_runner._validate_operator_tokenizer_handoff(evidence, fail_closed)
    stage_path.unlink()
    with pytest.raises(RuntimeError, match="application_arguments_absent"):
        desktop_runner._validate_operator_tokenizer_handoff(evidence, fail_closed)
    recorded = []
    desktop_runner._validate_operator_tokenizer_handoff(evidence, recorded.append)
    assert recorded == ["application_arguments_absent"]

    driver_log = tmp_path / "driver.log"
    driver_log.write_bytes(b"bounded-log")
    assert desktop_runner._rearm_tokenizer_stage(evidence, driver_log) == 11
    assert desktop_runner._read_tokenizer_stage(evidence) == "python_producer_not_invoked"

    with pytest.raises(RuntimeError, match="python_producer_not_invoked"):
        desktop_runner._validate_final_tokenizer_stage(evidence, fail_closed)
    publish("authoritative_evidence_published", 100)
    desktop_runner._validate_final_tokenizer_stage(evidence, fail_closed)
    stage_path.write_text("not-json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="application_arguments_absent"):
        desktop_runner._validate_final_tokenizer_stage(evidence, fail_closed)
    recorded.clear()
    desktop_runner._validate_final_tokenizer_stage(evidence, recorded.append)
    assert recorded == ["application_arguments_absent"]


@pytest.mark.parametrize(("category", "stage"), [
    ("application_arguments_accepted", 10),
    ("rust_python_handoff_failed", 20),
    ("rust_python_handoff_accepted", 20),
])
def test_tokenizer_handoff_converts_incomplete_stages_to_specific_failure(
        desktop_runner, tmp_path, category, stage):
    evidence = tmp_path / "private-request-path.json"
    desktop_runner.tokenizer_stage_path(evidence).write_text(json.dumps({
        "version": 1, "stage": stage, "category": category,
    }), encoding="utf-8")
    failures = []

    desktop_runner._validate_operator_tokenizer_handoff(evidence, failures.append)

    assert failures == ["rust_python_handoff_failed"]
    assert "private-request-path" not in failures[0]


@pytest.mark.parametrize(("category", "stage"), [
    ("python_producer_not_invoked", 35),
    ("request_validation_failure", 40),
    ("fixture_hash_validation_failure", 50),
    ("active_runtime_tokenizer_unavailable", 60),
    ("tokenization_failure", 65),
    ("runtime_identity_unavailable", 70),
    ("evidence_publication_failure", 90),
])
def test_tokenizer_stage_finalization_preserves_allowlisted_producer_failure(
        desktop_runner, tmp_path, category, stage):
    evidence = tmp_path / "payload-secret-exception-text.json"
    desktop_runner.tokenizer_stage_path(evidence).write_text(json.dumps({
        "version": 1, "stage": stage, "category": category,
    }), encoding="utf-8")
    failures = []

    desktop_runner._validate_final_tokenizer_stage(evidence, failures.append)

    assert failures == [category]
    assert all(sentinel not in failures[0]
        for sentinel in ("payload", "secret", "exception", str(evidence)))


def _phase_write(desktop_runner, path, *, clock, sleeper, platform_name="nt"):
    desktop_runner._write_benchmark_phase(path, "runner_startup", 0.0,
        h.PACKAGED_PHASE_STATUS_VERSION, h.PACKAGED_PHASES,
        last_safe_phase="runner_startup", clock=clock, sleeper=sleeper,
        retry_timeout_s=0.03, platform_name=platform_name)


def _sharing_violation(message="locked"):
    error = PermissionError(message)
    error.winerror = 32
    return error


def _permission_denial(message="access denied"):
    return PermissionError(message)


@pytest.mark.parametrize("denials", [1, 3])
@pytest.mark.parametrize("denial", [_sharing_violation, _permission_denial])
def test_phase_checkpoint_retries_windows_contention_atomically(
        desktop_runner, monkeypatch, tmp_path, denials, denial):
    destination = tmp_path / "phase.json"
    destination.write_text('{"stale": true}')
    original_replace = Path.replace
    attempts = []
    now = [0.0]
    def replace(path, target):
        attempts.append(path)
        if len(attempts) <= denials:
            raise denial("locked raw detail")
        return original_replace(path, target)
    monkeypatch.setattr(Path, "replace", replace)
    _phase_write(desktop_runner, destination, clock=lambda: now[0],
        sleeper=lambda delay: now.__setitem__(0, now[0] + delay))
    checkpoint = json.loads(destination.read_text())
    assert checkpoint["phase"] == "runner_startup"
    assert len(attempts) == denials + 1
    assert not list(tmp_path.glob(".phase.json.*.tmp"))


def test_phase_checkpoint_windows_permission_deadline_is_bounded_and_sanitized(
        desktop_runner, monkeypatch, tmp_path):
    destination = tmp_path / "private-phase.json"
    now = [0.0]
    attempts = []
    def denied(path, target):
        attempts.append((path, target))
        raise _permission_denial("C:/private/raw access denial")
    monkeypatch.setattr(Path, "replace", denied)
    with pytest.raises(RuntimeError) as raised:
        _phase_write(desktop_runner, destination, clock=lambda: now[0],
            sleeper=lambda delay: now.__setitem__(0, now[0] + delay))
    assert str(raised.value) == "phase checkpoint publication failed"
    assert len(attempts) == 4
    assert "private" not in str(raised.value)
    assert not list(tmp_path.glob(f".{destination.name}.*.tmp"))


def test_webview2_devtools_detects_owned_application_exit_after_poll(
        desktop_runner, monkeypatch):
    polls = iter([None, 7])
    clock = SimpleNamespace(value=0.0)
    def monotonic():
        clock.value += 0.01
        return clock.value
    monkeypatch.setattr(desktop_runner.time, "monotonic", monotonic)
    monkeypatch.setattr(desktop_runner.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(desktop_runner, "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("private response")))

    with pytest.raises(RuntimeError, match="^webdriver_application_startup_failed$") as raised:
        desktop_runner.wait_for_webview2_devtools(
            SimpleNamespace(poll=lambda: next(polls)), 49152, 1)

    assert "private" not in str(raised.value)


def test_phase_checkpoint_does_not_retry_unrelated_error(
        desktop_runner, monkeypatch, tmp_path):
    attempts = []
    def invalid(_path, _target):
        attempts.append(True)
        raise PermissionError("deterministic access denial")
    monkeypatch.setattr(Path, "replace", invalid)
    with pytest.raises(PermissionError, match="deterministic access denial"):
        _phase_write(desktop_runner, tmp_path / "phase.json",
            clock=lambda: 0.0, sleeper=lambda _delay: pytest.fail("slept"),
            platform_name="posix")
    assert attempts == [True]
    destination = tmp_path / "phase.json"
    assert not list(tmp_path.glob(f".{destination.name}.*.tmp"))


def test_phase_checkpoint_does_not_retry_unrelated_oserror(
        desktop_runner, monkeypatch, tmp_path):
    attempts = []
    def invalid(_path, _target):
        attempts.append(True)
        raise OSError("unrelated filesystem error")
    monkeypatch.setattr(Path, "replace", invalid)
    with pytest.raises(OSError, match="unrelated filesystem error"):
        _phase_write(desktop_runner, tmp_path / "phase.json",
            clock=lambda: 0.0, sleeper=lambda _delay: pytest.fail("slept"))
    assert attempts == [True]


def test_phase_checkpoint_temp_cleanup_reuses_publication_deadline(
        desktop_runner, monkeypatch, tmp_path):
    now = [0.0]
    monkeypatch.setattr(Path, "replace", lambda *_args: (_ for _ in ()).throw(
        _sharing_violation("locked")))
    monkeypatch.setattr(Path, "unlink", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        _sharing_violation("still locked")))
    with pytest.raises(RuntimeError, match="phase checkpoint publication failed"):
        _phase_write(desktop_runner, tmp_path / "phase.json", clock=lambda: now[0],
            sleeper=lambda delay: now.__setitem__(0, now[0] + delay))
    assert now[0] == pytest.approx(0.03)


def test_phase_checkpoint_windows_permission_cleanup_does_not_mask_publication_failure(
        desktop_runner, monkeypatch, tmp_path):
    now = [0.0]
    monkeypatch.setattr(Path, "replace", lambda *_args: (_ for _ in ()).throw(
        _permission_denial("C:/private/publication")))
    monkeypatch.setattr(Path, "unlink", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        _permission_denial("C:/private/cleanup")))
    with pytest.raises(RuntimeError) as raised:
        _phase_write(desktop_runner, tmp_path / "phase.json", clock=lambda: now[0],
            sleeper=lambda delay: now.__setitem__(0, now[0] + delay))
    assert str(raised.value) == "phase checkpoint publication failed"
    assert "private" not in str(raised.value)


@pytest.mark.parametrize("sharing_denials", [0, 1])
def test_phase_checkpoint_temp_cleanup_rejects_unrelated_errors_after_bounded_retry(
        desktop_runner, monkeypatch, tmp_path, sharing_denials):
    destination = tmp_path / "phase.json"
    now = [0.0]
    monkeypatch.setattr(Path, "replace", lambda *_args: (_ for _ in ()).throw(
        PermissionError("publication denied")))
    original_unlink = Path.unlink
    attempts = []

    def unlink(path, *args, **kwargs):
        if path.name.startswith(f".{destination.name}."):
            attempts.append(True)
            if len(attempts) <= sharing_denials:
                raise _sharing_violation()
            raise PermissionError("cleanup denied")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(PermissionError, match="cleanup denied"):
        _phase_write(desktop_runner, destination, clock=lambda: now[0],
            sleeper=lambda delay: now.__setitem__(0, now[0] + delay),
            platform_name="posix")
    assert len(attempts) == sharing_denials + 1
    assert now[0] == pytest.approx(0.01 * sharing_denials)


def test_owned_file_removal_retries_sharing_denial_without_real_sleep(
        desktop_runner, monkeypatch, tmp_path):
    path = tmp_path / "driver.log"
    path.write_text("bounded diagnostic")
    original_unlink = Path.unlink
    attempts = []
    now = [0.0]
    def unlink(candidate, *args, **kwargs):
        if candidate == path:
            attempts.append(True)
            if len(attempts) == 1:
                raise _sharing_violation("locked")
        return original_unlink(candidate, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", unlink)
    assert desktop_runner._remove_owned_path(path, 0.03, clock=lambda: now[0],
        sleeper=lambda delay: now.__setitem__(0, now[0] + delay)) is True
    assert attempts == [True, True]
    assert not path.exists()


def test_owned_file_removal_permanent_lock_is_bounded(desktop_runner, monkeypatch, tmp_path):
    path = tmp_path / "secret-driver.log"
    path.write_text("bounded diagnostic")
    now = [0.0]
    attempts = []
    def denied(_candidate, *args, **kwargs):
        attempts.append(True)
        raise _sharing_violation("C:/private/secret-driver.log")
    monkeypatch.setattr(Path, "unlink", denied)
    assert desktop_runner._remove_owned_path(path, 0.02, clock=lambda: now[0],
        sleeper=lambda delay: now.__setitem__(0, now[0] + delay)) is False
    assert len(attempts) == 3


def test_owned_directory_removal_handles_absence_but_not_unrelated_denial(
        desktop_runner, monkeypatch, tmp_path):
    missing = tmp_path / "missing"
    assert desktop_runner._remove_owned_path(missing, 1.0, directory=True,
        clock=lambda: 0.0, sleeper=lambda _delay: pytest.fail("slept")) is True
    monkeypatch.setattr(desktop_runner.shutil, "rmtree", lambda _path: (_ for _ in ()).throw(
        PermissionError("unrelated denial")))
    with pytest.raises(PermissionError, match="unrelated denial"):
        desktop_runner._remove_owned_path(missing, 1.0, directory=True,
            clock=lambda: 0.0, sleeper=lambda _delay: pytest.fail("slept"))


def test_webdriver_quit_is_attempted_after_timeout_configuration_failure(desktop_runner):
    calls = []
    class Session:
        def set_script_timeout(self, timeout):
            calls.append(("timeout", timeout))
            raise RuntimeError("disconnected")

        def quit(self):
            calls.append(("quit",))

    assert desktop_runner._quit_webdriver(Session(), 2.5) is False
    assert calls == [("timeout", 2.5), ("quit",)]


def test_webdriver_quit_failure_is_reported(desktop_runner):
    session = SimpleNamespace(set_script_timeout=lambda _timeout: None,
        quit=lambda: (_ for _ in ()).throw(RuntimeError("disconnected")))
    assert desktop_runner._quit_webdriver(session, 2.5) is False


def test_phase_reader_treats_sharing_denial_and_partial_json_as_retryable(monkeypatch, tmp_path):
    path = tmp_path / "phase.json"
    valid = {"schema_version": h.PACKAGED_PHASE_STATUS_VERSION,
        "phase": "runner_startup", "sequence": 1,
        "last_safe_phase": "runner_startup", "failure_reason": None,
        "elapsed_s": 0.0, "cleanup_succeeded": None}
    observations = [_sharing_violation("sharing violation"), '{"schema_version":',
        json.dumps(valid)]
    def read_text(_path, **_kwargs):
        observation = observations.pop(0)
        if isinstance(observation, Exception):
            raise observation
        return observation
    monkeypatch.setattr(Path, "read_text", read_text)
    assert h._read_packaged_phase_status(path, 1.0) == (
        None, "packaged_phase_status_missing")
    assert h._read_packaged_phase_status(path, 1.0) == (
        None, "packaged_phase_status_missing")
    assert h._read_packaged_phase_status(path, 1.0) == (valid, None)


def test_phase_reader_does_not_hide_unrelated_io_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "read_text", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        PermissionError("unrelated denial")))
    with pytest.raises(PermissionError, match="unrelated denial"):
        h._read_packaged_phase_status(tmp_path / "phase.json", 1.0)

def _memory_evidence(*, baseline=100, peak=300, final=200, samples=3, platform="linux"):
    return {"method": h.MEMORY_METHOD, "scope": h.MEMORY_SCOPE, "platform": platform,
        "sample_count": samples, "baseline_rss_bytes": baseline,
        "peak_rss_bytes": peak, "final_rss_bytes": final}


def _runtime_configuration(backend="cpu", tier="64k-full", window=65536, qwen=False):
    na = {"status": "not_applicable", "reason": "not_qwen_64k_profile"}
    result = {"mode": {"requested": "cpu" if backend == "cpu" else "gpu",
            "effective": backend},
        "backend": {"requested": backend, "available": backend, "selected": backend,
            "used": backend, "fallback_reason": "none"},
        "context": {"tier": tier, "effective_window_tokens": window},
        "runtime_profile": dict(na), "batch_profile": dict(na), "kv_cache": dict(na),
        "acceleration": dict(na), "yarn_rope": dict(na)}
    if qwen:
        result.update({"runtime_profile": {
                "selected": "qwen64k_kv_q8_fa_balanced_batch",
                "preferred": "qwen64k_kv_q8_fa_balanced_batch",
                "attempted": ["qwen64k_kv_q8_fa_balanced_batch"], "recovery_count": 0,
                "result": "passed", "fallback_reason": "none"},
            "batch_profile": {"requested": "balanced", "selected": "balanced",
                "n_batch": 512, "n_ubatch": 128},
            "kv_cache": {"precision": "q8", "type_k": 8, "type_v": 8,
                "device": backend},
            "acceleration": {"flash_attention": True, "kqv_offload": True,
                "offloaded_layers": "all_supported_layers"},
            "yarn_rope": {"requested_context_tokens": 65536,
                "original_context_tokens": 32768, "context_multiplier": 2.0,
                "rope_frequency_scale": 0.5, "extension_factor_overridden": False,
                "scaling_source": "top_level_enum", "configuration_valid": True}})
    return result


def _qwen_kv_summary(backend="metal"):
    attestation = {"method":"active_runtime_selected_profile", "applicability":"qwen_64k_full",
        "architecture":"qwen3", "profile_id":"qwen64k_kv_q8_fa_balanced_batch",
        "backend":backend, "context_tier":"64k-full", "context_size_tokens":65536}
    return {"pass":True, "applicability":"qwen_64k_full",
        "profile_id":"qwen64k_kv_q8_fa_balanced_batch", "backend":backend,
        "context_size_tokens":65536, "type_k":"q8", "type_v":"q8",
        "estimated_bytes":104857600, "observed_bytes":104857600, "delta_bytes":0,
        "precision_interval_bytes":[104852357, 104862843], "precision_bytes":5243,
        "record_count":1, "decimal_places":2,
        "estimator_provenance":"qwen_selected_profile_gguf_header",
        "runtime_provenance":"pinned_llama_cpp_kv_buffer_diagnostic", "attestation":attestation}


def _packaged_configuration_builder():
    source = (Path(__file__).parents[2] / "desktop-tauri" / "scripts" /
        "test_desktop_operator_ui_e2e.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {"_diagnostic_bool", "_diagnostic_int", "_diagnostic_float",
        "_normalize_profile_fallback_reason", "packaged_runtime_configuration"}
    nodes = [item for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name in names]
    namespace = {"__builtins__": __builtins__, "math": __import__("math")}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<packaged-configuration>", "exec"),
        namespace)
    return namespace["packaged_runtime_configuration"]


def _packaged_runtime_labels(backend="metal", *, tier="64k-full", window=65536):
    return {"Requested mode": "GPU" if backend != "cpu" else "CPU",
        "Effective mode": backend, "Backend available": backend,
        "Backend selected": backend, "Backend used": backend, "Fallback reason": "none",
        "Context tier": tier, "Context window": f"{window} tokens"}


def _qwen_readiness_diagnostics(*, result="passed", fallback="null"):
    return {"api_v1_readiness_qwen_64k_runtime_profile_id":
            "qwen64k_kv_q8_fa_balanced_batch",
        "api_v1_readiness_qwen_64k_runtime_preferred_profile_id":
            "qwen64k_kv_q8_fa_balanced_batch",
        "api_v1_readiness_qwen_64k_runtime_profile_attempt_ids":
            "qwen64k_kv_q8_fa_balanced_batch",
        "api_v1_readiness_qwen_64k_runtime_profile_recovery_count": "0",
        "api_v1_readiness_qwen_64k_runtime_profile_result": result,
        "api_v1_readiness_qwen_64k_runtime_profile_fallback_reason": fallback,
        "api_v1_readiness_qwen_64k_batch_profile_requested": "balanced",
        "api_v1_readiness_qwen_64k_batch_profile_selected": "balanced",
        "api_v1_readiness_qwen_64k_runtime_profile_n_batch": "512",
        "api_v1_readiness_qwen_64k_runtime_profile_n_ubatch": "128",
        "api_v1_readiness_qwen_64k_runtime_profile_kv_precision": "q8",
        "api_v1_readiness_qwen_64k_runtime_profile_type_k": "8",
        "api_v1_readiness_qwen_64k_runtime_profile_type_v": "8",
        "api_v1_readiness_qwen_64k_runtime_profile_flash_attn": "true",
        "api_v1_readiness_qwen_64k_runtime_profile_offload_kqv": "true",
        "kv_cache_device": "metal", "offloaded_layers": "all_supported_layers",
        "api_v1_readiness_yarn_requested_context_tokens": "65536",
        "api_v1_readiness_yarn_original_context_tokens": "32768",
        "api_v1_readiness_yarn_context_multiplier": "2.0",
        "api_v1_readiness_yarn_rope_freq_scale": "0.5",
        "api_v1_readiness_yarn_ext_factor_overridden": "false",
        "api_v1_readiness_yarn_rope_scaling_type_source": "top_level_enum",
        "api_v1_readiness_yarn_configuration_valid": "true"}


def test_desktop_runner_uses_evergreen_generation_settings_probe_name():
    source = (Path(__file__).parents[2] / "desktop-tauri" / "scripts" /
        "test_desktop_operator_ui_e2e.py").read_text(encoding="utf-8")
    assert "__p8" not in source
    assert source.count("__longContextBenchmarkGenerationSettings") == 3


def test_packaged_profile_fallback_normalizes_only_producer_absence_values():
    source = (Path(__file__).parents[2] / "desktop-tauri" / "scripts" /
        "test_desktop_operator_ui_e2e.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(item for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "_normalize_profile_fallback_reason")
    namespace = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<fallback-normalizer>", "exec"),
        {"__builtins__": __builtins__}, namespace)
    normalize = namespace["_normalize_profile_fallback_reason"]
    assert [normalize(value) for value in (None, "", "null")] == ["none"] * 3
    assert normalize("capability_incompatibility") == "capability_incompatibility"
    assert normalize("arbitrary") == "arbitrary"


def test_matrix_plan_is_deterministic_complete_and_duplicate_free():
    first = h.build_matrix_plan()
    second = h.build_matrix_plan()
    assert first == second
    h.validate_matrix_plan(first)
    cells = first["cells"]
    assert len(cells) == 5 * 7
    assert len({h._canonical_json(cell) for cell in cells}) == len(cells)
    for platform_name, backend, package in h.MATRIX_PACKAGED_BACKENDS:
        scoped = [cell for cell in cells if (cell["platform"], cell["backend"], cell["package"])
            == (platform_name, backend, package)]
        assert sum(cell["trials"] for cell in scoped) == 18
        assert sum(cell["cancellation_sequences"] for cell in scoped) == 1
        assert {(cell["context_tier"], cell["fixture"], cell["scenario"])
            for cell in scoped if cell["trials"]} == {
                (tier, fixture, scenario) for tier, fixture in h.MATRIX_WORKLOADS
                for scenario in ("single-needle", "structured-extraction")}


def test_matrix_plan_entry_point_imports_without_os_killpg():
    script = Path(__file__).parents[2] / "scripts" / "long_context_benchmark.py"
    probe = textwrap.dedent(f"""
        import os
        import runpy
        import sys
        if hasattr(os, "killpg"):
            del os.killpg
        sys.argv = [{str(script)!r}, "matrix-plan"]
        try:
            runpy.run_path({str(script)!r}, run_name="__main__")
        except SystemExit as exc:
            if exc.code:
                raise
    """)
    completed = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    plan = json.loads(completed.stdout)
    h.validate_matrix_plan(plan)


def test_runtime_configuration_validation_is_exact_and_fail_closed():
    attestation = {"attestation": {"applicability": "not_applicable_verified_non_qwen",
        "architecture": "llama", "profile_id": "default"}}
    valid = _runtime_configuration()
    assert h.validate_runtime_configuration(valid, backend="cpu", context_tier="64k-full",
        context_tokens=65536, kv_attestation=attestation) == valid
    for mutation in (
            lambda item: item.update(secret="plaintext"),
            lambda item: item["context"].update(effective_window_tokens=True),
            lambda item: item["backend"].update(used="cuda"),
            lambda item: item.update(yarn_rope={"status": "not_applicable", "reason": "arbitrary"})):
        malformed = json.loads(json.dumps(valid)); mutation(malformed)
        with pytest.raises(ValueError, match="runtime_configuration_invalid"):
            h.validate_runtime_configuration(malformed, backend="cpu", context_tier="64k-full",
                context_tokens=65536, kv_attestation=attestation)


def test_qwen_runtime_configuration_requires_complete_valid_yarn_and_profile_evidence():
    attestation = {"type_k": "q8", "type_v": "q8", "attestation": {
        "applicability": "qwen_64k_full", "architecture": "qwen3",
        "profile_id": "qwen64k_kv_q8_fa_balanced_batch"}}
    valid = _runtime_configuration("metal", qwen=True)
    assert h.validate_runtime_configuration(valid, backend="metal", context_tier="64k-full",
        context_tokens=65536, kv_attestation=attestation) == valid
    for field, value in (("configuration_valid", False), ("rope_frequency_scale", float("nan")),
            ("requested_context_tokens", 65535)):
        malformed = json.loads(json.dumps(valid)); malformed["yarn_rope"][field] = value
        with pytest.raises(ValueError, match="runtime_configuration_invalid"):
            h.validate_runtime_configuration(malformed, backend="metal", context_tier="64k-full",
                context_tokens=65536, kv_attestation=attestation)


def test_runtime_configuration_binds_modes_applicability_and_p7_precision():
    summary = _qwen_kv_summary()
    valid = _runtime_configuration("metal", qwen=True)
    mutations = (
        ("mode", {"requested":"gpu", "effective":"gpu"}),
        ("runtime_profile", {**valid["runtime_profile"], "selected":"qwen64k_kv_q4_fa"}),
        ("kv_cache", {**valid["kv_cache"], "precision":"q4"}),
        ("kv_cache", {**valid["kv_cache"], "type_k":2}),
        ("context", {"tier":"64k-full", "effective_window_tokens":65535}),
        ("yarn_rope", {**valid["yarn_rope"], "rope_frequency_scale":1.0}),
    )
    for section, replacement in mutations:
        malformed = json.loads(json.dumps(valid)); malformed[section] = replacement
        with pytest.raises(ValueError, match="runtime_configuration_invalid"):
            h.validate_runtime_configuration(malformed, backend="metal", context_tier="64k-full",
                context_tokens=65536, kv_attestation=summary)
    for architecture, tier, window, reason in (
            ("llama", "64k-full", 65536, "not_applicable_verified_non_qwen"),
            ("qwen3", "8k-fast", 8192, "not_applicable_context_tier")):
        attestation = {"method":"active_runtime_selected_profile", "applicability":reason,
            "architecture":architecture, "profile_id":"default", "backend":"cpu",
            "context_tier":tier, "context_size_tokens":window}
        summary_na = {"pass":True, "applicability":reason, "reason":reason,
            "attestation":attestation}
        exact = _runtime_configuration("cpu", tier=tier, window=window)
        assert h.validate_runtime_configuration(exact, backend="cpu", context_tier=tier,
            context_tokens=window, kv_attestation=summary_na) == exact
        fabricated = json.loads(json.dumps(exact))
        fabricated["runtime_profile"] = valid["runtime_profile"]
        with pytest.raises(ValueError, match="runtime_configuration_invalid"):
            h.validate_runtime_configuration(fabricated, backend="cpu", context_tier=tier,
                context_tokens=window, kv_attestation=summary_na)


def test_fixture_generation_stable_hash_and_depths():
    p1, m1 = h.generate_fixture("small-8k")
    p2, m2 = h.generate_fixture("small-8k")
    assert p1 == p2
    assert m1 == m2
    assert m1["fixture_sha256"] == h.hashlib.sha256(p1.encode()).hexdigest()
    assert m1["scenario"] == "structured-extraction"
    assert set(m1["target_depths_tokens"]) == {"VII", "XIV", "XXI", "canary"}
    h.validate_manifest(m1, p1)
    assert "The Winged Monkeys" in p1 and "Table of Contents" in p1


def test_fixture_generation_sizes_with_ci_tokenizer():
    for fixture in ("small-8k", "intermediate-32k", "long-55k"):
        _, manifest = h.generate_fixture(fixture)
        assert manifest["requested_tokens"] <= manifest["actual_tokens"] + 300
        assert manifest["actual_tokens"] > manifest["requested_tokens"] - 500


@pytest.mark.parametrize("scenario", ["single-needle", "structured-extraction"])
def test_small_fixture_fits_8k_fast_effective_prompt_budget(scenario):
    first_prompt, first = h.generate_fixture("small-8k", scenario=scenario)
    second_prompt, second = h.generate_fixture("small-8k", scenario=scenario)
    profile = h.get_context_profile("8k-fast")
    prompt_budget = profile.total_context_tokens - profile.default_output_reservation_tokens

    assert profile.total_context_tokens == 8192
    assert profile.default_output_reservation_tokens == 1024
    assert first["requested_tokens"] == prompt_budget == 7168
    assert 0.90 * prompt_budget <= first["actual_tokens"] <= prompt_budget
    assert (first_prompt, first["fixture_sha256"]) == (second_prompt, second["fixture_sha256"])


@pytest.mark.parametrize(("fixture", "depth"), [
    ("small-8k", 0.18), ("intermediate-32k", 0.50), ("long-55k", 0.82),
])
def test_single_needle_and_hidden_canary_have_controlled_depth(fixture, depth):
    prompt, manifest = h.generate_fixture(fixture, scenario="single-needle")
    needle = prompt.split("NEEDLE FACT: ", 1)[1].splitlines()[0]
    assert prompt.count(needle) == 1
    assert manifest["expected_answers"] == {"needle": needle}
    assert manifest["targets"]["needle"]["actual_ratio"] == pytest.approx(depth, abs=0.015)


def test_structured_canary_is_hidden_and_single_occurrence():
    prompt, manifest = h.generate_fixture("small-8k", scenario="structured-extraction")
    canary = manifest["expected_answers"]["canary"]
    assert prompt.count(canary) == 1
    assert canary not in prompt.split("Table of Contents", 1)[0]


def test_fixture_target_prefixes_use_structural_value_anchors():
    prompt, manifest = h.generate_fixture("small-8k", scenario="structured-extraction")
    prompt_bytes = prompt.encode("utf-8")

    for key in ("VII", "XIV", "XXI"):
        cut = manifest["targets"][key]["target_prefix_utf8_bytes"]
        prefix = prompt_bytes[:cut].decode("utf-8")
        suffix = prompt_bytes[cut:].decode("utf-8")
        assert prefix.endswith(f"Chapter {key}: {h.STRUCTURED_HEADINGS[key]}\n")
        assert suffix.startswith(manifest["expected_answers"][key])
    vii_cut = manifest["targets"]["VII"]["target_prefix_utf8_bytes"]
    assert prompt_bytes[:vii_cut].decode("utf-8").endswith(
        "VII. They were obliged to camp out\n"
    )

    canary_cut = manifest["targets"]["canary"]["target_prefix_utf8_bytes"]
    assert prompt_bytes[:canary_cut].decode("utf-8").endswith("RECORD CANARY: ")


def test_single_needle_prefix_uses_record_anchor():
    prompt, manifest = h.generate_fixture("small-8k", scenario="single-needle")
    cut = manifest["targets"]["needle"]["target_prefix_utf8_bytes"]
    assert prompt.encode("utf-8")[:cut].decode("utf-8").endswith("NEEDLE FACT: ")


def test_manifest_rejects_structured_heading_decoy_cut():
    prompt, manifest = h.generate_fixture("small-8k", scenario="structured-extraction")
    candidate = json.loads(json.dumps(manifest))
    heading_prefix = "Chapter VII: VII. "
    heading_cut = prompt.index(heading_prefix) + len(heading_prefix)
    candidate["targets"]["VII"]["target_prefix_utf8_bytes"] = heading_cut
    candidate["target_prefix_utf8_bytes"]["VII"] = heading_cut
    with pytest.raises(ValueError, match="manifest_target_prefix_invalid"):
        h.validate_manifest(candidate, prompt)


def test_fixture_seed_changes_bytes_but_remains_deterministic():
    first = h.generate_fixture("small-8k", "one")
    second = h.generate_fixture("small-8k", "two")
    assert first[0] != second[0]
    assert first[1]["fixture_sha256"] != second[1]["fixture_sha256"]


def test_manifest_validation_rejects_tampering():
    prompt, manifest = h.generate_fixture("small-8k")
    for mutate, code in [
        (lambda value: value.update(fixture_version="old"), "manifest_identity_invalid"),
        (lambda value: value.update(fixture_sha256="0" * 64), "fixture_hash_mismatch"),
        (lambda value: value["expected_answers"].pop("VII"), "manifest_oracle_invalid"),
        (lambda value: value["token_count_provenance"].update(authoritative=True), "manifest_token_provenance_invalid"),
        (lambda value: value["targets"].pop("VII"), "manifest_targets_invalid"),
    ]:
        candidate = json.loads(json.dumps(manifest))
        mutate(candidate)
        with pytest.raises(ValueError, match=code):
            h.validate_manifest(candidate, prompt)


def test_supplied_tokenizer_hook_used_without_claiming_authority():
    _, manifest = h.generate_fixture("small-8k", tokenizer=lambda text: len(text.split()) + 7)
    assert manifest["tokenizer"] == "supplied-callback"
    assert manifest["token_count_provenance"]["authoritative"] is False
    assert 0.90 * 7168 <= manifest["actual_tokens"] <= 7168


def test_semantic_exact_success():
    _, manifest = h.generate_fixture("small-8k")
    response = json.dumps(manifest["expected_answers"])
    score = h.evaluate_semantic(response, manifest)
    assert score["semantic_pass"] is True
    assert score["exact_match"] is True


@pytest.mark.parametrize(("payload", "failed"), [
    ({"needle": "wrong"}, "needle_exact"),
    ({}, "exact_key_set"),
    ({"needle": "wrong", "extra": "value"}, "exact_key_set"),
])
def test_single_needle_oracle_scores_retrieval(payload, failed):
    _, manifest = h.generate_fixture("small-8k", scenario="single-needle")
    score = h.evaluate_semantic(json.dumps(payload), manifest)
    assert score[failed] is False
    assert score["semantic_pass"] is False
    assert failed in score["errors"]


def test_single_needle_oracle_is_deterministic_across_trials():
    _, manifest = h.generate_fixture("small-8k", scenario="single-needle")
    response = json.dumps(manifest["expected_answers"])
    assert h.score_trials([response, response, response], manifest)["exact_match_count"] == 3


def test_semantic_known_p7_failures_detected():
    _, manifest = h.generate_fixture("small-8k")
    response = json.dumps({"VII":"They were obliged to camp out","XIV":"The Winged Monkeys","XXI":"The Lion Becomes the King","canary":"lunar-maple-508163"})
    score = h.evaluate_semantic(response, manifest)
    assert score["json_only"] is True
    assert score["exact_key_set"] is True
    assert score["canary_exact"] is True
    assert score["word_count"] is False
    assert score["prose_not_heading"] is False
    assert score["semantic_pass"] is False


@pytest.mark.parametrize(
    ("key", "heading"),
    [
        ("XIV", "The Winged Monkeys"),
        ("XXI", "The Lion Becomes the King"),
        ("XIV", "the winged monkeys"),
        ("XXI", "the lion becomes the king"),
        ("XIV", "The Winged Monkeys."),
        ("XXI", "The Lion Becomes the King!"),
        ("XIV", "The   Winged  Monkeys"),
        ("XXI", "The  Lion   Becomes the  King"),
    ],
)
def test_semantic_heading_variants_are_not_prose(key, heading):
    _, manifest = h.generate_fixture("small-8k")
    payload = {**manifest["expected_answers"], key: heading}
    score = h.evaluate_semantic(json.dumps(payload), manifest)
    assert score["prose_not_heading"] is False
    assert "prose_not_heading" in score["errors"]


def test_semantic_arbitrary_wrong_prose_is_not_a_heading():
    _, manifest = h.generate_fixture("small-8k")
    payload = {**manifest["expected_answers"], "VII": "These words are quite wrong"}
    score = h.evaluate_semantic(json.dumps(payload), manifest)
    assert score["prose_not_heading"] is True
    assert score["target_selection"] is False


@pytest.mark.parametrize("payload", [[], 7, None])
def test_semantic_valid_non_object_json_has_complete_closed_score(payload):
    _, manifest = h.generate_fixture("small-8k")
    score = h.evaluate_semantic(json.dumps(payload), manifest)
    assert score["json_only"] is True
    assert all(score[key] is False for key in manifest["scoring_rules"] if key != "json_only")


@pytest.mark.parametrize("response", ["not json", "```json\n{}\n```", '{"VII": "x"} commentary'])
def test_semantic_rejects_invalid_json_fences_and_commentary(response):
    _, manifest = h.generate_fixture("small-8k")
    score = h.evaluate_semantic(response, manifest)
    assert score["json_only"] is False
    assert score["semantic_pass"] is False


@pytest.mark.parametrize("bad_value", [None, 3, [], {}])
def test_semantic_missing_and_non_string_values_fail_closed(bad_value):
    _, manifest = h.generate_fixture("small-8k")
    payload = dict(manifest["expected_answers"])
    payload["VII"] = bad_value
    score = h.evaluate_semantic(json.dumps(payload), manifest)
    for key in ("target_selection", "prose_not_heading", "word_count", "capitalization", "trailing_punctuation", "exact_match", "semantic_pass"):
        assert score[key] is False
    del payload["VII"]
    assert h.evaluate_semantic(json.dumps(payload), manifest)["word_count"] is False


def test_semantic_categories_remain_independent():
    _, manifest = h.generate_fixture("small-8k")
    expected = manifest["expected_answers"]

    internal_case = {**expected, "XIV": "You will Remember there was"}
    score = h.evaluate_semantic(json.dumps(internal_case), manifest)
    assert score["target_selection"] is True
    assert score["capitalization"] is False

    punctuated = {**expected, "XXI": expected["XXI"] + "."}
    score = h.evaluate_semantic(json.dumps(punctuated), manifest)
    assert score["target_selection"] is True
    assert score["trailing_punctuation"] is False

    spaced = {**expected, "VII": "They  were obliged to camp"}
    score = h.evaluate_semantic(json.dumps(spaced), manifest)
    assert score["target_selection"] is True
    assert score["word_count"] is True
    assert score["exact_match"] is False

    wrong_five_words = {**expected, "VII": "These words are quite wrong"}
    score = h.evaluate_semantic(json.dumps(wrong_five_words), manifest)
    assert score["word_count"] is True
    assert score["target_selection"] is False


def test_semantic_wrong_missing_canary_and_key_sets():
    _, manifest = h.generate_fixture("small-8k")
    expected = manifest["expected_answers"]
    assert h.evaluate_semantic(json.dumps({**expected, "canary": "wrong"}), manifest)["canary_exact"] is False
    missing = dict(expected); del missing["canary"]
    score = h.evaluate_semantic(json.dumps(missing), manifest)
    assert score["canary_exact"] is False and score["exact_key_set"] is False
    assert h.evaluate_semantic(json.dumps({**expected, "extra": "x"}), manifest)["exact_key_set"] is False


def test_semantic_score_shape_is_stable_boolean_and_errors_deduplicated():
    _, manifest = h.generate_fixture("small-8k")
    fields = set(manifest["scoring_rules"]) | {"semantic_pass"}
    for response in (json.dumps(manifest["expected_answers"]), "null", "bad"):
        score = h.evaluate_semantic(response, manifest)
        assert fields <= score.keys()
        assert all(type(score[key]) is bool for key in fields)
        assert len(score["errors"]) == len(set(score["errors"]))


def test_semantic_json_key_canary_format_failures():
    _, manifest = h.generate_fixture("small-8k")
    assert h.evaluate_semantic("```json\n{}\n```", manifest)["json_only"] is False
    extra = dict(manifest["expected_answers"], extra="x")
    assert h.evaluate_semantic(json.dumps(extra), manifest)["exact_key_set"] is False
    wrong = dict(manifest["expected_answers"], canary="wrong")
    assert h.evaluate_semantic(json.dumps(wrong), manifest)["canary_exact"] is False
    punct = dict(manifest["expected_answers"], VII="They were obliged to camp.")
    assert h.evaluate_semantic(json.dumps(punct), manifest)["trailing_punctuation"] is False
    cap = dict(manifest["expected_answers"], XIV="you will remember there was")
    assert h.evaluate_semantic(json.dumps(cap), manifest)["capitalization"] is False


def test_repeated_trial_scoring():
    _, manifest = h.generate_fixture("small-8k")
    scores = h.score_trials([json.dumps(manifest["expected_answers"]), "not json"], manifest)
    assert scores["trial_count"] == 2
    assert scores["exact_match_count"] == 1
    assert scores["pass_rate"] == 0.5
    assert scores["failure_categories"]["json_only"] == 1


def test_progress_invariants_success_and_failures():
    ok = [
        {"sequence":1,"phase":"preparing","total_prompt_tokens":10,"cached_prompt_tokens":0,"processed_prompt_tokens":0,"generated_tokens":0,"elapsed_ms":0},
        {"sequence":2,"phase":"prefill","total_prompt_tokens":10,"cached_prompt_tokens":2,"processed_prompt_tokens":5,"generated_tokens":0,"elapsed_ms":1},
        {"sequence":3,"phase":"generating","total_prompt_tokens":10,"cached_prompt_tokens":2,"processed_prompt_tokens":10,"generated_tokens":1,"elapsed_ms":2},
    ]
    lifecycle = ok + [{"kind":"result","status":"success","sequence":4,"elapsed_ms":3},
        {"kind":"terminal","state":"completed","sequence":5,"elapsed_ms":4}]
    assert h.analyze_progress(lifecycle)["pass"] is True
    bad = ok + [{"sequence":3,"phase":"prefill","total_prompt_tokens":11,"cached_prompt_tokens":13,"processed_prompt_tokens":12,"generated_tokens":0,"elapsed_ms":1}]
    result = h.analyze_progress(bad)
    assert result["pass"] is False
    assert "decreasing_sequence" in result["errors"]
    assert "cached_exceeds_processed" in result["errors"]
    assert "decreasing_elapsed" in result["errors"]
    assert "changing_prompt_total" in result["errors"]


def test_phase_timing_throughput():
    m = h.summarize_metrics(start_s=0, preparing_end_s=1, prefill_end_s=3,
        first_token_s=3, inference_duration_s=6, request_duration_s=6, prompt_tokens=100, output_tokens=6, request_budget_s=10)
    assert m["prompt_tokens_per_s"] == 50
    assert "decode_tokens_per_s" not in m


def test_kv_compare_boundaries_and_fallback():
    est = {"profile_id":"qwen64k_kv_q8_fa_balanced_batch", "backend":"metal", "context_size_tokens":65536,
        "type_k":"q8", "type_v":"q8", "exact_kv_allocation_bytes":10000,
        "metadata_source":"gguf_header", "conservative_fallback_used":False}
    runtime = {"method":"pinned_llama_cpp_kv_buffer_diagnostic", "llama_cpp_python_version":"0.3.32",
        "llama_cpp_commit":"b3fed31b99f9bd37725833674252bccb429bb183", "observed_bytes":11000,
        "precision_bytes":5243, "record_count":1, "unit":"MiB", "decimal_places":2}
    assert h.compare_kv_estimate(est, runtime, backend="metal", context_tokens=65536)["pass"] is True
    runtime["observed_bytes"] = 16000
    assert h.compare_kv_estimate(est, runtime)["pass"] is False
    est["conservative_fallback_used"] = True
    assert h.compare_kv_estimate(est, runtime)["code"] == "kv_diagnostic_provenance_mismatch"


@pytest.mark.parametrize(("field", "value"), [
    ("profile_id", None), ("type_k", None), ("type_v", "unknown"),
    ("exact_kv_allocation_bytes", True), ("exact_kv_allocation_bytes", -1),
    ("exact_kv_allocation_bytes", 1 << 64),
])
def test_kv_compare_rejects_malformed_estimator_fields(field, value):
    estimate = {"profile_id":"qwen64k_kv_q8_fa_balanced_batch", "backend":"metal",
        "context_size_tokens":65536, "type_k":"q8", "type_v":"q8",
        "exact_kv_allocation_bytes":104857600, "metadata_source":"gguf_header",
        "conservative_fallback_used":False}
    runtime = {"method":"pinned_llama_cpp_kv_buffer_diagnostic",
        "llama_cpp_python_version":"0.3.32",
        "llama_cpp_commit":"b3fed31b99f9bd37725833674252bccb429bb183",
        "observed_bytes":104857600, "precision_bytes":5243, "record_count":1,
        "unit":"MiB", "decimal_places":2}
    estimate[field] = value
    assert h.compare_kv_estimate(estimate, runtime)["pass"] is False


@pytest.mark.parametrize(("field", "value"), [
    ("decimal_places", 10**9), ("record_count", 10**9),
])
def test_kv_compare_bounds_diagnostic_dimensions_before_arithmetic(field, value, monkeypatch):
    estimate = {"profile_id":"qwen64k_kv_q8_fa_balanced_batch", "backend":"metal",
        "context_size_tokens":65536, "type_k":"q8", "type_v":"q8",
        "exact_kv_allocation_bytes":104857600, "metadata_source":"gguf_header",
        "conservative_fallback_used":False}
    runtime = {"method":"pinned_llama_cpp_kv_buffer_diagnostic",
        "llama_cpp_python_version":"0.3.32",
        "llama_cpp_commit":"b3fed31b99f9bd37725833674252bccb429bb183",
        "observed_bytes":104857600, "precision_bytes":5243, "record_count":1,
        "unit":"MiB", "decimal_places":2}
    runtime[field] = value
    monkeypatch.setattr(h.math, "ceil", lambda _value: pytest.fail("arithmetic ran before bounds validation"))
    assert h.compare_kv_estimate(estimate, runtime)["pass"] is False


def test_kv_precision_arithmetic_and_report_shape_fail_closed():
    attestation = {"method":"active_runtime_selected_profile", "applicability":"qwen_64k_full",
        "architecture":"qwen3", "profile_id":"qwen64k_kv_q8_fa_balanced_batch",
        "backend":"metal", "context_tier":"64k-full", "context_size_tokens":65536}
    summary = {"pass":True, "applicability":"qwen_64k_full",
        "profile_id":"qwen64k_kv_q8_fa_balanced_batch", "backend":"metal",
        "context_size_tokens":65536, "type_k":"q8", "type_v":"q8",
        "estimated_bytes":104857600, "observed_bytes":104857600, "delta_bytes":0,
        "precision_interval_bytes":[104852357, 104862843], "precision_bytes":5243,
        "record_count":1, "decimal_places":2,
        "estimator_provenance":"qwen_selected_profile_gguf_header",
        "runtime_provenance":"pinned_llama_cpp_kv_buffer_diagnostic", "attestation":attestation}
    assert h.validate_kv_comparison_summary(summary)["pass"] is True
    for mutation in ({"precision_bytes":5242}, {"record_count":2},
            {"delta_bytes":1}, {"profile_id":None}, {"extra":True}):
        malformed = {**summary, **mutation}
        with pytest.raises(ValueError, match="report_kv_diagnostics_invalid"):
            h.validate_kv_comparison_summary(malformed)


def test_kv_applicability_is_profile_attested_not_filename_derived():
    qwen = {"method":"active_runtime_selected_profile", "applicability":"qwen_64k_full",
        "architecture":"qwen3", "profile_id":"qwen64k_kv_q8_fa_balanced_batch",
        "backend":"metal", "context_tier":"64k-full", "context_size_tokens":65536}
    assert h.validate_kv_applicability(qwen, backend="metal", context_tier="64k-full") == qwen
    non_qwen = {**qwen, "architecture":"llama", "profile_id":"default",
        "applicability":"not_applicable_verified_non_qwen"}
    assert h.validate_kv_applicability(non_qwen, backend="metal", context_tier="64k-full") == non_qwen
    with pytest.raises(ValueError, match="kv_applicability"):
        h.validate_kv_applicability(None, backend="metal", context_tier="64k-full")
    for context_size in (65535, 32768):
        with pytest.raises(ValueError, match="kv_applicability_context_mismatch"):
            h.validate_kv_applicability({**qwen, "context_size_tokens":context_size,
                "applicability":"not_applicable_context_tier"},
                backend="metal", context_tier="64k-full")
    non_64k = {**qwen, "context_tier":"8k-fast", "context_size_tokens":8192,
        "applicability":"not_applicable_context_tier"}
    assert h.validate_kv_applicability(non_64k, backend="metal", context_tier="8k-fast") == non_64k


def test_kv_report_summary_binds_profile_backend_context_and_attestation():
    qwen_attestation = {"method":"active_runtime_selected_profile", "applicability":"qwen_64k_full",
        "architecture":"qwen3", "profile_id":"qwen64k_kv_q8_fa_balanced_batch",
        "backend":"metal", "context_tier":"64k-full", "context_size_tokens":65536}
    summary = {"pass":True, "applicability":"qwen_64k_full",
        "profile_id":"qwen64k_kv_q8_fa_balanced_batch", "backend":"metal",
        "context_size_tokens":65536, "type_k":"q8", "type_v":"q8",
        "estimated_bytes":104857600, "observed_bytes":104857600, "delta_bytes":0,
        "precision_interval_bytes":[104852357, 104862843], "precision_bytes":5243,
        "record_count":1, "decimal_places":2,
        "estimator_provenance":"qwen_selected_profile_gguf_header",
        "runtime_provenance":"pinned_llama_cpp_kv_buffer_diagnostic", "attestation":qwen_attestation}
    assert h.validate_kv_comparison_summary(summary, backend="metal",
        context_tier="64k-full", context_tokens=65536)["pass"] is True
    for mutation, kwargs in (({"profile_id":"qwen64k_kv_q4_fa"}, {}),
            ({"backend":"cuda"}, {}), ({"estimated_bytes":1 << 63}, {}),
            ({}, {"context_tokens":8192}), ({}, {"context_tier":"8k-fast"})):
        with pytest.raises(ValueError, match="report_kv_diagnostics_invalid"):
            h.validate_kv_comparison_summary({**summary, **mutation}, backend="metal",
                context_tier=kwargs.get("context_tier", "64k-full"),
                context_tokens=kwargs.get("context_tokens", 65536))
    attestation = {"method":"active_runtime_selected_profile",
        "applicability":"not_applicable_verified_non_qwen", "architecture":"llama",
        "profile_id":"default", "backend":"metal", "context_tier":"64k-full",
        "context_size_tokens":65536}
    non_applicable = {"pass":True, "applicability":"not_applicable_verified_non_qwen",
        "reason":"not_applicable_verified_non_qwen", "attestation":attestation}
    assert h.validate_kv_comparison_summary(non_applicable, backend="metal",
        context_tier="64k-full", context_tokens=65536)["pass"] is True
    with pytest.raises(ValueError, match="report_kv_diagnostics_invalid"):
        h.validate_kv_comparison_summary({**non_applicable, "attestation":{**attestation,
            "architecture":"qwen3"}}, backend="metal", context_tier="64k-full",
            context_tokens=65536)


def test_memory_probe_success_absent_timeout_malformed_and_sanitize(tmp_path):
    good = tmp_path/"good.py"; good.write_text('import json; print(json.dumps({"rss_bytes": 7, "path":"/Users/alice/secret"}))')
    assert h.platform_memory_probe([sys.executable, str(good)])["available"] is True
    missing = h.platform_memory_probe([str(tmp_path/"missing")])
    assert missing["code"] == "probe_absent"
    slow = tmp_path/"slow.py"; slow.write_text('import time; time.sleep(9)')
    assert h.platform_memory_probe([sys.executable, str(slow)], timeout_s=0.1)["code"] == "probe_timeout"
    malformed = tmp_path/"bad.py"; malformed.write_text('print("secret=abc /Users/alice/file")')
    assert "<redacted>" in h.platform_memory_probe([sys.executable, str(malformed)])["stdout_tail"]


def test_atomic_report_schema_and_redaction(tmp_path):
    path = h.write_report_atomic(tmp_path, {"mode":"semantic-evaluation", "status":"passed",
        "fixture":{"id":"small-8k", "version":h.FIXTURE_VERSION,
            "scenario":"single-needle", "sha256":"abc"},
        "semantic":{"semantic_pass":True}, "prompt":"secret"})
    data = json.loads(path.read_text())
    assert data["schema_version"] == h.SCHEMA_VERSION
    assert "prompt" not in data


def test_cli_validation_and_evaluate(tmp_path):
    proc = subprocess.run([sys.executable, "scripts/long_context_benchmark.py", "packaged-runtime", "--out-dir", str(tmp_path)], text=True, capture_output=True)
    assert proc.returncode == 2
    prompt, manifest = h.generate_fixture("small-8k")
    mf = tmp_path/"m.json"; mf.write_text(json.dumps(manifest))
    resp = tmp_path/"r.json"; resp.write_text(json.dumps(manifest["expected_answers"]))
    proc = subprocess.run([sys.executable, "scripts/long_context_benchmark.py", "evaluate", "--manifest", str(mf), "--response", str(resp), "--strict", "--out-dir", str(tmp_path)], text=True, capture_output=True)
    assert proc.returncode == 0


def test_platform_context_behavior():
    assert h.get_context_profile("8k-fast").total_context_tokens == 8192
    assert h.platform.system().lower() in {"linux", "darwin", "windows"}

def _physical_cancellation_evidence(total_prompt_tokens=100):
    def scenario(phase):
        return {"phase": phase, "trigger_observed": True, "trigger_count": 50,
            "threshold": 50, "total_prompt_tokens": total_prompt_tokens,
            "attempted": True, "acknowledged": True, "cleanup_s": 0.2,
            "quiescence_s": 0.5, "stale_progress_count": 0, "late_result_count": 0,
            "active_after_quiescence": False, "followup_ok": True, "followup_s": 1.0}
    return {"scenarios": [scenario("prefill"), scenario("generating")],
        "operator_lifecycle": {"stop_confirmed": True, "restart_ready": True,
            "session_changed": True, "restart_s": 2.0, "post_restart_followup_ok": True,
            "post_restart_followup_s": 1.0}}


def test_physical_cancellation_recovery_evidence_success_and_privacy():
    result = h.validate_cancellation_recovery(_physical_cancellation_evidence(),
        cleanup_budget_s=3, observation_window_s=0.5, recovery_timeout_s=3,
        total_prompt_tokens=100)
    assert result["pass"] is True
    serialized = json.dumps(result).lower()
    assert all(term not in serialized for term in ("request_id", "session_id",
        "response", "ciphertext", "credential", "cancel_token"))


@pytest.mark.parametrize(("mutate", "code"), [
    (lambda v: v["scenarios"][0].update(trigger_observed=False), "cancellation_trigger_missed"),
    (lambda v: v["scenarios"][0].update(acknowledged=False), "cancellation_unconfirmed"),
    (lambda v: v["scenarios"][0].update(late_result_count=1), "cancellation_late_result"),
    (lambda v: v["scenarios"][0].update(stale_progress_count=1), "cancellation_stale_progress"),
    (lambda v: v["scenarios"][0].update(active_after_quiescence=True), "cancellation_stale_progress"),
    (lambda v: v["scenarios"][0].update(cleanup_s=4), "cancellation_cleanup_timeout"),
    (lambda v: v["scenarios"][0].update(followup_ok=False), "cancellation_followup_failed"),
    (lambda v: v["operator_lifecycle"].update(stop_confirmed=False), "operator_stop_failed"),
    (lambda v: v["operator_lifecycle"].update(session_changed=False), "operator_restart_failed"),
    (lambda v: v["operator_lifecycle"].update(restart_ready=False), "operator_restart_failed"),
    (lambda v: v["operator_lifecycle"].update(post_restart_followup_ok=False), "operator_followup_failed"),
    (lambda v: v["operator_lifecycle"].update(restart_s=4), "operator_restart_timeout"),
    (lambda v: v["scenarios"][0].pop("attempted"), "cancellation_evidence_malformed"),
])
def test_physical_cancellation_recovery_evidence_fails_closed(mutate, code):
    value = _physical_cancellation_evidence()
    mutate(value)
    with pytest.raises(ValueError, match=code):
        h.validate_cancellation_recovery(value, cleanup_budget_s=3,
            observation_window_s=0.5, recovery_timeout_s=3, total_prompt_tokens=100)


def test_physical_cancellation_threshold_mismatch_fails_closed():
    with pytest.raises(ValueError, match="cancellation_threshold_mismatched"):
        h.validate_cancellation_recovery(_physical_cancellation_evidence(),
            cleanup_budget_s=3, observation_window_s=0.5, recovery_timeout_s=3,
            total_prompt_tokens=100, prefill_threshold=49, generation_threshold=50)


@pytest.mark.parametrize(("count", "threshold", "total", "state"), [
    (50, 50, 100, "trigger"),
    (100, 50, 100, "completed"),
    (101, 50, 100, "completed"),
    (0, 1, 1, "completed"),
])
def test_prefill_cancellation_requires_interior_progress(count, threshold, total, state):
    assert h.prefill_cancellation_trigger_state(count, threshold, total) == state


@pytest.mark.parametrize(("mutate", "code"), [
    (lambda value: value["scenarios"][0].pop("total_prompt_tokens"), "cancellation_evidence_malformed"),
    (lambda value: value["scenarios"][0].update(total_prompt_tokens="100"), "cancellation_evidence_malformed"),
    (lambda value: value["scenarios"][0].update(total_prompt_tokens=99), "cancellation_prompt_total_mismatched"),
    (lambda value: value["scenarios"][0].update(trigger_count=100), "cancellation_trigger_missed"),
    (lambda value: value["scenarios"][0].update(trigger_count=101), "cancellation_trigger_missed"),
])
def test_cancellation_prompt_total_evidence_fails_closed(mutate, code):
    value = _physical_cancellation_evidence()
    mutate(value)
    with pytest.raises(ValueError, match=code):
        h.validate_cancellation_recovery(value, cleanup_budget_s=3,
            observation_window_s=0.5, recovery_timeout_s=3, total_prompt_tokens=100)


def test_manifest_scoring_rules_match_score_keys():
    _, manifest = h.generate_fixture("small-8k")
    score = h.evaluate_semantic(json.dumps(manifest["expected_answers"]), manifest)
    assert set(manifest["scoring_rules"]).issubset(score.keys())


def test_phase_timing_allows_zero_first_token():
    m = h.summarize_metrics(start_s=0.0, preparing_end_s=0.0, prefill_end_s=0.0,
        first_token_s=0.0, inference_duration_s=5.0, request_duration_s=5.0, prompt_tokens=100, output_tokens=6, request_budget_s=5.0)
    assert m["prefill_duration_s"] == 0.0
    assert m["local_inference_duration_s"] == 5.0
    assert "decode_duration_s" not in m


def _completed_lifecycle():
    return [
        {"sequence":1,"phase":"preparing","total_prompt_tokens":10,"cached_prompt_tokens":0,
         "processed_prompt_tokens":0,"generated_tokens":0,"elapsed_ms":0},
        {"sequence":2,"phase":"prefill","total_prompt_tokens":10,"cached_prompt_tokens":1,
         "processed_prompt_tokens":10,"generated_tokens":0,"elapsed_ms":1},
        {"sequence":3,"phase":"generating","total_prompt_tokens":10,"cached_prompt_tokens":1,
         "processed_prompt_tokens":10,"generated_tokens":1,"elapsed_ms":2},
        {"kind":"result","status":"success","sequence":4,"elapsed_ms":3},
        {"kind":"terminal","state":"completed","sequence":5,"elapsed_ms":4},
    ]


@pytest.mark.parametrize(("mutate", "error"), [
    (lambda items: items.clear(), "progress_missing"),
    (lambda items: items[0].pop("total_prompt_tokens"), "malformed_telemetry"),
    (lambda items: items[1].update(sequence=1), "decreasing_sequence"),
    (lambda items: items[1].update(elapsed_ms=0), "decreasing_elapsed"),
    (lambda items: items[1].update(processed_prompt_tokens=-1), "malformed_telemetry"),
    (lambda items: items[2].update(generated_tokens=-1), "malformed_telemetry"),
    (lambda items: (items[1].update(processed_prompt_tokens=10),
        items[2].update(processed_prompt_tokens=9)), "decreasing_processed"),
    (lambda items: (items[1].update(generated_tokens=2),
        items[2].update(generated_tokens=1)), "decreasing_generated"),
    (lambda items: items[1].update(total_prompt_tokens=11), "changing_prompt_total"),
    (lambda items: items[0].update(total_prompt_tokens=0), "invalid_prompt_total"),
    (lambda items: items[1].update(processed_prompt_tokens=11), "processed_exceeds_total"),
    (lambda items: items[1].update(phase="generating"), "invalid_phase_transition"),
    (lambda items: (items[1].update(processed_prompt_tokens=9),
        items[2].update(processed_prompt_tokens=9)), "incomplete_prefill"),
    (lambda items: items.append({"sequence":6,"phase":"generating","total_prompt_tokens":10,
        "cached_prompt_tokens":1,"processed_prompt_tokens":10,"generated_tokens":2,"elapsed_ms":5}),
        "progress_after_terminal"),
    (lambda items: items.append({"kind":"terminal","state":"failed","sequence":6,"elapsed_ms":5}),
        "duplicate_terminal"),
    (lambda items: items[-1].update(elapsed_ms=2), "decreasing_elapsed"),
])
def test_ordered_progress_lifecycle_failures(mutate, error):
    lifecycle = _completed_lifecycle()
    mutate(lifecycle)
    result = h.analyze_progress(lifecycle)
    assert result["pass"] is False
    assert error in result["errors"]


def test_cancellation_rejects_late_result():
    lifecycle = _completed_lifecycle()[:3] + [
        {"kind":"terminal","state":"cancelled","sequence":4,"elapsed_ms":3},
        {"kind":"result","status":"success","sequence":5,"elapsed_ms":4},
    ]
    result = h.analyze_progress(lifecycle)
    assert {"result_after_terminal", "result_after_cancellation"}.issubset(result["errors"])


def test_completed_generating_only_lifecycle_requires_prefill():
    lifecycle = [
        {"sequence":1,"phase":"generating","total_prompt_tokens":10,
         "cached_prompt_tokens":0,"processed_prompt_tokens":10,"generated_tokens":1,"elapsed_ms":0},
        {"kind":"result","status":"success","sequence":2,"elapsed_ms":1},
        {"kind":"terminal","state":"completed","sequence":3,"elapsed_ms":2},
    ]
    result = h.analyze_progress(lifecycle)
    assert result["pass"] is False
    assert "prefill_phase_missing" in result["errors"]


def test_completed_lifecycle_may_begin_with_prefill():
    lifecycle = _completed_lifecycle()[1:]
    for sequence, observation in enumerate(lifecycle, start=1):
        observation["sequence"] = sequence
    assert h.analyze_progress(lifecycle)["pass"] is True


def test_missing_prefill_cannot_become_zero_duration_passing_metrics():
    lifecycle = _completed_lifecycle()
    lifecycle.pop(1)
    result = h.analyze_progress(lifecycle)
    assert result["pass"] is False
    assert "prefill_phase_missing" in result["errors"]


@pytest.mark.parametrize(("change", "code"), [
    ({"request_duration_s": float("nan")}, "timing_non_finite"),
    ({"prefill_end_s": 3, "first_token_s": 2}, "timing_order_invalid"),
    ({"request_duration_s": 11}, "request_budget_exceeded"),
])
def test_timing_fails_closed(change, code):
    values = dict(start_s=0, preparing_end_s=1, prefill_end_s=2, first_token_s=2,
        inference_duration_s=5, request_duration_s=5, prompt_tokens=100, output_tokens=6, request_budget_s=10)
    assert h.summarize_metrics(**{**values, **change})["code"] == code


def test_timing_reports_every_duration_throughput_budget_and_margin():
    metrics = h.summarize_metrics(start_s=1, preparing_end_s=2, prefill_end_s=4,
        first_token_s=5, inference_duration_s=8, request_duration_s=7, prompt_tokens=100, output_tokens=6, request_budget_s=10)
    assert metrics == {"pass":True, "preparing_duration_s":1, "prefill_duration_s":2,
        "time_to_first_token_s":4, "local_inference_duration_s":8,
        "end_to_end_request_duration_s":7,
        "prompt_tokens":100, "output_tokens":6, "prompt_tokens_per_s":50,
        "request_budget_s":10, "completion_margin_s":3,
        "phase_timing_source":"worker_progress_elapsed_ms",
        "inference_timing_source":"parent_inference_monotonic",
        "request_timing_source":"runner_end_to_end_monotonic",
        "completion_token_source":"validated_response_usage"}


def test_invalid_report_preserves_existing_atomic_destination(tmp_path):
    destination = tmp_path / "long_context_benchmark_report.json"
    destination.write_text("existing")
    with pytest.raises(ValueError, match="report_schema_missing"):
        h.write_report_atomic(tmp_path, {"mode":"packaged-runtime"})
    assert destination.read_text() == "existing"


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_report_schema_rejects_non_finite_values(tmp_path, bad):
    with pytest.raises(ValueError, match="report_non_finite"):
        h.write_report_atomic(tmp_path, {"mode":"semantic-evaluation", "status":"passed",
            "fixture":{"id":"small", "version":h.FIXTURE_VERSION,
                "scenario":"single-needle", "sha256":"abc"},
            "semantic":{"semantic_pass":True, "pass_rate":bad}})


def test_post_terminal_observation_is_clock_bounded():
    now = [0.0]; sleeps = []
    def sleep(value):
        sleeps.append(value); now[0] += value
    observed = h.observe_post_terminal(lambda: "poll", clock=lambda: now[0],
        sleeper=sleep, window_s=0.1, interval_s=0.05)
    assert observed == ["poll", "poll"]
    assert all(0 <= value <= 0.05 for value in sleeps)


def test_memory_probe_parses_before_sanitizing_long_json(tmp_path):
    probe = tmp_path / "long_probe.py"
    probe.write_text('import json; print(json.dumps({"padding":"' + ('x' * 700) + '", "rss_bytes": 9}))')
    result = h.platform_memory_probe([sys.executable, str(probe)])
    assert result["available"] is True
    assert result["payload"]["rss_bytes"] == 9
    assert len(result["payload"]["padding"]) == 512


def test_owned_process_tree_memory_aggregation_handles_descendant_churn():
    class Process:
        def __init__(self, rss=0, children=(), error=None):
            self.rss, self.descendants, self.error = rss, list(children), error
        def children(self, recursive=False):
            assert recursive is True
            return self.descendants
        def memory_info(self):
            if self.error:
                raise self.error
            return type("Memory", (), {"rss": self.rss})()

    gone = Process(error=h.psutil.NoSuchProcess(9))
    denied = Process(error=h.psutil.AccessDenied(10))
    roots = iter([Process(100, [Process(40), gone]), Process(110, [Process(90), denied]),
        Process(80)])
    sampler = h.OwnedProcessTreeMemorySampler(7, lambda _pid: next(roots), system="Linux")
    assert [sampler.sample(), sampler.sample(), sampler.sample()] == [True, True, True]
    assert sampler.summary() == _memory_evidence(baseline=140, peak=200, final=80)


def test_primary_memory_evidence_snapshot_survives_recovery_activity():
    class Process:
        def __init__(self, rss):
            self.rss = rss
        def children(self, recursive=False):
            assert recursive is True
            return []
        def memory_info(self):
            return type("Memory", (), {"rss": self.rss})()

    processes = iter([Process(100), Process(200), Process(900)])
    sampler = h.OwnedProcessTreeMemorySampler(
        7, lambda _pid: next(processes), system="Linux")
    assert sampler.sample() is True
    assert sampler.sample() is True
    primary_memory = sampler.summary()

    # Recovery may use the same process tree, but the primary report is frozen.
    assert sampler.sample() is True
    assert primary_memory == _memory_evidence(
        baseline=100, peak=200, final=200, samples=2)
    assert sampler.summary() == _memory_evidence(
        baseline=100, peak=900, final=900, samples=3)


def test_owned_process_tree_memory_fails_without_valid_sample_or_platform():
    denied = lambda _pid: (_ for _ in ()).throw(h.psutil.AccessDenied(7))
    sampler = h.OwnedProcessTreeMemorySampler(7, denied, system="Linux")
    assert sampler.sample() is False
    with pytest.raises(ValueError, match="memory_sample_unavailable"):
        sampler.summary()
    assert h.normalized_memory_platform("Darwin") == "macos"
    assert h.normalized_memory_platform("Windows") == "windows"
    assert h.normalized_memory_platform("Plan9") == "unsupported"


@pytest.mark.parametrize("mutation", [
    lambda value: value.pop("scope"),
    lambda value: value.update(method="process_name_scan"),
    lambda value: value.update(platform="freebsd"),
    lambda value: value.update(sample_count=0),
    lambda value: value.update(peak_rss_bytes=99),
    lambda value: value.update(final_rss_bytes=-1),
    lambda value: value.update(pid=123),
])
def test_physical_memory_evidence_exact_shape_and_bounds(mutation):
    evidence = _memory_evidence()
    mutation(evidence)
    with pytest.raises(ValueError, match="physical_memory_evidence_invalid"):
        h.validate_physical_memory_evidence(evidence)


def test_report_redacts_authorization_and_message_like_payloads(tmp_path):
    path = h.write_report_atomic(tmp_path, {
        "mode":"semantic-evaluation", "status":"passed",
        "fixture":{"id":"small-8k", "version":h.FIXTURE_VERSION,
            "scenario":"single-needle", "sha256":"abc"},
        "semantic":{"semantic_pass":True},
        "diagnostics": "Authorization: Bearer sk-secret api_key = sk-other",
        "adapter": {
            "messages": [{"content": "plain prompt"}],
            "tool_arguments": {"secret": "plain args"},
            "model_output": "plain output",
            "safe": "Authorization: Bearer sk-nested",
        },
    })
    text = path.read_text()
    data = json.loads(text)
    assert "sk-secret" not in text
    assert "sk-other" not in text
    assert "plain prompt" not in text
    assert "plain args" not in text
    assert "plain output" not in text
    assert "messages" not in data["adapter"]
    assert data["adapter"]["safe"] == "<redacted>"


def test_packaged_runtime_response_usage_and_authoritative_evidence_validation(tmp_path):
    prompt, manifest = h.generate_fixture("small-8k")
    authoritative_total = manifest["actual_tokens"] + 17
    authoritative_offsets = {key: round(value["actual_ratio"] * authoritative_total)
        for key, value in manifest["targets"].items()}
    model = tmp_path / "model.gguf"
    model.write_bytes(b"test artifact")
    payload = {
        "response_text": json.dumps(manifest["expected_answers"]),
        "progress_events": [
            {"schema_version": 1, "sequence": 1, "phase": "preparing", "total_prompt_tokens": authoritative_total, "cached_prompt_tokens": 0, "processed_prompt_tokens": 0, "generated_tokens": 0, "elapsed_ms": 0},
            {"schema_version": 1, "sequence": 2, "phase": "prefill", "total_prompt_tokens": authoritative_total, "cached_prompt_tokens": 0, "processed_prompt_tokens": authoritative_total, "generated_tokens": 0, "elapsed_ms": 1000},
            {"schema_version": 1, "sequence": 3, "phase": "generating", "total_prompt_tokens": authoritative_total, "cached_prompt_tokens": 0, "processed_prompt_tokens": authoritative_total, "generated_tokens": 4, "elapsed_ms": 2000},
        ],
        "post_terminal_observations": [], "atomic_response_completed": True,
        "request_duration_s": 2.2,
        "response_metadata": {"prompt_tokens": authoritative_total,
            "completion_tokens": 4, "finish_reason": "stop"},
        "generation_settings": {"supplied": {"max_tokens": 1024},
            "omitted_runtime_default": ["seed", "temperature", "top_p"]},
        "messages": [{"content": "plaintext"}],
        "memory": _memory_evidence(),
        "runtime_configuration": _runtime_configuration("metal", qwen=True),
        "app_identity": "token.place-test",
        "runtime_identity": "bundled-test",
        "bundled_runtime_identity": "bundled-test",
        "build_identity": "unit-test",
        "backend_requested": "metal", "backend_selected": "metal", "backend_used": "metal",
        "model_fingerprint": "sha256:test",
        "authoritative_prompt_tokens": authoritative_total,
        "authoritative_tokenizer_evidence": {"method": "packaged_admission_render_and_tokenize_chat", "runtime_identity": "bundled-test", "fixture_sha256": manifest["fixture_sha256"], "total_prompt_tokens": authoritative_total, "target_offsets_tokens": authoritative_offsets},
        "kv_applicability": {"method":"active_runtime_selected_profile",
            "applicability":"qwen_64k_full", "architecture":"qwen3",
            "profile_id":"qwen64k_kv_q8_fa_balanced_batch", "backend":"metal", "context_tier":"64k-full",
            "context_size_tokens":65536},
        "kv_estimate":{"profile_id":"qwen64k_kv_q8_fa_balanced_batch", "backend":"metal",
            "context_size_tokens":65536, "type_k":"q8", "type_v":"q8",
            "exact_kv_allocation_bytes":104857600, "metadata_source":"gguf_header",
            "conservative_fallback_used":False},
        "kv_runtime":{"method":"pinned_llama_cpp_kv_buffer_diagnostic",
            "llama_cpp_python_version":"0.3.32",
            "llama_cpp_commit":"b3fed31b99f9bd37725833674252bccb429bb183",
            "observed_bytes":104857600, "precision_bytes":5243, "record_count":1,
            "unit":"MiB", "decimal_places":2},
        "cancellation_recovery": _physical_cancellation_evidence(authoritative_total),
    }
    payload["local_telemetry"] = {"progress_events": [{key: value for key, value in event.items()
            if key != "schema_version"} for event in payload["progress_events"]],
        "inference_complete": [{"active_tier": "64k-full",
            "prompt_tokens": authoritative_total, "output_reservation": 1024,
            "inference_duration_seconds": 2.0}], "ambiguous": False, "malformed": False}


    app = tmp_path / "app"; app.write_text("app"); app.chmod(0o700)
    payload["cancellation_recovery"]["scenarios"][0].update(
        threshold=max(1, int(authoritative_total * 0.5)),
        trigger_count=max(1, int(authoritative_total * 0.5)))
    payload["cancellation_recovery"]["scenarios"][1].update(threshold=8, trigger_count=8)
    seen = {}
    def fake_run(command, **kwargs):
        seen.update(command=command, kwargs=kwargs)
        request_path = command[command.index("--benchmark-request") + 1]
        evidence_path = command[command.index("--benchmark-evidence") + 1]
        seen["request"] = json.loads(h.Path(request_path).read_text())
        h.Path(evidence_path).write_text(json.dumps(payload))
        _write_phase(h.Path(command[command.index("--benchmark-phase-status") + 1]),
            "cleanup", 0.0, cleanup_succeeded=True)
        return subprocess.CompletedProcess(command, 0, "", "")

    result = h.invoke_packaged_runtime_adapter(timeout_s=3.0, app_binary=str(app), model=str(model),
        backend="metal", relay_url="https://relay.example", cleanup_timeout_s=3.0,
        external_prompt=prompt, external_manifest=manifest, subprocess_run=fake_run,
        cancellation_validation=True, prefill_cancel_fraction=0.5,
        generation_cancel_tokens=8, observation_window_s=0.5, recovery_timeout_s=3)
    assert seen["request"]["fixture_id"] == "small-8k"
    assert seen["request"]["prompt"] not in json.dumps(result)
    assert result["runner_kind"] == "repository_packaged_desktop_webdriver"
    assert result["pass"] is True
    assert result["fixture"]["estimated_prompt_tokens"] != result["fixture"]["authoritative_prompt_tokens"]
    assert result["fixture"]["authoritative_target_offsets_tokens"] == authoritative_offsets
    assert seen["request"]["cancellation_validation"] is True
    assert result["cancellation_recovery"]["pass"] is True
    assert result["memory"] == _memory_evidence()
    assert result["metrics"]["output_tokens"] == 4
    assert result["metrics"]["completion_token_source"] == "validated_response_usage"
    assert result["response_usage"] == {**payload["response_metadata"], "source": "validated_atomic_response_usage"}
    assert "messages" not in result
    assert not h.Path(seen["command"][seen["command"].index("--benchmark-request") + 1]).exists()
    assert not h.Path(seen["command"][seen["command"].index("--benchmark-evidence") + 1]).exists()

    def invoke_with_payload_change(key, value):
        original = payload.get(key)
        had_key = key in payload
        if value is missing:
            payload.pop(key, None)
        else:
            payload[key] = value
        try:
            return h.invoke_packaged_runtime_adapter(timeout_s=3.0, app_binary=str(app),
                model=str(model), backend="metal", relay_url="https://relay.example",
                cleanup_timeout_s=3.0, external_prompt=prompt, external_manifest=manifest,
                subprocess_run=fake_run, cancellation_validation=True,
                prefill_cancel_fraction=0.5, generation_cancel_tokens=8,
                observation_window_s=0.5, recovery_timeout_s=3)
        finally:
            if had_key:
                payload[key] = original
            else:
                payload.pop(key, None)

    missing = object()
    for local_telemetry in (None, {}):
        assert invoke_with_payload_change("local_telemetry", local_telemetry)["code"] == \
            "authoritative_local_progress_missing"
    for response_metadata in (missing, None, {}):
        assert invoke_with_payload_change("response_metadata", response_metadata)["code"] == \
            "response_usage_missing_or_inconsistent"
    zero_usage = {**payload["response_metadata"], "completion_tokens": 0}
    assert invoke_with_payload_change("response_metadata", zero_usage)["code"] == \
        "response_usage_missing_or_inconsistent"
    for duration in (float("nan"), float("inf"), True):
        local_telemetry = copy.deepcopy(payload["local_telemetry"])
        local_telemetry["inference_complete"][0]["inference_duration_seconds"] = duration
        assert invoke_with_payload_change("local_telemetry", local_telemetry)["code"] == \
            "local_timing_record_malformed"
    incompatible_settings = copy.deepcopy(payload["generation_settings"])
    incompatible_settings["supplied"]["max_tokens"] = 2048
    assert invoke_with_payload_change("generation_settings", incompatible_settings)["code"] == \
        "local_telemetry_configuration_mismatch"
    assert invoke_with_payload_change("request_duration_s", float("nan"))["code"] == \
        "local_timing_record_malformed"
    assert invoke_with_payload_change("atomic_response_completed", False)["code"] == \
        "encrypted_progress_delivery_invalid"

    payload["response_metadata"]["prompt_tokens"] += 1
    disagreement = h.invoke_packaged_runtime_adapter(timeout_s=3.0, app_binary=str(app),
        model=str(model), backend="metal", relay_url="https://relay.example",
        cleanup_timeout_s=3.0, external_prompt=prompt, external_manifest=manifest,
        subprocess_run=fake_run, cancellation_validation=True, prefill_cancel_fraction=0.5,
        generation_cancel_tokens=8, observation_window_s=0.5, recovery_timeout_s=3)
    assert disagreement["code"] == "response_usage_missing_or_inconsistent"
    payload["response_metadata"]["prompt_tokens"] -= 1

    payload["cancellation_recovery"]["scenarios"][0]["trigger_count"] = authoritative_total
    failed = h.invoke_packaged_runtime_adapter(timeout_s=3.0, app_binary=str(app), model=str(model),
        backend="metal", relay_url="https://relay.example", cleanup_timeout_s=3.0,
        external_prompt=prompt, external_manifest=manifest, subprocess_run=fake_run,
        cancellation_validation=True, prefill_cancel_fraction=0.5,
        generation_cancel_tokens=8, observation_window_s=0.5, recovery_timeout_s=3,
        report_only=True)
    assert failed["code"] == "cancellation_trigger_missed"
    assert failed["runtime_contract_pass"] is False


def _local_telemetry(total=100):
    events = [
        {"sequence": 1, "phase": "preparing", "total_prompt_tokens": 0,
         "cached_prompt_tokens": 0, "processed_prompt_tokens": 0,
         "generated_tokens": 0, "elapsed_ms": 0},
        {"sequence": 2, "phase": "prefill", "total_prompt_tokens": total,
         "cached_prompt_tokens": 0, "processed_prompt_tokens": total,
         "generated_tokens": 0, "elapsed_ms": 900},
        {"sequence": 3, "phase": "generating", "total_prompt_tokens": total,
         "cached_prompt_tokens": 0, "processed_prompt_tokens": total,
         "generated_tokens": 4, "elapsed_ms": 1000},
    ]
    return {"progress_events": events, "inference_complete": [{
        "active_tier": "64k-full", "prompt_tokens": total,
        "output_reservation": 1024, "inference_duration_seconds": 1.2}],
        "ambiguous": False, "malformed": False}


def test_old_app_log_parser_allowlists_windows_records_and_ignores_surrounding_text():
    log = """2026-01-01T00:00:00Z C:\\Users\\alice\\secret.py unrelated prompt text
[INFO] api_v1.local_progress request_id=private-id worker_generation=7 sequence=1 phase=preparing total_prompt_tokens=0 cached_prompt_tokens=0 processed_prompt_tokens=0 generated_tokens=0 elapsed_ms=0
[INFO] api_v1.local_progress request_id=private-id worker_generation=7 sequence=2 phase=prefill total_prompt_tokens=100 cached_prompt_tokens=0 processed_prompt_tokens=100 generated_tokens=0 elapsed_ms=900
[INFO] api_v1.local_progress request_id=private-id worker_generation=7 sequence=3 phase=generating total_prompt_tokens=100 cached_prompt_tokens=0 processed_prompt_tokens=100 generated_tokens=4 elapsed_ms=1000
[INFO] api_v1.inference_complete active_tier=64k-full prompt_tokens=100 output_reservation=1024 admission_result=admitted inference_duration_seconds=1.2 safe_error_code=none"""
    parsed = h.parse_packaged_local_telemetry(log)
    authoritative = h.validate_authoritative_local_telemetry(parsed)
    rendered = json.dumps(authoritative)
    assert authoritative["prompt_tokens"] == 100
    assert "private-id" not in rendered and "worker_generation" not in rendered
    assert "Users" not in rendered and "prompt text" not in rendered


def test_local_telemetry_failure_branches_are_directly_classified():
    malformed_completion = h.parse_packaged_local_telemetry(
        "api_v1.inference_complete active_tier=64k-full incomplete")
    assert malformed_completion["malformed"] is True

    mutations = [
        lambda value: value.update(malformed=True),
        lambda value: value.update(ambiguous=True),
        lambda value: value.update(inference_complete=[]),
        lambda value: value["progress_events"][0].update(extra=True),
        lambda value: value["progress_events"][0].update(sequence=-1),
        lambda value: value["progress_events"][1].update(phase="generating"),
        lambda value: value["progress_events"][1].update(cached_prompt_tokens=101),
    ]
    for mutate in mutations:
        telemetry = _local_telemetry()
        mutate(telemetry)
        with pytest.raises(ValueError, match="local_timing_record_malformed"):
            h.validate_authoritative_local_telemetry(telemetry)

    telemetry = _local_telemetry()
    for event in telemetry["progress_events"]:
        event["phase"] = "generating"
        event["total_prompt_tokens"] = 100
    with pytest.raises(ValueError, match="local_prefill_phase_missing"):
        h.validate_authoritative_local_telemetry(telemetry)

    authoritative = h.validate_authoritative_local_telemetry(_local_telemetry())
    with pytest.raises(ValueError, match="encrypted_progress_delivery_invalid"):
        h.validate_encrypted_progress_delivery([], authoritative)


def test_best_effort_prefill_only_delivery_records_terminal_overtake():
    authoritative = h.validate_authoritative_local_telemetry(_local_telemetry())
    browser_event = {**authoritative["events"][1], "schema_version": 1, "sequence": 1}
    delivered = h.validate_encrypted_progress_delivery(
        [browser_event], authoritative)
    assert delivered == {"pass": True, "best_effort": True, "progress_event_count": 1,
        "observed_phases": ["prefill"],
        "terminal_overtook_generating_update": True}


@pytest.mark.parametrize(("mutate", "reason"), [
    (lambda value: value["progress_events"].pop(), "local_generating_phase_missing"),
    (lambda value: value["progress_events"][-1].update(generated_tokens=0),
        "positive_generated_token_progress_missing"),
    (lambda value: value["progress_events"][1].update(sequence=0), "local_timing_record_malformed"),
    (lambda value: value["progress_events"][1].update(elapsed_ms=1001), "local_timing_record_malformed"),
    (lambda value: value["progress_events"][-1].update(total_prompt_tokens=101),
        "local_timing_record_malformed"),
    (lambda value: [event.update(processed_prompt_tokens=99)
        for event in value["progress_events"][1:]],
        "local_timing_record_malformed"),
])
def test_authoritative_local_telemetry_fails_closed(mutate, reason):
    value = _local_telemetry()
    mutate(value)
    with pytest.raises(ValueError, match=reason):
        h.validate_authoritative_local_telemetry(value)


@pytest.mark.parametrize("phase_index", [0, 1], ids=["preparing", "prefill"])
def test_authoritative_local_telemetry_rejects_generated_tokens_before_generation(phase_index):
    value = _local_telemetry()
    value["progress_events"][phase_index]["generated_tokens"] = 1
    with pytest.raises(ValueError, match="local_timing_record_malformed"):
        h.validate_authoritative_local_telemetry(value)


def test_missing_authoritative_local_progress_has_stable_reason():
    with pytest.raises(ValueError, match="authoritative_local_progress_missing"):
        h.validate_authoritative_local_telemetry({"progress_events": [],
            "inference_complete": [], "ambiguous": True, "malformed": False})


def test_zero_total_preparing_is_only_valid_as_initial_observation():
    value = _local_telemetry()
    value["progress_events"].insert(1, {**value["progress_events"][0], "sequence": 2,
        "elapsed_ms": 1})
    value["progress_events"][2]["sequence"] = 3
    value["progress_events"][3]["sequence"] = 4
    with pytest.raises(ValueError, match="local_timing_record_malformed"):
        h.validate_authoritative_local_telemetry(value)


def test_encrypted_progress_must_match_authoritative_without_replay_or_fabrication():
    authoritative = h.validate_authoritative_local_telemetry(_local_telemetry())
    replay = [
        {**authoritative["events"][0], "schema_version": 1, "sequence": 2},
        {**authoritative["events"][0], "schema_version": 1, "sequence": 2},
    ]
    with pytest.raises(ValueError, match="encrypted_progress_delivery_invalid"):
        h.validate_encrypted_progress_delivery(replay, authoritative)
    changed = [{**authoritative["events"][1], "schema_version": 1,
        "sequence": 1, "processed_prompt_tokens": 99}]
    with pytest.raises(ValueError, match="encrypted_progress_delivery_invalid"):
        h.validate_encrypted_progress_delivery(changed, authoritative)


def test_encrypted_progress_accepts_first_and_later_observation_gaps():
    authoritative = h.validate_authoritative_local_telemetry(_local_telemetry())
    delivered = [
        {**authoritative["events"][0], "schema_version": 1, "sequence": 3},
        {**authoritative["events"][2], "schema_version": 1, "sequence": 8},
    ]
    result = h.validate_encrypted_progress_delivery(delivered, authoritative)
    assert result["pass"] is True
    assert result["observed_phases"] == ["preparing", "generating"]


@pytest.mark.parametrize("sequences", [(2, 2), (3, 1)])
def test_encrypted_progress_rejects_duplicate_or_decreasing_sequences(sequences):
    authoritative = h.validate_authoritative_local_telemetry(_local_telemetry())
    delivered = [
        {**authoritative["events"][0], "schema_version": 1, "sequence": sequences[0]},
        {**authoritative["events"][1], "schema_version": 1, "sequence": sequences[1]},
    ]
    with pytest.raises(ValueError, match="encrypted_progress_delivery_invalid"):
        h.validate_encrypted_progress_delivery(delivered, authoritative)


@pytest.mark.parametrize("schema", [None, 2, True])
def test_encrypted_progress_rejects_invalid_schema_version(schema):
    authoritative = h.validate_authoritative_local_telemetry(_local_telemetry())
    event = {**authoritative["events"][0], "sequence": 1}
    if schema is not None:
        event["schema_version"] = schema
    with pytest.raises(ValueError, match="encrypted_progress_delivery_invalid"):
        h.validate_encrypted_progress_delivery([event], authoritative)


def test_encrypted_progress_rejects_extra_fields():
    authoritative = h.validate_authoritative_local_telemetry(_local_telemetry())
    event = {**authoritative["events"][0], "schema_version": 1,
        "sequence": 1, "unexpected": "field"}
    with pytest.raises(ValueError, match="encrypted_progress_delivery_invalid"):
        h.validate_encrypted_progress_delivery([event], authoritative)


def test_encrypted_progress_matches_ordered_local_projection_after_coalescing():
    authoritative = h.validate_authoritative_local_telemetry(_local_telemetry())
    delivered = [
        {**authoritative["events"][2], "schema_version": 1, "sequence": 1},
    ]
    result = h.validate_encrypted_progress_delivery(delivered, authoritative)
    assert result["pass"] is True
    assert result["observed_phases"] == ["generating"]


def test_encrypted_progress_accepts_initial_zero_preparing_and_equal_elapsed():
    telemetry = _local_telemetry()
    telemetry["progress_events"][1]["elapsed_ms"] = 0
    authoritative = h.validate_authoritative_local_telemetry(telemetry)
    delivered = [
        {**authoritative["events"][0], "schema_version": 1, "sequence": 1},
        {**authoritative["events"][1], "schema_version": 1, "sequence": 2},
    ]
    result = h.validate_encrypted_progress_delivery(delivered, authoritative)
    assert result["observed_phases"] == ["preparing", "prefill"]


def test_local_log_parser_rejects_partial_final_line_and_mixed_identity():
    complete = (
        "api_v1.local_progress request_id=one worker_generation=1 sequence=1 "
        "phase=preparing total_prompt_tokens=0 cached_prompt_tokens=0 "
        "processed_prompt_tokens=0 generated_tokens=0 elapsed_ms=0\n")
    partial = "api_v1.local_progress request_id=one worker_generation=1 sequence=2 phase=prefill"
    assert h.parse_packaged_local_telemetry(complete + partial)["malformed"] is True
    mixed = complete + complete.replace("request_id=one", "request_id=two")
    assert h.parse_packaged_local_telemetry(mixed)["ambiguous"] is True


@pytest.mark.parametrize(("field", "value"), [
    ("active_tier", "8k-fast"),
    ("output_reservation", 2048),
])
def test_authoritative_local_telemetry_binds_requested_configuration(field, value):
    telemetry = _local_telemetry()
    telemetry["inference_complete"][0][field] = value
    with pytest.raises(ValueError, match="local_telemetry_configuration_mismatch"):
        h.validate_authoritative_local_telemetry(telemetry,
            expected_tier="64k-full", expected_output_reservation=1024)


def test_packaged_runtime_external_fixture_pair_and_hash_fail_closed(tmp_path):
    prompt, manifest = h.generate_fixture("small-8k")
    model = tmp_path / "qwen-in-name-but-verified-llama.gguf"; model.write_bytes(b"x")
    app = tmp_path / "app"; app.write_text("x"); app.chmod(0o700)
    common = dict(app_binary=str(app), model=str(model), backend="cpu",
        relay_url="https://relay.example", cleanup_timeout_s=1)
    assert h.invoke_packaged_runtime_adapter(**common, external_prompt=prompt)["code"] == "external_fixture_pair_required"
    assert h.invoke_packaged_runtime_adapter(**common, external_prompt=prompt + "tampered",
        external_manifest=manifest)["code"] == "fixture_hash_mismatch"


@pytest.mark.parametrize(("mutation", "code"), [
    (lambda value: value.update(method="whitespace-ci"), "authoritative_target_depth_malformed"),
    (lambda value: value.update(runtime_identity="other"), "authoritative_target_depth_mismatched"),
    (lambda value: value.update(total_prompt_tokens=99), "authoritative_target_depth_mismatched"),
    (lambda value: value.update(fixture_sha256="0" * 64), "authoritative_target_depth_stale"),
    (lambda value: value.update(target_offsets_tokens={}), "authoritative_target_depth_malformed"),
    (lambda value: value["target_offsets_tokens"].update(XIV=value["target_offsets_tokens"]["VII"]),
     "authoritative_target_depth_ambiguous"),
    (lambda value: value["target_offsets_tokens"].update(
        VII=value["target_offsets_tokens"]["XXI"] - 1),
     "authoritative_target_depth_ordering"),
    (lambda value: value["target_offsets_tokens"].update(VII=1),
     "authoritative_target_depth_ratio"),
])
def test_authoritative_target_depth_evidence_fails_categorically(mutation, code):
    _, manifest = h.generate_fixture("small-8k")
    evidence = {"method": "packaged_admission_render_and_tokenize_chat",
        "runtime_identity": "bundled", "total_prompt_tokens": manifest["actual_tokens"],
        "fixture_sha256": manifest["fixture_sha256"],
        "target_offsets_tokens": {key: value["actual_offset_tokens"]
            for key, value in manifest["targets"].items()}}
    mutation(evidence)
    _, error = h._validate_authoritative_tokenizer_evidence(
        evidence, manifest, "bundled", manifest["actual_tokens"])
    assert error == code


def test_primary_tokenizer_evidence_snapshot_survives_recovery_overwrite(
        desktop_runner, tmp_path):
    evidence_path = tmp_path / "evidence.json"
    primary = {"runtime_identity": "bundled", "fixture_sha256": "fixture",
        "total_prompt_tokens": 8192, "kv_applicability": "primary",
        "kv_estimator": {"estimated_bytes": 10}, "kv_runtime": {"observed_bytes": 11}}
    recovery = {"runtime_identity": "bundled", "fixture_sha256": "fixture",
        "total_prompt_tokens": 1024, "kv_applicability": "recovery",
        "kv_estimator": {"estimated_bytes": 20}, "kv_runtime": {"observed_bytes": 21}}
    evidence_path.write_text(json.dumps(primary), encoding="utf-8")

    snapshot = desktop_runner._read_primary_tokenizer_observation(
        evidence_path, "bundled", "fixture")
    evidence_path.write_text(json.dumps(recovery), encoding="utf-8")

    assert snapshot == primary
    assert snapshot["total_prompt_tokens"] == 8192
    assert snapshot["kv_applicability"] == "primary"
    assert snapshot["kv_estimator"] == {"estimated_bytes": 10}
    assert snapshot["kv_runtime"] == {"observed_bytes": 11}


@pytest.mark.parametrize(("contents", "runtime_identity", "expected"), [
    (None, "bundled", "authoritative_target_depth_unavailable"),
    ("not-json", "bundled", "authoritative_target_depth_unavailable"),
    ('{"runtime_identity":"other","fixture_sha256":"fixture"}', "bundled",
     "authoritative_target_depth_mismatched"),
])
def test_primary_tokenizer_evidence_snapshot_preserves_failure_classifications(
        desktop_runner, tmp_path, contents, runtime_identity, expected):
    evidence_path = tmp_path / "evidence.json"
    if contents is not None:
        evidence_path.write_text(contents, encoding="utf-8")
    with pytest.raises(RuntimeError, match=expected):
        desktop_runner._read_primary_tokenizer_observation(
            evidence_path, runtime_identity, "fixture")


def test_packaged_runtime_requires_physical_prerequisites():
    result = h.invoke_packaged_runtime_adapter(timeout_s=1.5)
    assert result["pass"] is False
    assert result["code"] == "packaged_prerequisites_missing"
    assert set(result["missing"]) == {"app_binary", "model", "backend", "relay_url", "cleanup_timeout_s"}


def test_packaged_runtime_validates_app_model_backend_and_relay(tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"x")
    app = tmp_path / "app"; app.write_text("x"); app.chmod(0o700)
    common = dict(timeout_s=1, app_binary=str(app), model=str(model), backend="metal", relay_url="https://relay.example", cleanup_timeout_s=1)
    assert h.invoke_packaged_runtime_adapter(**{**common, "model": str(tmp_path / "absent.gguf")})["code"] == "model_artifact_invalid"
    assert h.invoke_packaged_runtime_adapter(**{**common, "app_binary": str(tmp_path / "absent")})["code"] == "packaged_app_invalid"
    assert h.invoke_packaged_runtime_adapter(**{**common, "backend": "rocm"})["code"] == "backend_unsupported"
    for url in ("http://relay.example", "ftp://relay.example", "https://user:pw@relay.example", "https://relay.example/#fragment", "https://relay.example:bad"):
        assert h.invoke_packaged_runtime_adapter(**{**common, "relay_url": url})["code"] == "relay_url_invalid"
    assert h._valid_relay_url("http://127.0.0.1:8000")
    assert h._valid_relay_url("https://relay.example")


@pytest.mark.parametrize(
    ("runner_outcome", "expected_code"),
    [
        ("timeout", "packaged_runner_timeout"),
        ("failed", "packaged_runner_failed"),
        ("invalid-json", "packaged_evidence_malformed"),
        ("non-object", "packaged_evidence_malformed"),
        ("missing", "authoritative_target_depth_unavailable"),
    ],
)
def test_packaged_runtime_rejects_runner_and_evidence_failures(tmp_path, runner_outcome, expected_code):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"x")
    app = tmp_path / "app"
    app.write_text("x")
    app.chmod(0o700)

    def fake_run(command, **kwargs):
        if runner_outcome == "timeout":
            phase_path = command[command.index("--benchmark-phase-status") + 1]
            _write_phase(h.Path(phase_path), "request_active", 0.0)
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        if runner_outcome == "failed":
            phase_path = command[command.index("--benchmark-phase-status") + 1]
            _write_phase(h.Path(phase_path), "cleanup", 0.0,
                last_safe_phase="landing_page_ready", failure_reason="send_button_not_enabled",
                cleanup_succeeded=True)
            return subprocess.CompletedProcess(command, 1, "", "")
        evidence_path = command[command.index("--benchmark-evidence") + 1]
        evidence = {"invalid-json": "not json", "non-object": "[]", "missing": "{}"}[runner_outcome]
        h.Path(evidence_path).write_text(evidence)
        _write_phase(h.Path(command[command.index("--benchmark-phase-status") + 1]),
            "cleanup", 0.0, cleanup_succeeded=True)
        return subprocess.CompletedProcess(command, 0, "", "")

    result = h.invoke_packaged_runtime_adapter(
        timeout_s=1,
        app_binary=str(app),
        model=str(model),
        backend="metal",
        relay_url="https://relay.example",
        cleanup_timeout_s=1,
        subprocess_run=fake_run,
    )
    assert result["pass"] is False
    assert result["code"] == expected_code
    if runner_outcome == "timeout":
        assert result["last_safe_phase"] == "request_active"
        assert result["request_timeout_s"] == 1
        assert result["runner_timeout_s"] == (
            h.PACKAGED_SETUP_BUDGET_S + 1 + h.PACKAGED_FINALIZATION_BUDGET_S)
        assert result["overall_timeout_s"] == result["runner_timeout_s"] + 1
        assert result["cleanup_succeeded"] is False


@pytest.mark.parametrize(("phase", "cleanup_value"), [
    ("request_active", None), ("cleanup", None),
])
def test_nonzero_normal_exit_requires_final_cleanup_checkpoint(
        tmp_path, phase, cleanup_value):
    model = tmp_path / "model.gguf"; model.write_bytes(b"x")
    app = tmp_path / "app"; app.write_text("x"); app.chmod(0o700)
    def failed_runner(command, **_kwargs):
        _write_phase(h.Path(command[command.index("--benchmark-phase-status") + 1]),
            phase, 0.0, cleanup_succeeded=cleanup_value)
        return subprocess.CompletedProcess(command, 1)
    result = h.invoke_packaged_runtime_adapter(timeout_s=1, app_binary=str(app),
        model=str(model), backend="metal", relay_url="https://relay.example",
        cleanup_timeout_s=1, subprocess_run=failed_runner)
    assert result["code"] == "packaged_phase_status_malformed"


@pytest.mark.parametrize(("primary_reason", "expected_reason"), [
    (None, "cleanup_failure"),
    ("send_button_not_enabled", "send_button_not_enabled"),
    ("tokenization_failure", "tokenization_failure"),
])
def test_nonzero_cleanup_failure_is_categorical_and_preserves_primary(
        tmp_path, primary_reason, expected_reason):
    model = tmp_path / "model.gguf"; model.write_bytes(b"x")
    app = tmp_path / "app"; app.write_text("x"); app.chmod(0o700)
    def failed_runner(command, **_kwargs):
        phase = h.Path(command[command.index("--benchmark-phase-status") + 1])
        _write_phase(phase, "cleanup", 0.0, failure_reason=primary_reason,
            cleanup_succeeded=False)
        return subprocess.CompletedProcess(command, 1)
    result = h.invoke_packaged_runtime_adapter(timeout_s=1, app_binary=str(app),
        model=str(model), backend="metal", relay_url="https://relay.example",
        cleanup_timeout_s=1, subprocess_run=failed_runner)
    assert result["failure_reason"] == expected_reason
    assert result["cleanup_succeeded"] is False
    if primary_reason == "tokenization_failure":
        assert result["pass"] is False
        assert "semantic" not in result
        assert "performance" not in result


@pytest.mark.parametrize(("contents", "expected"), [
    (None, "packaged_phase_status_missing"),
    ("not-json", "packaged_phase_status_missing"),
    (json.dumps({"schema_version": "wrong", "phase": "request_active",
        "sequence": 6, "elapsed_s": 0}), "packaged_phase_status_malformed"),
    (json.dumps({"schema_version": h.PACKAGED_PHASE_STATUS_VERSION,
        "phase": "request_active", "sequence": 6, "elapsed_s": 50}),
        "packaged_phase_status_malformed"),
    (json.dumps({"schema_version": h.PACKAGED_PHASE_STATUS_VERSION,
        "phase": "request_active", "sequence": 6, "last_safe_phase": "operator_ready",
        "failure_reason": [], "elapsed_s": 0, "cleanup_succeeded": None}),
        "packaged_phase_status_malformed"),
    (json.dumps({"schema_version": h.PACKAGED_PHASE_STATUS_VERSION,
        "phase": "request_active", "sequence": 6, "last_safe_phase": "operator_ready",
        "failure_reason": None, "elapsed_s": 0, "cleanup_succeeded": {}}),
        "packaged_phase_status_malformed"),
])
def test_packaged_phase_status_missing_malformed_or_stale_fails_closed(tmp_path, contents, expected):
    path = tmp_path / "phase.json"
    if contents is not None:
        path.write_text(contents)
    assert h._read_packaged_phase_status(path, 1) == (None, expected)


def test_packaged_adapter_watchdog_is_explicit_and_cli_compatible(tmp_path):
    model = tmp_path / "model.gguf"; model.write_bytes(b"x")
    app = tmp_path / "app"; app.write_text("x"); app.chmod(0o700)
    observed = {}
    def fake_run(command, **kwargs):
        observed["timeout"] = kwargs["timeout"]
        request_path = command[command.index("--benchmark-request") + 1]
        observed["request"] = json.loads(h.Path(request_path).read_text())
        _write_phase(h.Path(command[command.index("--benchmark-phase-status") + 1]),
            "cleanup", 0.0, failure_reason="packaged_runner_failure", cleanup_succeeded=True)
        return subprocess.CompletedProcess(command, 1)
    result = h.invoke_packaged_runtime_adapter(timeout_s=600, app_binary=str(app),
        model=str(model), backend="cuda", relay_url="https://relay.example",
        cleanup_timeout_s=30, subprocess_run=fake_run)
    assert result["code"] == "packaged_runner_failed"
    work_budget = (h.PACKAGED_SETUP_BUDGET_S + 600
        + h.packaged_finalization_budget_s(False))
    assert observed["timeout"] == work_budget + 30
    assert observed["request"]["phase_status_version"] == h.PACKAGED_PHASE_STATUS_VERSION
    assert observed["request"]["phase_status_phases"] == list(h.PACKAGED_PHASES)
    assert observed["request"]["request_timeout_s"] == 600
    assert observed["request"]["setup_timeout_s"] == h.PACKAGED_SETUP_BUDGET_S
    assert observed["request"]["finalization_timeout_s"] == h.PACKAGED_FINALIZATION_BUDGET_S
    assert observed["request"]["cancellation_timeout_s"] == 0


def test_cancellation_budget_is_named_additive_and_bounded(tmp_path):
    model = tmp_path / "model.gguf"; model.write_bytes(b"x")
    app = tmp_path / "app"; app.write_text("x"); app.chmod(0o700)
    observed = {}
    def fake_run(command, **kwargs):
        observed["timeout"] = kwargs["timeout"]
        request_path = command[command.index("--benchmark-request") + 1]
        observed["request"] = json.loads(h.Path(request_path).read_text())
        return subprocess.CompletedProcess(command, 1)
    h.invoke_packaged_runtime_adapter(timeout_s=10, app_binary=str(app), model=str(model),
        backend="cuda", relay_url="https://relay.example", cleanup_timeout_s=3,
        cancellation_validation=True, prefill_cancel_fraction=0.5,
        observation_window_s=2, recovery_timeout_s=4, subprocess_run=fake_run)
    cancellation = h.packaged_cancellation_budget_s(10, 2, 4)
    assert cancellation == 56
    assert observed["request"]["cancellation_timeout_s"] == cancellation
    assert observed["timeout"] == (h.PACKAGED_SETUP_BUDGET_S + 10
        + h.packaged_finalization_budget_s(True) + cancellation + 3)


def test_parent_watchdog_accounts_for_each_finalization_window():
    assert h.packaged_finalization_budget_s(False) == h.PACKAGED_FINALIZATION_BUDGET_S
    assert h.packaged_finalization_budget_s(True) == 2 * h.PACKAGED_FINALIZATION_BUDGET_S


def test_cancellation_budget_enumerates_every_bounded_operation():
    request, observation, recovery = 11, 3, 5
    bounded_operations = ([request] * 2 + [observation] * 2 + [recovery] * 8)
    assert h.packaged_cancellation_budget_s(request, observation, recovery) == sum(bounded_operations)


def test_cancellation_phase_and_finalization_allowances_are_independent():
    now = [10.0]
    def consume_complete_cancellation_allowance():
        now[0] += 56.0
        return "validated"
    result, finalization_deadline = h.start_phase_after(
        consume_complete_cancellation_allowance, 120.0, clock=lambda: now[0])
    assert result == "validated"
    assert finalization_deadline == 186.0
    assert h.packaged_phase_remaining(finalization_deadline, "timeout",
        clock=lambda: now[0]) == 120.0


def test_cancellation_deadline_exhaustion_fails_closed_with_fake_clock():
    with pytest.raises(RuntimeError, match="packaged cancellation validation timeout"):
        h.packaged_phase_remaining(1.0, "packaged cancellation validation timeout",
            clock=lambda: 1.0)


def test_disabled_cancellation_has_no_budget_or_cli_contract_change(tmp_path):
    model = tmp_path / "model.gguf"; model.write_bytes(b"x")
    app = tmp_path / "app"; app.write_text("x"); app.chmod(0o700)
    observed = {}
    def fake_run(command, **kwargs):
        request_path = command[command.index("--benchmark-request") + 1]
        observed.update(json.loads(h.Path(request_path).read_text()))
        return subprocess.CompletedProcess(command, 1)
    h.invoke_packaged_runtime_adapter(timeout_s=10, app_binary=str(app), model=str(model),
        backend="cuda", relay_url="https://relay.example", cleanup_timeout_s=3,
        subprocess_run=fake_run)
    assert observed["cancellation_timeout_s"] == 0
    assert observed["cancellation_validation"] is False


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), "1"])
def test_packaged_runtime_rejects_invalid_timeouts(tmp_path, timeout):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"x")
    app = tmp_path / "app"
    app.write_text("x")
    app.chmod(0o700)
    result = h.invoke_packaged_runtime_adapter(
        timeout_s=timeout,
        app_binary=str(app),
        model=str(model),
        backend="metal",
        relay_url="https://relay.example",
        cleanup_timeout_s=1,
    )
    assert result == {"pass": False, "code": "timeout_invalid"}


def test_report_only_does_not_suppress_runtime_failure(tmp_path):
    proc = subprocess.run([
        sys.executable, "scripts/long_context_benchmark.py", "packaged-runtime",
        "--out-dir", str(tmp_path), "--app-binary", str(tmp_path / "missing-app"),
        "--model", str(tmp_path / "missing.gguf"), "--backend", "metal",
        "--relay-url", "http://127.0.0.1:8000", "--cleanup-timeout", "1",
        "--report-only",
    ], text=True, capture_output=True)
    assert proc.returncode == 1


@pytest.mark.parametrize("scenario", ["single-needle", "structured-extraction"])
def test_small_fixture_passes_8k_fast_context_preflight(tmp_path, scenario):
    model = tmp_path / "model.gguf"; model.write_bytes(b"x")
    app = tmp_path / "app"; app.write_text("x"); app.chmod(0o700)
    launched = []
    def fake_run(command, **kwargs):
        launched.append(command)
        _write_phase(h.Path(command[command.index("--benchmark-phase-status") + 1]),
            "cleanup", 0.0, failure_reason="packaged_runner_failure", cleanup_succeeded=True)
        return subprocess.CompletedProcess(command, 1, "runner stopped", "")
    result = h.invoke_packaged_runtime_adapter(app_binary=str(app), model=str(model),
        backend="cpu", relay_url="https://relay.example", cleanup_timeout_s=1,
        context_tier="8k-fast", scenario=scenario, subprocess_run=fake_run)
    assert launched
    assert result["code"] == "packaged_runner_failed"


@pytest.mark.parametrize(("report_only", "semantic_ok", "accepted"), [
    (False, False, False), (True, False, True), (True, True, False),
])
def test_report_only_only_accepts_semantic_failure(tmp_path, report_only, semantic_ok, accepted):
    _, manifest = h.generate_fixture("small-8k")
    model = tmp_path / "model.gguf"; model.write_bytes(b"x")
    app = tmp_path / "app"; app.write_text("x"); app.chmod(0o700)
    response = manifest["expected_answers"] if semantic_ok else {**manifest["expected_answers"], "canary": "wrong"}
    payload = {
        "response_text": json.dumps(response),
        "generation_settings":{"supplied":{"max_tokens":1024},
            "omitted_runtime_default":["seed", "temperature", "top_p"]},
        "memory": _memory_evidence(),
        "runtime_configuration": _runtime_configuration(),
        "request_duration_s": 2.2, "atomic_response_completed": True,
        "response_metadata": {"prompt_tokens": manifest["actual_tokens"],
            "completion_tokens": 4, "finish_reason": "stop"},
        "post_terminal_observations":[],
        "app_identity": "token.place", "runtime_identity": "bundled",
        "bundled_runtime_identity": "bundled", "build_identity": "build",
        "backend_requested": "cpu", "backend_selected": "cpu", "backend_used": "cpu", "model_fingerprint": "sha256:test",
        "authoritative_prompt_tokens": manifest["actual_tokens"],
        "authoritative_tokenizer_evidence": {"method": "packaged_admission_render_and_tokenize_chat", "runtime_identity": "bundled", "fixture_sha256": manifest["fixture_sha256"], "total_prompt_tokens": manifest["actual_tokens"], "target_offsets_tokens": {key: value["actual_offset_tokens"] for key, value in manifest["targets"].items()}},
        "kv_applicability": {"method":"active_runtime_selected_profile",
            "applicability":"not_applicable_verified_non_qwen", "architecture":"llama",
            "profile_id":"default", "backend":"cpu", "context_tier":"64k-full",
            "context_size_tokens":65536},
        "progress_events": [
            {"schema_version": 1, "sequence": 1, "phase": "prefill",
             "total_prompt_tokens": manifest["actual_tokens"], "cached_prompt_tokens": 0,
             "processed_prompt_tokens": manifest["actual_tokens"], "generated_tokens": 0,
             "elapsed_ms": 1000},
            {"schema_version": 1, "sequence": 2, "phase": "generating",
             "total_prompt_tokens": manifest["actual_tokens"], "cached_prompt_tokens": 0,
             "processed_prompt_tokens": manifest["actual_tokens"], "generated_tokens": 4,
             "elapsed_ms": 2000}],
    }
    payload["local_telemetry"] = {"progress_events": [{key: value for key, value in event.items()
            if key != "schema_version"} for event in payload["progress_events"]],
        "inference_complete": [{"active_tier": "64k-full",
            "prompt_tokens": manifest["actual_tokens"], "output_reservation": 1024,
            "inference_duration_seconds": 2.0}], "ambiguous": False, "malformed": False}
    def fake_run(command, **kwargs):
        h.Path(command[command.index("--benchmark-evidence") + 1]).write_text(json.dumps(payload))
        _write_phase(h.Path(command[command.index("--benchmark-phase-status") + 1]),
            "cleanup", 0.0, cleanup_succeeded=True)
        return subprocess.CompletedProcess(command, 0)
    result = h.invoke_packaged_runtime_adapter(app_binary=str(app), model=str(model), backend="cpu",
        relay_url="https://relay.example", cleanup_timeout_s=1, report_only=report_only,
        subprocess_run=fake_run)
    assert result["runtime_contract_pass"] is True
    assert result["pass"] is semantic_ok
    assert result["report_only_accepted"] is accepted


def test_packaged_temp_permissions_do_not_require_fchmod(tmp_path, monkeypatch):
    model = tmp_path / "model.gguf"; model.write_bytes(b"x")
    app = tmp_path / "app"; app.write_text("x"); app.chmod(0o700)
    monkeypatch.delattr(h.os, "fchmod", raising=False)
    def failed_runner(command, **kwargs):
        _write_phase(h.Path(command[command.index("--benchmark-phase-status") + 1]),
            "cleanup", 0.0, failure_reason="packaged_runner_failure", cleanup_succeeded=True)
        return subprocess.CompletedProcess(command, 1)
    result = h.invoke_packaged_runtime_adapter(app_binary=str(app), model=str(model), backend="cpu",
        relay_url="https://relay.example", cleanup_timeout_s=1, subprocess_run=failed_runner)
    assert result["code"] == "packaged_runner_failed"


@pytest.mark.parametrize(("state", "expected"), [
    ({"h": [], "b": True}, ("running", None)),
    ({"h": [], "b": False}, ("running", None)),
    ({"h": [{"role": "assistant", "content": "ok", "isTyping": False,
              "finishReason": "stop"}], "b": False}, ("completed", "ok")),
    ({"h": [{"role": "assistant", "content": "fallback"}], "b": False}, ("failed", None)),
    ({"h": [{"role": "assistant", "content": {"error": "bad"}, "isTyping": False,
              "finishReason": "error"}], "b": False}, ("failed", None)),
    ({"h": [{"role": "assistant", "content": "missing lifecycle"}], "b": False}, ("failed", None)),
])
def test_desktop_runner_requires_success_lifecycle(state, expected):
    assert h.classify_benchmark_landing_state(state) == expected


def test_desktop_runner_applies_tier_and_maps_operator_mode():
    class Browser:
        def execute_script(self, script, tier):
            assert "selectedContextTier" in script
            self.tier = tier
            return tier
    browser = Browser()
    assert h.apply_benchmark_context_tier(browser, "64k-full") == "64k-full"
    assert browser.tier == "64k-full"
    assert h.benchmark_operator_mode("cpu") == "cpu"
    assert h.benchmark_operator_mode("metal") == "gpu"
    assert h.benchmark_operator_mode("cuda") == "gpu"
    with pytest.raises(ValueError):
        h.benchmark_operator_mode("mock")


def test_packaged_multiline_prompt_uses_shift_enter_and_submits_after_exact_population(
        desktop_runner, monkeypatch):
    events = []
    prompt = "line1\nline2\n\nline4"
    class Field:
        parent = object()
        def send_keys(self, value): events.append(("text", value))
    class Button:
        def is_enabled(self): events.append(("eligibility",)); return True
        def click(self): events.append(("click",))
    field, button = Field(), Button()
    class Browser:
        def find_element(self, _by, selector):
            return field if selector == ".message-input" else button
        def execute_script(self, _script): events.append(("population",)); return prompt
    class Actions:
        def __init__(self, _parent): pass
        def key_down(self, key): events.append(("key_down", key)); return self
        def send_keys(self, key): events.append(("newline", key)); return self
        def key_up(self, key): events.append(("key_up", key)); return self
        def perform(self): events.append(("perform",)); return self
    class Wait:
        def __init__(self, browser, timeout, **_kwargs): pass
        def until(self, predicate): return predicate(Browser())
    monkeypatch.setattr(desktop_runner, "WebDriverWait", Wait)
    phases = []
    def checkpoint(phase):
        phases.append(phase)
        events.append(("phase", phase))
    started = desktop_runner._populate_and_submit_packaged_prompt(
        Browser(), prompt, lambda: 10, pytest.fail, checkpoint,
        clock=lambda: events.append(("timer",)) or 42.0, action_factory=Actions,
        before_submit=lambda: events.append(("boundary",)))
    assert started == 42.0
    assert [event for event in events if event[0] == "text"] == [
        ("text", "line1"), ("text", "line2"), ("text", "line4")]
    assert len([event for event in events if event[0] == "newline"]) == 3
    assert all(event[1] == desktop_runner.Keys.ENTER
        for event in events if event[0] == "newline")
    assert not any(event == ("text", desktop_runner.Keys.ENTER) for event in events)
    assert events.index(("population",)) < events.index(("eligibility",))
    assert events.index(("boundary",)) < events.index(("timer",))
    assert events.index(("timer",)) + 1 == events.index(("click",))
    assert phases == ["landing_page_ready", "request_active"]
    assert events.index(("click",)) < events.index(("phase", "request_active"))


def test_packaged_prompt_fails_closed_when_setup_expires_before_click(
        desktop_runner, monkeypatch):
    events = []
    prompt = "ready"
    class Field:
        parent = object()
        def send_keys(self, value): events.append(("text", value))
    class Button:
        def is_enabled(self): return True
        def click(self): events.append(("click",))
    field, button = Field(), Button()
    class Browser:
        def find_element(self, _by, selector):
            return field if selector == ".message-input" else button
        def execute_script(self, _script): return prompt
    class Wait:
        def __init__(self, browser, timeout, **_kwargs): pass
        def until(self, predicate): return predicate(Browser())
    monkeypatch.setattr(desktop_runner, "WebDriverWait", Wait)
    remaining_calls = 0
    def setup_remaining():
        nonlocal remaining_calls
        remaining_calls += 1
        if remaining_calls == 5:
            raise RuntimeError("packaged setup timeout")
        return 10
    def checkpoint(phase): events.append(("phase", phase))
    with pytest.raises(RuntimeError, match="packaged setup timeout"):
        desktop_runner._populate_and_submit_packaged_prompt(
            Browser(), prompt, setup_remaining, pytest.fail, checkpoint,
            clock=lambda: events.append(("timer",)) or 42.0)
    assert remaining_calls == 5
    assert ("phase", "landing_page_ready") in events
    assert ("timer",) not in events
    assert ("click",) not in events
    assert ("phase", "request_active") not in events


def test_packaged_prompt_rejects_inexact_vue_population(desktop_runner):
    class Field:
        parent = object()

        def send_keys(self, _value):
            pass

    class Browser:
        def find_element(self, _by, _selector):
            return Field()

        def execute_script(self, _script):
            return "partial prompt"

    failures = []

    def fail_closed(reason):
        failures.append(reason)
        raise RuntimeError(reason)

    with pytest.raises(RuntimeError, match="message_input_not_populated"):
        desktop_runner._populate_and_submit_packaged_prompt(
            Browser(), "complete prompt", lambda: 10, fail_closed, pytest.fail)
    assert failures == ["message_input_not_populated"]


def test_packaged_runner_setup_timeout_records_sanitized_cleanup_checkpoint(tmp_path):
    """Exercise the real runner's pre-launch failure and final checkpoint path."""
    source = RUNNER_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {"_is_windows_sharing_violation", "_is_windows_checkpoint_contention",
        "_write_benchmark_phase", "_remove_owned_path",
        "tauri_driver_environment", "tokenizer_stage_path", "_write_tokenizer_stage",
        "run_long_context_packaged_mode"}
    functions = [node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted]
    namespace = {
        "Path": Path, "json": json, "time": time, "tempfile": __import__("tempfile"),
        "os": os, "shutil": __import__("shutil"), "contextlib": __import__("contextlib"),
        "PACKAGED_FAILURE_REASONS": h.PACKAGED_FAILURE_REASONS, "psutil": __import__("psutil"),
        "_classify_webdriver_session_failure": lambda _exc, _process: ("webdriver_session_creation_failed", "running"),
        "_write_webdriver_diagnostic": lambda *_args: None,
        "_read_operator_start_diagnostic": lambda _driver: {},
        "_read_native_startup_diagnostic": lambda _driver: {},
        "_read_packaged_startup_diagnostic": lambda *_args: {},
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(RUNNER_SOURCE), "exec"),
        namespace)
    request_path = tmp_path / "request.json"
    phase_path = tmp_path / "phase.json"
    request_path.write_text(json.dumps({
        "phase_status_version": h.PACKAGED_PHASE_STATUS_VERSION,
        "phase_status_phases": list(h.PACKAGED_PHASES),
        "setup_timeout_s": 0,
        "cleanup_timeout_s": 1,
        "manifest": {"fixture_sha256": "0" * 64, "targets": {}},
    }))

    with pytest.raises(RuntimeError, match="packaged setup timeout"):
        namespace["run_long_context_packaged_mode"](
            request_path, tmp_path / "evidence.json", phase_path, tmp_path / "app")

    checkpoint = json.loads(phase_path.read_text())
    assert checkpoint["phase"] == "cleanup"
    assert checkpoint["failure_reason"] == "packaged_runner_failure"
    assert checkpoint["cleanup_succeeded"] is True


@pytest.mark.parametrize(("command_error", "gate_error", "launch_error", "devtools_error",
    "start_error", "ready_error", "operator_error", "expected_reason", "expected_phase",
    "expected_progress"), [
    (None, None, None, None, RuntimeError("private session exception /secret/path"),
        None, None, "webdriver_session_creation_failed", "webdriver_ready", "not_started"),
    (None, None, OSError("private launch exception /secret/path"), None, None, None, None,
        "webdriver_application_startup_failed", "webdriver_ready", "not_started"),
    (None, None, None, RuntimeError("webdriver_application_startup_failed"), None, None,
        None, "webdriver_application_startup_failed", "webdriver_ready", "not_started"),
    (None, None, None, RuntimeError("webdriver_transport_failure"), None, None, None,
        "webdriver_transport_failure", "webdriver_ready", "not_started"),
    (None, None, None, None, None, RuntimeError("private readiness exception /secret/path"),
        None, "desktop_ui_not_ready", "desktop_session_started", "not_started"),
    (None, None, None, None, None, None, RuntimeError("packaged_runner_failure"),
        "packaged_runner_failure", "desktop_ready", "operator_started"),
    (None, None, None, None, None, None, RuntimeError("operator_running_not_reached"),
        "operator_running_not_reached", "desktop_ready", "operator_started"),
    (None, None, None, None, None, None, RuntimeError("operator_registration_not_reached"),
        "operator_registration_not_reached", "desktop_ready", "operator_running"),
    (None, RuntimeError("tauri_driver_exited"), None, None, None, None, None,
        "tauri_driver_exited", "runner_startup", "not_started"),
    (None, RuntimeError("webdriver_transport_failure"), None, None, None, None, None,
        "webdriver_transport_failure", "runner_startup", "not_started"),
    (RuntimeError("native_driver_unavailable"), None, None, None, None, None, None,
        "native_driver_unavailable", "runner_startup", "not_started"),
    (RuntimeError("packaged_runner_failure"), None, None, None,
        None, None, None, "packaged_runner_failure", "runner_startup", "not_started"),
    (None, None, None, None, None, RuntimeError("posix readiness failure"), None,
        "desktop_ui_not_ready", "desktop_session_started", "not_started"),
    (None, None, None, None, None, None, RuntimeError("handoff_failure"),
        "rust_python_handoff_failed", "operator_ready", "operator_registered"),
    (None, None, None, None, None, None, RuntimeError("submit_failure"),
        "packaged_runner_failure", "operator_ready", "operator_registered"),
    (None, None, None, None, None, None, RuntimeError("final_stage_failure"),
        "tokenization_failure", "response_received", "operator_registered"),
])
def test_packaged_runner_distinguishes_desktop_session_and_ui_failures(
        tmp_path, command_error, gate_error, launch_error, devtools_error, start_error,
        ready_error, operator_error, expected_reason, expected_phase, expected_progress):
    source = RUNNER_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {"tauri_driver_environment", "tokenizer_stage_path",
        "_write_tokenizer_stage", "_webdriver_session_elapsed_bucket",
        "_webdriver_process_posture", "run_long_context_packaged_mode"}
    functions = [node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted]
    checkpoints = []
    process = SimpleNamespace(pid=1234)
    application_process = SimpleNamespace(pid=1235)
    driver = SimpleNamespace(
        find_element=lambda *_args: SimpleNamespace(click=lambda: None),
        execute_script=lambda *_args: None,
    )
    start_calls = []
    popen_calls = []
    cleaned_pids = []
    memory_roots = []

    def popen(args, **kwargs):
        popen_calls.append((args, kwargs))
        if args != ["tauri-driver"] and launch_error:
            raise launch_error
        return process if args == ["tauri-driver"] else application_process

    def start(*args, **kwargs):
        start_calls.append((args, kwargs))
        if start_error:
            raise start_error
        return driver

    def webdriver_ready(*_args, **_kwargs):
        if gate_error:
            raise gate_error

    def webview2_ready(*_args, **_kwargs):
        if devtools_error:
            raise devtools_error

    def launch_webview2(app_binary, env, output, timeout_seconds,
            *, application_args=None, process_started=None):
        application_env = dict(env)
        application_env.update({"TAURI_AUTOMATION": "true",
            "TAURI_WEBVIEW_AUTOMATION": "true"})
        try:
            launched = popen([str(app_binary.resolve()),
                "--edge-webview-switches=--remote-debugging-port=49152",
                *list(application_args or [])], cwd=app_binary.resolve().parent,
                env=application_env, stdout=output, stderr=-2, text=True)
        except (OSError, ValueError):
            raise RuntimeError("webdriver_application_startup_failed") from None
        if process_started is not None:
            process_started(launched)
        try:
            webview2_ready(launched, 49152, timeout_seconds)
        except Exception:
            cleaned_pids.append(launched.pid)
            raise
        return launched, "127.0.0.1:49152"

    def driver_command():
        if command_error:
            raise command_error
        return ["tauri-driver"]

    def ready(*_args, **_kwargs):
        if ready_error:
            raise ready_error

    def post_start(_driver, _remaining, record_progress, fail_closed, _relay_url,
            record_relay_observation):
        if operator_error:
            if str(operator_error) in {
                    "handoff_failure", "submit_failure", "final_stage_failure"}:
                record_progress("operator_running")
                record_progress("operator_registered")
                record_relay_observation("registered")
                return
            if str(operator_error) == "operator_registration_not_reached":
                record_progress("operator_running")
            if str(operator_error) in h.PACKAGED_FAILURE_REASONS:
                fail_closed(str(operator_error))
            raise operator_error

    fake_os = SimpleNamespace(**vars(os))
    fake_os.name = "posix" if str(ready_error) == "posix readiness failure" else "nt"
    diagnostics = []
    class MemorySampler:
        def __init__(self, pid):
            memory_roots.append(pid)

        def sample(self):
            return True

        def summary(self):
            return {"peak_rss_bytes": 1}

    class LandingBrowser:
        def set_page_load_timeout(self, _timeout):
            pass

        def set_script_timeout(self, _timeout):
            pass

        def get(self, _url):
            pass

        def execute_script(self, script):
            if "FinalMetadata" in script:
                return {"prompt_tokens": 1, "completion_tokens": 1,
                    "finish_reason": "stop"}
            if "GenerationSettings" in script:
                return {"supplied": {}, "omitted_runtime_default": []}
            if "return {p:" in script:
                return {"p": {"sequence": 1}, "h": [{"role": "assistant",
                    "content": "bounded response", "isTyping": False,
                    "finishReason": "stop"}], "b": False, "t": "8k-fast"}
            return None

    namespace = {
        "Path": Path, "json": json, "time": time, "tempfile": __import__("tempfile"),
        "os": fake_os, "PACKAGED_FAILURE_REASONS": h.PACKAGED_FAILURE_REASONS,
        "By": SimpleNamespace(XPATH="xpath"),
        "WEBDRIVER_READINESS_CATEGORIES": frozenset({"ready", "no_window_handle",
            "wrong_handle", "missing_shell", "missing_required_controls",
            "webdriver_failure", "unknown"}),
        "_classify_webdriver_session_failure": lambda _exc, _process: (
            "webdriver_session_creation_failed", "running", "unknown"),
            "_write_webdriver_diagnostic": lambda *args: diagnostics.append(args),
            "_read_native_startup_diagnostic": lambda _driver: {
                "native_startup_phase": "startup_task_failed",
                "native_startup_outcome": "failed",
                "native_startup_failure_category": "child_spawn_failed",
            },
            "_read_packaged_startup_diagnostic": lambda *_args: {},
            "_read_operator_start_diagnostic": lambda _driver: {
                "start_handler_state": "entered", "invocation_state": "resolved",
                "native_event_observation": "running_rejected",
                "polling_observation": "not_running", "render_state": "not_running",
            },
        "psutil": __import__("psutil"),
        "subprocess": SimpleNamespace(
            Popen=popen, STDOUT=-2),
        "tauri_driver_command": driver_command, "TAURI_ROOT": tmp_path,
        "OwnedProcessTreeMemorySampler": MemorySampler,
        "wait_for_webdriver_ready": webdriver_ready,
        "wait_for_webview2_devtools": webview2_ready,
        "launch_webview2_application": launch_webview2,
        "reserve_free_port": lambda: 49152,
        "start_driver": start, "tokenizer_handoff_args": lambda *_args: [
            "--tokenizer-request=request.json", "--tokenizer-evidence=evidence.json"],
        "wait_for_ui_ready": ready,
        "fill_input_by_label": lambda *_args: None,
        "benchmark_operator_mode": lambda _backend: "cpu",
        "wait_for_start_operator_enabled": lambda *_args, **_kwargs: None,
        "require_clean_relay_registration_baseline": lambda *_args, **_kwargs: 0,
        "wait_for_post_start_operator_state": post_start,
        "_validate_operator_tokenizer_handoff": lambda _evidence, fail_closed: (
            fail_closed("rust_python_handoff_failed")
            if str(operator_error) == "handoff_failure" else None),
        "_status_value": lambda _driver, label: {
            "Launcher source": "bundled", "Backend selected": "cpu",
            "Backend used": "cpu", "Runtime ID": "runtime",
        }.get(label, "bounded"),
        "_readiness_diagnostics_map": lambda _driver: {},
        "packaged_runtime_configuration": lambda *_args: {},
        "start_landing_driver": LandingBrowser,
        "_prepare_packaged_landing_page": lambda *_args: None,
        "_rearm_tokenizer_stage": lambda _evidence, _log: 0,
        "_populate_and_submit_packaged_prompt": lambda *_args, **kwargs: (
            kwargs["before_submit"](),
            (_ for _ in ()).throw(RuntimeError("packaged_runner_failure")),
        )[-1] if str(operator_error) == "submit_failure" else (
            kwargs["before_submit"]() or time.monotonic()),
        "classify_benchmark_landing_state": h.classify_benchmark_landing_state,
        "parse_packaged_local_telemetry": lambda _text: {},
        "observe_post_terminal": lambda *_args, **_kwargs: [],
        "_validate_final_tokenizer_stage": lambda _evidence, fail_closed: (
            fail_closed("tokenization_failure")
            if str(operator_error) == "final_stage_failure" else None),
        "_validate_packaged_failure_reason": lambda reason: reason,
        "_write_benchmark_phase": lambda *_args, **kwargs: checkpoints.append(dict(kwargs)),
        "_cleanup_owned_process_tree": lambda owned, *_args: (
            cleaned_pids.append(owned.pid) or True),
        "_quit_webdriver": lambda *_args: True,
        "_remove_owned_path": lambda *_args, **_kwargs: True,
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(RUNNER_SOURCE), "exec"),
        namespace)
    app = tmp_path / "current-head.exe"
    app.write_bytes(b"app")
    model = tmp_path / "model.gguf"
    model.write_bytes(b"model")
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps({
        "phase_status_version": h.PACKAGED_PHASE_STATUS_VERSION,
        "phase_status_phases": list(h.PACKAGED_PHASES), "setup_timeout_s": 10,
        "cleanup_timeout_s": 1,
        "model": str(model), "relay_url": "https://relay.example",
        "backend": "cpu", "context_tier": "8k-fast",
        "request_timeout_s": 1, "finalization_timeout_s": 1,
        "prompt": "bounded prompt",
        "manifest": {"fixture_sha256": "0" * 64, "targets": {}},
    }))

    with pytest.raises(RuntimeError, match=f"^{expected_reason}$") as raised:
        namespace["run_long_context_packaged_mode"](
            request_path, tmp_path / "evidence.json", tmp_path / "phase.json", app)

    assert raised.value.__cause__ is None
    assert "private" not in str(raised.value)
    assert checkpoints[-1]["last_safe_phase"] == expected_phase
    assert checkpoints[-1]["failure_reason"] == expected_reason
    assert checkpoints[-1]["cleanup_succeeded"] is True
    assert diagnostics[-1][-4] == expected_progress
    assert diagnostics[-1][-3]["start_handler_state"] == "entered"
    assert diagnostics[-1][-2] == {
        "native_startup_phase": "startup_task_failed",
        "native_startup_outcome": "failed",
        "native_startup_failure_category": "child_spawn_failed",
    }
    assert diagnostics[-1][-1] == {}
    assert len(start_calls) == (
        0 if command_error or gate_error or launch_error or devtools_error else 1)
    if fake_os.name == "nt" and not command_error and not gate_error and not launch_error:
        app_args, app_kwargs = popen_calls[1]
        assert app_args == [str(app.resolve()),
            "--edge-webview-switches=--remote-debugging-port=49152",
            "--tokenizer-request=request.json", "--tokenizer-evidence=evidence.json"]
        assert app_kwargs["cwd"] == app.parent
        assert "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS" not in app_kwargs["env"]
        webview2_user_data = Path(app_kwargs["env"]["WEBVIEW2_USER_DATA_FOLDER"])
        assert webview2_user_data.resolve(strict=True) == (
            Path(app_kwargs["env"]["HOME"]) / "WebView2").resolve(strict=True)
        assert webview2_user_data.is_absolute()
        assert webview2_user_data.is_dir()
        assert app_kwargs["env"]["TAURI_AUTOMATION"] == "true"
        assert app_kwargs["env"]["TAURI_WEBVIEW_AUTOMATION"] == "true"
        if not devtools_error:
            assert start_calls[0][1]["application_args"] == app_args[2:]
            assert start_calls[0][1]["debugger_address"] == "127.0.0.1:49152"
        assert memory_roots == [1235]
        assert sorted(cleaned_pids) == [1234, 1235]


def test_packaged_runner_primary_failure_survives_cleanup_failure(tmp_path):
    source = RUNNER_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {"_is_windows_sharing_violation", "_is_windows_checkpoint_contention",
        "_write_benchmark_phase", "_remove_owned_path",
        "tauri_driver_environment", "tokenizer_stage_path", "_write_tokenizer_stage",
        "run_long_context_packaged_mode"}
    functions = [node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted]
    namespace = {
        "Path": Path, "json": json, "time": time, "tempfile": __import__("tempfile"),
        "os": os, "shutil": __import__("shutil"), "contextlib": __import__("contextlib"),
        "PACKAGED_FAILURE_REASONS": h.PACKAGED_FAILURE_REASONS, "psutil": __import__("psutil"),
        "_classify_webdriver_session_failure": lambda _exc, _process: ("webdriver_session_creation_failed", "running"),
        "_write_webdriver_diagnostic": lambda *_args: None,
        "_read_operator_start_diagnostic": lambda _driver: {},
        "_read_native_startup_diagnostic": lambda _driver: {},
        "_read_packaged_startup_diagnostic": lambda *_args: {},
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(RUNNER_SOURCE), "exec"),
        namespace)
    namespace["_remove_owned_path"] = lambda *_args, **_kwargs: False
    request_path = tmp_path / "request.json"
    phase_path = tmp_path / "phase.json"
    request_path.write_text(json.dumps({
        "phase_status_version": h.PACKAGED_PHASE_STATUS_VERSION,
        "phase_status_phases": list(h.PACKAGED_PHASES), "setup_timeout_s": 0,
        "cleanup_timeout_s": 1,
        "manifest": {"fixture_sha256": "0" * 64, "targets": {}},
    }))

    with pytest.raises(RuntimeError, match="packaged setup timeout"):
        namespace["run_long_context_packaged_mode"](
            request_path, tmp_path / "evidence.json", phase_path, tmp_path / "app")

    checkpoint = json.loads(phase_path.read_text())
    assert checkpoint["failure_reason"] == "packaged_runner_failure"
    assert checkpoint["cleanup_succeeded"] is False


def test_packaged_runner_provisional_checkpoint_retry_preserves_cleanup_allowance(tmp_path):
    """The provisional publish gets the small retry window, not all cleanup time."""
    source = RUNNER_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {"_is_windows_sharing_violation", "_is_windows_checkpoint_contention",
        "_write_benchmark_phase",
        "_remove_owned_path", "tauri_driver_environment", "tokenizer_stage_path",
        "_write_tokenizer_stage", "run_long_context_packaged_mode"}
    functions = [node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted]
    now = [0.0]
    fake_time = SimpleNamespace(monotonic=lambda: now[0], sleep=lambda delay: None)
    namespace = {
        "Path": Path, "json": json, "time": fake_time,
        "tempfile": __import__("tempfile"), "os": os,
        "shutil": __import__("shutil"), "contextlib": __import__("contextlib"),
        "PACKAGED_FAILURE_REASONS": h.PACKAGED_FAILURE_REASONS,
        "_classify_webdriver_session_failure": lambda _exc, _process: (
            "webdriver_session_creation_failed", "running"),
        "_write_webdriver_diagnostic": lambda *_args: None,
        "_read_operator_start_diagnostic": lambda _driver: {},
        "_read_native_startup_diagnostic": lambda _driver: {},
        "_read_packaged_startup_diagnostic": lambda *_args: {},
        "psutil": __import__("psutil"), "sys": sys,
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(RUNNER_SOURCE), "exec"),
        namespace)
    writes = []
    removals = []

    def write_phase(_path, _phase, _started, _version, _phases, **kwargs):
        writes.append(kwargs)
        if kwargs.get("cleanup_succeeded") is False and len(writes) == 2:
            now[0] += kwargs["retry_timeout_s"]
            raise RuntimeError("phase checkpoint publication failed")

    def remove_path(_path, deadline, **_kwargs):
        removals.append(deadline - now[0])
        return True

    namespace["_write_benchmark_phase"] = write_phase
    namespace["_remove_owned_path"] = remove_path
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps({
        "phase_status_version": h.PACKAGED_PHASE_STATUS_VERSION,
        "phase_status_phases": list(h.PACKAGED_PHASES), "setup_timeout_s": 0,
        "cleanup_timeout_s": 10,
        "manifest": {"fixture_sha256": "0" * 64, "targets": {}},
    }))

    with pytest.raises(RuntimeError, match="packaged setup timeout"):
        namespace["run_long_context_packaged_mode"](
            request_path, tmp_path / "evidence.json", tmp_path / "phase.json",
            tmp_path / "app")

    provisional = [write for write in writes
        if write.get("cleanup_succeeded") is False]
    assert provisional[0]["retry_timeout_s"] == 1.0
    assert len(removals) == 3
    assert removals == [pytest.approx(9.0)] * 3
    assert writes[-1]["cleanup_succeeded"] is False
    assert writes[-1]["failure_reason"] == "packaged_runner_failure"


def test_packaged_runner_log_close_failure_preserves_primary_and_finishes_cleanup(
        tmp_path, monkeypatch):
    """A log-close fault cannot interrupt owned cleanup or final reporting."""
    source = RUNNER_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {"tauri_driver_environment", "tokenizer_stage_path",
        "_write_tokenizer_stage", "run_long_context_packaged_mode"}
    functions = [node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted]
    events = []
    checkpoints = []
    removed = []
    process = SimpleNamespace(pid=1234)

    class ClosingFault:
        def __init__(self, handle):
            self.handle = handle

        def write(self, value):
            return self.handle.write(value)

        def close(self):
            events.append("log_close")
            self.handle.close()
            raise RuntimeError("private close detail /absolute/private/path")

    real_io_open = __import__("io").open

    def open_with_closing_fault(path, *args, **kwargs):
        handle = real_io_open(path, *args, **kwargs)
        if str(path).endswith(".log") and "long-context-tauri-driver-" in str(path):
            return ClosingFault(handle)
        return handle

    monkeypatch.setattr("io.open", open_with_closing_fault)
    namespace = {
        "Path": Path, "json": json, "time": time, "tempfile": __import__("tempfile"),
        "os": os, "PACKAGED_FAILURE_REASONS": h.PACKAGED_FAILURE_REASONS,
        "_classify_webdriver_session_failure": lambda _exc, _process: ("webdriver_session_creation_failed", "running"),
        "_write_webdriver_diagnostic": lambda *_args: None,
        "_read_operator_start_diagnostic": lambda _driver: {},
        "_read_native_startup_diagnostic": lambda _driver: {},
        "_read_packaged_startup_diagnostic": lambda *_args: {},
        "subprocess": SimpleNamespace(Popen=lambda *_args, **_kwargs: process, STDOUT=-2),
        "tauri_driver_command": lambda: ["tauri-driver"], "TAURI_ROOT": tmp_path,
        "OwnedProcessTreeMemorySampler": lambda _pid: object(),
        "wait_for_webdriver_ready": lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("primary packaged failure")),
        "_write_benchmark_phase": lambda *_args, **kwargs: checkpoints.append(dict(kwargs)),
        "_cleanup_owned_process_tree": lambda owned, _remaining: (
            events.append(("process_cleanup", owned.pid)) or True),
        "_remove_owned_path": lambda path, *_args, **_kwargs: (
            events.append(("remove", path)) or removed.append(path) or True),
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(RUNNER_SOURCE), "exec"),
        namespace)
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps({
        "phase_status_version": h.PACKAGED_PHASE_STATUS_VERSION,
        "phase_status_phases": list(h.PACKAGED_PHASES), "setup_timeout_s": 10,
        "cleanup_timeout_s": 1,
        "manifest": {"fixture_sha256": "0" * 64, "targets": {}},
    }))

    with pytest.raises(RuntimeError, match="primary packaged failure") as raised:
        namespace["run_long_context_packaged_mode"](
            request_path, tmp_path / "evidence.json", tmp_path / "phase.json",
            tmp_path / "app")

    assert events.index(("process_cleanup", 1234)) < events.index("log_close")
    assert events.index("log_close") < min(events.index(("remove", path)) for path in removed)
    assert len(removed) == 3
    assert checkpoints[-1]["cleanup_succeeded"] is False
    assert checkpoints[-1]["failure_reason"] == "packaged_runner_failure"
    report = json.dumps(checkpoints)
    assert "private close detail" not in report
    assert str(tmp_path) not in report
    assert "private close detail" not in str(raised.value)


class _CleanupProcess:
    def __init__(self, name, events, *, terminate_error=False, kill_error=False):
        self.name = name
        self.events = events
        self.terminate_error = terminate_error
        self.kill_error = kill_error

    def terminate(self):
        self.events.append(f"terminate:{self.name}")
        if self.terminate_error:
            raise RuntimeError("private terminate detail /absolute/path")

    def kill(self):
        self.events.append(f"kill:{self.name}")
        if self.kill_error:
            raise RuntimeError("private kill detail /absolute/path")


def test_cleanup_owned_process_tree_recurses_kills_survivor_and_waits(desktop_runner,
        monkeypatch):
    events = []
    child = _CleanupProcess("child", events)
    root = _CleanupProcess("root", events)
    root.children = lambda recursive: (events.append(f"children:{recursive}") or [child])
    monkeypatch.setattr(desktop_runner.psutil, "Process", lambda pid:
        (events.append(f"root:{pid}") or root))
    wait_calls = [0]

    def wait_procs(processes, timeout):
        wait_calls[0] += 1
        events.append(f"wait:{wait_calls[0]}:{','.join(item.name for item in processes)}")
        return ([], [child]) if wait_calls[0] == 1 else ([child], [])

    monkeypatch.setattr(desktop_runner.psutil, "wait_procs", wait_procs)
    parent = SimpleNamespace(pid=731, wait=lambda timeout: events.append("parent-wait"))

    assert desktop_runner._cleanup_owned_process_tree(parent, lambda: 5.0) is True
    assert events == ["root:731", "children:True", "terminate:child", "terminate:root",
        "wait:1:child,root", "kill:child", "wait:2:child", "parent-wait"]


@pytest.mark.parametrize("fault", [
    "access", "terminate", "first_wait", "kill", "second_wait", "process_wait",
])
def test_cleanup_owned_process_tree_faults_are_non_raising_failures(
        desktop_runner, monkeypatch, fault):
    events = []
    child = _CleanupProcess("child", events, terminate_error=fault == "terminate",
        kill_error=fault == "kill")
    root = _CleanupProcess("root", events)
    root.children = lambda recursive: [child]

    def discover(_pid):
        if fault == "access":
            raise desktop_runner.psutil.AccessDenied(731)
        return root

    monkeypatch.setattr(desktop_runner.psutil, "Process", discover)
    wait_calls = [0]

    def wait_procs(processes, timeout):
        wait_calls[0] += 1
        events.append(f"wait:{wait_calls[0]}")
        if fault == "first_wait" and wait_calls[0] == 1:
            raise RuntimeError("private first wait /absolute/path")
        if fault == "second_wait" and wait_calls[0] == 2:
            raise RuntimeError("private second wait /absolute/path")
        return ([], [child]) if wait_calls[0] == 1 else ([child], [])

    monkeypatch.setattr(desktop_runner.psutil, "wait_procs", wait_procs)

    def parent_wait(timeout):
        events.append("parent-wait")
        if fault == "process_wait":
            raise RuntimeError("private parent wait /absolute/path")

    parent = SimpleNamespace(pid=731, wait=parent_wait)
    assert desktop_runner._cleanup_owned_process_tree(parent, lambda: 5.0) is False
    assert "parent-wait" in events


def test_cleanup_owned_process_tree_treats_missing_root_as_already_gone(
        desktop_runner, monkeypatch):
    monkeypatch.setattr(desktop_runner.psutil, "Process", lambda _pid: (_ for _ in ()).throw(
        desktop_runner.psutil.NoSuchProcess(731)))
    parent = SimpleNamespace(pid=731, wait=lambda timeout: None)
    assert desktop_runner._cleanup_owned_process_tree(parent, lambda: 5.0) is True


def test_cleanup_owned_process_tree_deadline_exhaustion_is_non_raising(
        desktop_runner, monkeypatch):
    child = _CleanupProcess("child", [])
    root = _CleanupProcess("root", [])
    root.children = lambda recursive: [child]
    monkeypatch.setattr(desktop_runner.psutil, "Process", lambda _pid: root)
    parent = SimpleNamespace(pid=731, wait=lambda timeout: pytest.fail("waited"))
    assert desktop_runner._cleanup_owned_process_tree(parent, lambda: 0.0) is False


def test_cleanup_owned_process_tree_ignores_racing_process_exits(
        desktop_runner, monkeypatch):
    class ExitedProcess:
        def terminate(self):
            raise desktop_runner.psutil.NoSuchProcess(732)

        def kill(self):
            raise desktop_runner.psutil.NoSuchProcess(732)

    child = ExitedProcess()
    root = SimpleNamespace(children=lambda recursive: [child], terminate=lambda: None)
    monkeypatch.setattr(desktop_runner.psutil, "Process", lambda _pid: root)
    monkeypatch.setattr(desktop_runner.psutil, "wait_procs",
        lambda processes, timeout: ([], [child]))
    parent = SimpleNamespace(pid=731, wait=lambda timeout: None)
    assert desktop_runner._cleanup_owned_process_tree(parent, lambda: 5.0) is False


@pytest.mark.parametrize("reason", [
    "vue_not_ready", "client_keypair_not_ready", "model_selection_not_ready",
    "send_button_not_enabled",
])
def test_packaged_setup_exhaustion_preserves_specific_failure_reason(desktop_runner, reason):
    observed = []
    def exhausted():
        raise RuntimeError("packaged setup timeout")
    def fail_closed(value):
        observed.append(value)
        raise LookupError(value)
    with pytest.raises(LookupError, match=reason):
        desktop_runner._wait_for_packaged_setup_condition(
            object(), exhausted, lambda _browser: True, reason, fail_closed)
    assert observed == [reason]


def test_packaged_landing_page_readiness_checks_and_context_are_bounded(
        desktop_runner, monkeypatch):
    scripts = []
    remaining_calls = []

    class Browser:
        def execute_script(self, script, *_args):
            scripts.append(script)
            return "64k-full" if "selectedContextTier" in script else True

    class Wait:
        def __init__(self, browser, timeout, **_kwargs):
            self.browser = browser
            assert timeout == 10

        def until(self, predicate):
            return predicate(self.browser)

    monkeypatch.setattr(desktop_runner, "WebDriverWait", Wait)

    def setup_remaining():
        remaining_calls.append(True)
        return 10

    desktop_runner._prepare_packaged_landing_page(
        Browser(), setup_remaining, pytest.fail, "64k-full")
    assert len(remaining_calls) == 4
    assert any("hasClientKeypair" in script for script in scripts)
    assert any("modelsLoaded" in script for script in scripts)


def test_packaged_landing_page_rejects_wrong_context_tier(desktop_runner, monkeypatch):
    class Browser:
        def execute_script(self, script, *_args):
            return "8k-fast" if "selectedContextTier" in script else True

    class Wait:
        def __init__(self, browser, _timeout, **_kwargs):
            self.browser = browser

        def until(self, predicate):
            return predicate(self.browser)

    monkeypatch.setattr(desktop_runner, "WebDriverWait", Wait)
    with pytest.raises(RuntimeError, match="requested_context_tier_not_applied"):
        desktop_runner._prepare_packaged_landing_page(
            Browser(), lambda: 10,
            lambda reason: (_ for _ in ()).throw(RuntimeError(reason)), "64k-full")


def test_packaged_runner_never_bypasses_production_send_eligibility():
    source = RUNNER_SOURCE.read_text(encoding="utf-8")
    benchmark = source[source.index("def run_long_context_packaged_mode"):source.index(
        "def _long_context_followup_request")]
    assert "sendMessage(" not in benchmark
    assert "disabled = false" not in benchmark
    assert "removeAttribute('disabled')" not in benchmark
    assert '_populate_and_submit_packaged_prompt(browser, request["prompt"]' in benchmark


def test_packaged_failure_reasons_are_explicit_low_cardinality_categories():
    assert {"vue_not_ready", "client_keypair_not_ready", "model_selection_not_ready",
        "requested_context_tier_not_applied", "message_input_not_populated",
        "send_button_not_enabled"}.issubset(h.PACKAGED_FAILURE_REASONS)
    assert all(value.isascii() and value.replace("_", "").islower()
        for value in h.PACKAGED_FAILURE_REASONS)


@pytest.mark.parametrize("reason", sorted(h.PACKAGED_FAILURE_REASONS))
def test_desktop_runner_accepts_every_packaged_failure_reason(desktop_runner, reason):
    assert desktop_runner._validate_packaged_failure_reason(reason) == reason


def test_desktop_runner_rejects_unlisted_packaged_failure_reason(desktop_runner):
    with pytest.raises(RuntimeError, match="invalid packaged failure reason"):
        desktop_runner._validate_packaged_failure_reason("prompt content")


def test_owned_runner_keeps_only_bounded_diagnostic_tail():
    completed = h._run_owned_runner(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 10000 + 'TAIL')"], 2, 1)
    assert completed.returncode == 0
    assert len(completed.stdout) <= 2048
    assert completed.stdout.endswith("TAIL")


class _TimedOutProcess:
    pid = 731
    stdout = None

    def __init__(self, waits):
        self.waits = iter(waits)
        self.killed = False

    def wait(self, timeout):
        outcome = next(self.waits)
        if outcome == "timeout":
            time.sleep(timeout)
            raise subprocess.TimeoutExpired("runner", timeout)
        return outcome

    def kill(self):
        self.killed = True


def _write_phase(path, phase, elapsed, *, last_safe_phase=None, failure_reason=None,
        cleanup_succeeded=None):
    path.write_text(json.dumps({"schema_version": h.PACKAGED_PHASE_STATUS_VERSION,
        "phase": phase, "sequence": h.PACKAGED_PHASES.index(phase) + 1,
        "last_safe_phase": last_safe_phase or phase, "failure_reason": failure_reason,
        "elapsed_s": elapsed, "cleanup_succeeded": cleanup_succeeded}))


def test_owned_runner_allows_on_time_child_cleanup_once(tmp_path):
    clock = [0.0]
    phase = tmp_path / "phase.json"
    class Process(_TimedOutProcess):
        def wait(self, timeout):
            clock[0] += timeout
            if clock[0] >= 2.0 and not phase.exists():
                _write_phase(phase, "cleanup", 2.0)
            if clock[0] < 4.0:
                raise subprocess.TimeoutExpired("runner", timeout)
            return 0
    process = Process([])
    process.stdout = type("Output", (), {"read": lambda self, size: b""})()
    completed = h._run_owned_runner(["runner"], 10, 5,
        popen=lambda _command, **_kwargs: process, phase_status_path=phase,
        platform_name="posix", clock=lambda: clock[0])
    assert completed.returncode == 0
    assert 4.0 <= clock[0] < 5.1
    assert process.killed is False


def test_owned_runner_work_timeout_does_not_borrow_cleanup_window(tmp_path):
    clock = [0.0]
    phase = tmp_path / "phase.json"
    _write_phase(phase, "request_active", 9.5)
    class Process(_TimedOutProcess):
        def wait(self, timeout):
            if process.killed:
                return 0
            clock[0] += timeout
            raise subprocess.TimeoutExpired("runner", timeout)
    process = Process([])
    process.stdout = type("Output", (), {"read": lambda self, size: b""})()
    signals = []
    def kill_group(pid, sig):
        signals.append(sig)
        if sig == h.POSIX_SIGTERM:
            process.killed = True
        if sig == 0:
            raise ProcessLookupError
    with pytest.raises(h.PackagedRunnerTimeout) as raised:
        h._run_owned_runner(["runner"], 10, 5,
            popen=lambda _command, **_kwargs: process, phase_status_path=phase,
            killpg=kill_group, platform_name="posix", clock=lambda: clock[0])
    assert raised.value.timeout == 10
    assert h.POSIX_SIGTERM in signals
    assert raised.value.cleanup_succeeded is True


def test_owned_runner_cleanup_overrun_is_bounded_and_fails_closed(tmp_path):
    clock = [0.0]
    phase = tmp_path / "phase.json"
    class Process(_TimedOutProcess):
        def wait(self, timeout):
            clock[0] += timeout
            if clock[0] >= 2.0 and not phase.exists():
                _write_phase(phase, "cleanup", 2.0)
            raise subprocess.TimeoutExpired("runner", timeout)
    process = Process([])
    process.stdout = type("Output", (), {"read": lambda self, size: b""})()
    signals = []
    with pytest.raises(h.PackagedRunnerTimeout) as raised:
        h._run_owned_runner(["runner"], 10, 5,
            popen=lambda _command, **_kwargs: process, phase_status_path=phase,
            killpg=lambda _pid, sig: signals.append(sig), platform_name="posix",
            clock=lambda: clock[0])
    assert 5.0 <= clock[0] < 10.0
    assert h.POSIX_SIGKILL in signals
    assert raised.value.cleanup_succeeded is False


def test_owned_runner_posix_terminates_exact_process_group(monkeypatch):
    process = _TimedOutProcess(["timeout", "timeout", -9])
    process.stdout = type("Output", (), {"read": lambda self, size: b""})()
    launched = {}
    signals = []
    def kill_group(pid, sig):
        signals.append((pid, sig))
        if sig == 0:
            raise ProcessLookupError
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        h._run_owned_runner(["runner"], 1, 2,
            popen=lambda command, **kwargs: launched.update(kwargs) or process,
            killpg=kill_group, platform_name="posix", phase_poll_interval_s=10)
    assert launched["start_new_session"] is True
    assert signals == [(731, h.POSIX_SIGTERM), (731, h.POSIX_SIGKILL),
        (731, h.POSIX_SIGKILL)]
    assert raised.value.cleanup_succeeded is False


def test_owned_runner_posix_does_not_claim_cleanup_while_group_survives(monkeypatch):
    clock = [0.0]
    class Process(_TimedOutProcess):
        def wait(self, timeout):
            if self.killed:
                return 0
            clock[0] += timeout
            raise subprocess.TimeoutExpired("runner", timeout)
    process = Process([])
    process.stdout = type("Output", (), {"read": lambda self, size: b""})()
    monkeypatch.setattr(h.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    def surviving_group(_pid, sig):
        if sig == h.POSIX_SIGTERM:
            process.killed = True
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        h._run_owned_runner(["runner"], 1, 2,
            popen=lambda _command, **_kwargs: process,
            killpg=surviving_group, platform_name="posix", clock=lambda: clock[0])
    assert raised.value.cleanup_succeeded is False


@pytest.mark.parametrize("cleanup_outcome", ["failed", "timeout"])
def test_owned_runner_windows_never_invokes_injected_killpg(cleanup_outcome):
    process = _TimedOutProcess(["timeout", 1] if cleanup_outcome == "failed" else ["timeout", 1])
    process.stdout = type("Output", (), {"read": lambda self, size: b""})()
    launched = {}; cleanup = []
    def cleanup_run(command, **kwargs):
        cleanup.append((command, kwargs))
        if cleanup_outcome == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.CompletedProcess(command, 1)
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        h._run_owned_runner(["runner"], 1, 2,
            popen=lambda command, **kwargs: launched.update(kwargs) or process,
            cleanup_run=cleanup_run,
            killpg=lambda *_args: pytest.fail("Windows cleanup called POSIX killpg"),
            platform_name="nt", phase_poll_interval_s=10)
    assert launched["creationflags"] == getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    assert cleanup[0][0] == ["taskkill", "/PID", "731", "/T", "/F"]
    assert process.killed is True
    assert raised.value.cleanup_succeeded is False


def test_owned_runner_windows_never_resolves_os_killpg(monkeypatch):
    process = _TimedOutProcess(["timeout", 1])
    process.stdout = type("Output", (), {"read": lambda self, size: b""})()
    cleanup = []
    monkeypatch.delattr(os, "killpg", raising=False)

    with pytest.raises(subprocess.TimeoutExpired):
        h._run_owned_runner(["runner"], 1, 2,
            popen=lambda _command, **_kwargs: process,
            cleanup_run=lambda command, **kwargs: cleanup.append((command, kwargs))
            or subprocess.CompletedProcess(command, 0),
            platform_name="nt", phase_poll_interval_s=10)

    assert cleanup[0][0] == ["taskkill", "/PID", "731", "/T", "/F"]


def test_owned_runner_posix_fails_closed_without_killpg(monkeypatch):
    process = _TimedOutProcess(["timeout", 1])
    process.stdout = type("Output", (), {"read": lambda self, size: b""})()
    monkeypatch.delattr(os, "killpg", raising=False)
    with pytest.raises(RuntimeError, match="^owned_process_group_cleanup_unavailable$"):
        h._run_owned_runner(["runner"], 1, 2,
            popen=lambda _command, **_kwargs: process, platform_name="posix",
            phase_poll_interval_s=10)
    assert process.killed is True


def test_main_generate_and_evaluate_commands(tmp_path):
    fixture_dir = tmp_path / "fixture"
    assert h.main(["generate-fixture", "--fixture", "small-8k", "--scenario", "structured-extraction", "--out-dir", str(fixture_dir)]) == 0
    manifest_path = fixture_dir / "small-8k.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    response_path = tmp_path / "response.json"
    response_path.write_text(json.dumps(manifest["expected_answers"]))
    report_dir = tmp_path / "report"
    assert h.main(["evaluate", "--manifest", str(manifest_path), "--response",
        str(response_path), "--strict", "--out-dir", str(report_dir)]) == 0
    assert json.loads((report_dir / "long_context_benchmark_report.json").read_text())["semantic"]["semantic_pass"]


def test_bounded_external_fixture_reader_rejects_oversized_utf8(tmp_path):
    fixture = tmp_path / "fixture.txt"
    fixture.write_text("éé", encoding="utf-8")

    assert h._read_bounded_text(str(fixture), limit=4) == "éé"
    with pytest.raises(ValueError, match="fixture_too_large"):
        h._read_bounded_text(str(fixture), limit=3)


def test_main_packaged_runtime_exit_codes(tmp_path, monkeypatch):
    evidence = {"pass": False, "report_only_accepted": True, "runtime_contract_pass":True,
        "fixture":{"sha256":"abc", "authoritative_prompt_tokens":10},
        "runtime":{"app_identity":"token.place", "runtime_identity":"bundled",
            "build_identity":"build", "backend_requested":"cpu", "backend_selected":"cpu", "model_fingerprint":"sha256:model", "backend_used":"cpu"},
        "authoritative_local_progress":{"pass":True, "progress_event_count":2,
            "observed_phases":["prefill", "generating"]},
        "encrypted_progress":{"pass":True, "best_effort":True,
            "progress_event_count":1, "observed_phases":["prefill"],
            "terminal_overtook_generating_update":True},
        "response_usage":{"prompt_tokens":10, "completion_tokens":1,
            "finish_reason":"stop", "source":"validated_atomic_response_usage"},
        "atomic_response_completion":{"completed":True,
            "source":"browser_decrypted_final_response"},
        "post_terminal_silence":{"observed":True,
            "source":"pre_cancellation_primary_snapshot"},
        "metrics":{"pass":True, "preparing_duration_s":0, "prefill_duration_s":1,
            "time_to_first_token_s":1, "local_inference_duration_s":2,
            "end_to_end_request_duration_s":2, "prompt_tokens":10, "output_tokens":1,
            "prompt_tokens_per_s":10, "request_budget_s":600, "completion_margin_s":598,
            "phase_timing_source":"worker_progress_elapsed_ms",
            "inference_timing_source":"parent_inference_monotonic",
            "request_timing_source":"runner_end_to_end_monotonic",
            "completion_token_source":"validated_response_usage"},
        "semantic":{"semantic_pass":False, "exact_match":False, "errors":["exact_match"]},
        "generation_settings":{"supplied":{"max_tokens":1024},
            "omitted_runtime_default":["seed", "temperature", "top_p"]},
        "memory": _memory_evidence(),
        "runtime_configuration": _runtime_configuration(),
        "kv_compare":{"pass":True, "applicability":"not_applicable_verified_non_qwen",
            "reason":"not_applicable_verified_non_qwen",
            "attestation":{"method":"active_runtime_selected_profile",
                "applicability":"not_applicable_verified_non_qwen", "architecture":"llama",
                "profile_id":"default", "backend":"cpu", "context_tier":"64k-full",
                "context_size_tokens":65536}}}
    monkeypatch.setattr(h, "invoke_packaged_runtime_adapter", lambda **kwargs: evidence)
    args = ["packaged-runtime", "--out-dir", str(tmp_path), "--app-binary", "app",
        "--model", "model", "--backend", "cpu", "--relay-url", "https://relay.example",
        "--report-only"]
    assert h.main(args) == 0
    report = json.loads((tmp_path / "long_context_benchmark_report.json").read_text())
    assert report["overall_pass"] is False
    assert report["semantic"]["semantic_pass"] is False
    assert report["report_only_accepted"] is True

    evidence["report_only_accepted"] = False
    assert h.main(args) == 0


def _packaged_main_evidence(semantic_pass=True, *, max_tokens=1024):
    return {"pass": semantic_pass, "report_only_accepted": False, "runtime_contract_pass": True,
        "fixture":{"sha256":"abc", "authoritative_prompt_tokens":10},
        "runtime":{"app_identity":"token.place", "runtime_identity":"bundled",
            "build_identity":"build", "backend_requested":"cpu", "backend_selected":"cpu",
            "model_fingerprint":"sha256:model", "backend_used":"cpu"},
        "authoritative_local_progress":{"pass":True, "progress_event_count":2,
            "observed_phases":["prefill", "generating"]},
        "encrypted_progress":{"pass":True, "best_effort":True,
            "progress_event_count":1, "observed_phases":["prefill"],
            "terminal_overtook_generating_update":True},
        "response_usage":{"prompt_tokens":10, "completion_tokens":1,
            "finish_reason":"stop", "source":"validated_atomic_response_usage"},
        "atomic_response_completion":{"completed":True,
            "source":"browser_decrypted_final_response"},
        "post_terminal_silence":{"observed":True,
            "source":"pre_cancellation_primary_snapshot"},
        "metrics":{"pass":True, "preparing_duration_s":0, "prefill_duration_s":1,
            "time_to_first_token_s":1, "local_inference_duration_s":2,
            "end_to_end_request_duration_s":2, "prompt_tokens":10, "output_tokens":1,
            "prompt_tokens_per_s":10, "request_budget_s":600, "completion_margin_s":598,
            "phase_timing_source":"worker_progress_elapsed_ms",
            "inference_timing_source":"parent_inference_monotonic",
            "request_timing_source":"runner_end_to_end_monotonic",
            "completion_token_source":"validated_response_usage"},
        "semantic":{"semantic_pass":semantic_pass, "exact_match":semantic_pass,
            "errors":[] if semantic_pass else ["exact_match", "target_selection"]},
        "generation_settings":{"supplied":{"max_tokens":max_tokens},
            "omitted_runtime_default":["seed", "temperature", "top_p"]},
        "memory": _memory_evidence(),
        "runtime_configuration": _runtime_configuration(),
        "kv_compare":{"pass":True, "applicability":"not_applicable_verified_non_qwen",
            "reason":"not_applicable_verified_non_qwen",
            "attestation":{"method":"active_runtime_selected_profile",
                "applicability":"not_applicable_verified_non_qwen", "architecture":"llama",
                "profile_id":"default", "backend":"cpu", "context_tier":"64k-full",
                "context_size_tokens":65536}}}


def _packaged_main_args(tmp_path, *extra):
    return ["packaged-runtime", "--out-dir", str(tmp_path), "--app-binary", "app",
        "--model", "model", "--backend", "cpu", "--relay-url", "https://relay.example", *extra]


def test_production_shaped_qwen_report_validates_and_writes_atomically(tmp_path, monkeypatch):
    evidence = _packaged_main_evidence()
    evidence["runtime"].update(backend_requested="metal", backend_selected="metal",
        backend_used="metal")
    evidence["runtime_configuration"] = _packaged_configuration_builder()(
        _packaged_runtime_labels(), _qwen_readiness_diagnostics(), "metal")
    evidence["kv_compare"] = _qwen_kv_summary()
    monkeypatch.setattr(h, "invoke_packaged_runtime_adapter", lambda **_kwargs: evidence)
    args = ["packaged-runtime", "--out-dir", str(tmp_path), "--app-binary", "app",
        "--model", "renamed-model.gguf", "--backend", "metal",
        "--relay-url", "https://relay.example"]
    assert h.main(args) == 0
    report_path = tmp_path / "long_context_benchmark_report.json"
    report = json.loads(report_path.read_text())
    assert report["runtime_configuration"]["trials"][0]["mode"] == {
        "requested":"gpu", "effective":"metal"}
    assert report["kv_diagnostics"]["trials"][0]["type_k"] == "q8"
    h.validate_report(report)
    assert report["encrypted_progress"] == {
        "pass": True, "best_effort": True, "progress_event_count": 1,
        "observed_phases": ["prefill"],
        "terminal_overtook_generating_update": True}
    rewritten = h.write_report_atomic(tmp_path / "rewritten", report)
    assert json.loads(rewritten.read_text()) == report

    contract_mutations = (
        ("report_response_usage_invalid",
            lambda item: item["response_usage"].update(completion_tokens=0)),
        ("report_response_usage_invalid",
            lambda item: item["response_usage"].update(prompt_tokens=11)),
        ("report_atomic_response_invalid",
            lambda item: item.update(atomic_response_completion={"completed": False})),
        ("report_post_terminal_silence_invalid",
            lambda item: item.update(post_terminal_silence={"observed": False})),
        ("report_metrics_invalid",
            lambda item: item["metrics"].update(request_timing_source="unknown")),
    )
    for reason, mutate in contract_mutations:
        malformed = copy.deepcopy(report)
        mutate(malformed)
        with pytest.raises(ValueError, match=reason):
            h.validate_report(malformed)

    progress_mutations = (
        ("report_authoritative_local_progress_invalid",
            lambda item: item["authoritative_local_progress"].pop("observed_phases")),
        ("report_authoritative_local_progress_invalid",
            lambda item: item["authoritative_local_progress"].update(extra=True)),
        ("report_authoritative_local_progress_invalid",
            lambda item: item["authoritative_local_progress"].update(
                progress_event_count=True)),
        ("report_authoritative_local_progress_invalid",
            lambda item: item["authoritative_local_progress"].update(
                progress_event_count=0)),
        ("report_authoritative_local_progress_invalid",
            lambda item: item["authoritative_local_progress"].update(
                progress_event_count=-1)),
        ("report_authoritative_local_progress_invalid",
            lambda item: item["authoritative_local_progress"].update(
                observed_phases=[])),
        ("report_authoritative_local_progress_invalid",
            lambda item: item["authoritative_local_progress"].update(
                observed_phases=["prefill", "invalid", "generating"])),
        ("report_authoritative_local_progress_invalid",
            lambda item: item["authoritative_local_progress"].update(
                observed_phases=["prefill", "prefill", "generating"])),
        ("report_authoritative_local_progress_invalid",
            lambda item: item["authoritative_local_progress"].update(
                observed_phases=["generating", "prefill"])),
        ("report_authoritative_local_progress_invalid",
            lambda item: item["authoritative_local_progress"].update(
                observed_phases=["prefill"])),
        ("report_authoritative_local_progress_invalid",
            lambda item: item["authoritative_local_progress"].update(
                observed_phases=["prefill", "generating"], progress_event_count=1)),
        ("report_progress_invalid",
            lambda item: item["encrypted_progress"].pop("best_effort")),
        ("report_progress_invalid",
            lambda item: item["encrypted_progress"].pop(
                "terminal_overtook_generating_update")),
        ("report_progress_invalid",
            lambda item: item["encrypted_progress"].update(extra=True)),
        ("report_progress_invalid",
            lambda item: item["encrypted_progress"].update(best_effort=False)),
        ("report_progress_invalid",
            lambda item: item["encrypted_progress"].update(progress_event_count=True)),
        ("report_progress_invalid",
            lambda item: item["encrypted_progress"].update(progress_event_count=0)),
        ("report_progress_invalid",
            lambda item: item["encrypted_progress"].update(progress_event_count=-1)),
        ("report_progress_invalid",
            lambda item: item["encrypted_progress"].update(observed_phases=[])),
        ("report_progress_invalid",
            lambda item: item["encrypted_progress"].update(observed_phases=["invalid"])),
        ("report_progress_invalid",
            lambda item: item["encrypted_progress"].update(
                observed_phases=["prefill", "prefill"])),
        ("report_progress_invalid",
            lambda item: item["encrypted_progress"].update(
                observed_phases=["generating", "prefill"])),
        ("report_progress_invalid",
            lambda item: item["encrypted_progress"].update(
                terminal_overtook_generating_update=1)),
        ("report_progress_invalid",
            lambda item: item["encrypted_progress"].update(
                terminal_overtook_generating_update=False)),
    )
    for reason, mutate_progress in progress_mutations:
        malformed = json.loads(json.dumps(report))
        mutate_progress(malformed)
        with pytest.raises(ValueError, match=f"^{reason}$"):
            h.validate_report(malformed)

    mutations = (
        lambda item: item["runtime_configuration"]["trials"][0]["mode"].update(effective="gpu"),
        lambda item: item["runtime_configuration"]["trials"][0]["backend"].update(
            available="cpu"),
        lambda item: item["runtime_configuration"]["trials"][0]["backend"].update(
            fallback_reason="automatic_cpu_fallback"),
        lambda item: item["runtime_configuration"]["trials"][0]["runtime_profile"].update(
            fallback_reason="null"),
        lambda item: item["runtime_configuration"]["trials"][0]["runtime_profile"].update(
            selected="qwen64k_kv_q4_fa"),
        lambda item: item["runtime_configuration"]["trials"][0]["kv_cache"].update(precision="q4"),
        lambda item: item["runtime_configuration"]["trials"][0]["kv_cache"].update(type_v=2),
        lambda item: item["backend"].update(used="cuda"),
        lambda item: item["context"].update(window_tokens=65535),
        lambda item: item["runtime_configuration"]["trials"][0]["yarn_rope"].update(
            configuration_valid=False),
    )
    for mutate in mutations:
        malformed = json.loads(json.dumps(report)); mutate(malformed)
        with pytest.raises(ValueError):
            h.validate_report(malformed)


@pytest.mark.parametrize("result", ["constructed", "failed"])
def test_completed_qwen_runtime_rejects_nonfinal_profile_results(result):
    configuration = _packaged_configuration_builder()(
        _packaged_runtime_labels(), _qwen_readiness_diagnostics(result=result), "metal")
    with pytest.raises(ValueError, match="runtime_configuration_invalid"):
        h.validate_runtime_configuration(configuration, backend="metal",
            context_tier="64k-full", context_tokens=65536,
            kv_attestation=_qwen_kv_summary())


@pytest.mark.parametrize(("architecture", "tier", "window", "reason"), [
    ("llama", "64k-full", 65536, "not_applicable_verified_non_qwen"),
    ("qwen3", "8k-fast", 8192, "not_applicable_context_tier"),
])
def test_rendered_null_profile_diagnostics_validate_end_to_end(tmp_path, monkeypatch,
        architecture, tier, window, reason):
    diagnostics = {key: "null" for key in _qwen_readiness_diagnostics()}
    configuration = _packaged_configuration_builder()(
        _packaged_runtime_labels("cpu", tier=tier, window=window), diagnostics, "cpu")
    not_applicable = {"status":"not_applicable", "reason":"not_qwen_64k_profile"}
    assert all(configuration[key] == not_applicable for key in (
        "runtime_profile", "batch_profile", "kv_cache", "acceleration", "yarn_rope"))
    evidence = _packaged_main_evidence()
    evidence["runtime_configuration"] = configuration
    evidence["kv_compare"] = {"pass":True, "applicability":reason, "reason":reason,
        "attestation":{"method":"active_runtime_selected_profile", "applicability":reason,
            "architecture":architecture, "profile_id":"default", "backend":"cpu",
            "context_tier":tier, "context_size_tokens":window}}
    monkeypatch.setattr(h, "invoke_packaged_runtime_adapter", lambda **_kwargs: evidence)
    args = _packaged_main_args(tmp_path)
    if tier == "8k-fast":
        args.extend(["--context-tier", tier])
    assert h.main(args) == 0
    report_path = tmp_path / "long_context_benchmark_report.json"
    report = json.loads(report_path.read_text())
    h.validate_report(report)
    rewritten = h.write_report_atomic(tmp_path / "rewritten", report)
    assert json.loads(rewritten.read_text()) == report


def test_packaged_trials_default_and_multiple_are_sequential(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(h, "invoke_packaged_runtime_adapter",
        lambda **kwargs: calls.append(len(calls)) or _packaged_main_evidence())
    assert h.main(_packaged_main_args(tmp_path)) == 0
    assert calls == [0]
    assert h.main(_packaged_main_args(tmp_path, "--trials", "3")) == 0
    assert calls == [0, 1, 2, 3]
    report = json.loads((tmp_path / "long_context_benchmark_report.json").read_text())
    assert report["requested_trial_count"] == report["completed_trial_count"] == 3
    assert report["aggregate_semantic"]["trial_count"] == 3


def test_not_run_timeout_report_retains_validated_fixture_sha_and_safe_diagnostics(tmp_path, monkeypatch):
    _, manifest = h.generate_fixture("small-8k", scenario="single-needle")
    timeout = {"pass": False, "runtime_contract_pass": False,
        "code": "packaged_runner_timeout", "last_safe_phase": "request_active",
        "request_timeout_s": 600.0, "setup_timeout_s": h.PACKAGED_SETUP_BUDGET_S,
        "finalization_timeout_s": h.PACKAGED_FINALIZATION_BUDGET_S,
        "cancellation_timeout_s": 0.0,
        "cleanup_timeout_s": 30.0,
        "runner_timeout_s": h.PACKAGED_SETUP_BUDGET_S + 600 + h.PACKAGED_FINALIZATION_BUDGET_S,
        "overall_timeout_s": h.PACKAGED_SETUP_BUDGET_S + 600
            + h.PACKAGED_FINALIZATION_BUDGET_S + 30,
        "elapsed_s": 700.0, "cleanup_succeeded": True}
    monkeypatch.setattr(h, "invoke_packaged_runtime_adapter", lambda **_kwargs: timeout)
    assert h.main(_packaged_main_args(tmp_path, "--fixture", "small-8k", "--scenario",
        "single-needle", "--request-timeout", "600", "--cleanup-timeout", "30",
        "--report-only")) == 1
    report = json.loads((tmp_path / "long_context_benchmark_report.json").read_text())
    assert report["fixture"]["sha256"] == manifest["fixture_sha256"]
    assert report["completed_trial_count"] == 0
    assert report["last_safe_phase"] == "request_active"
    assert not h.SENSITIVE_KEYS.intersection(report)


def test_not_run_runner_failure_retains_only_allowlisted_safe_diagnostics(tmp_path, monkeypatch):
    _, manifest = h.generate_fixture("small-8k", scenario="single-needle")
    failure = {"pass": False, "runtime_contract_pass": False,
        "code": "packaged_runner_failed", "last_safe_phase": "operator_ready",
        "failure_reason": "client_keypair_not_ready", "elapsed_s": 299.0,
        "cleanup_succeeded": True, "prompt": "secret fixture prompt",
        "traceback": "Traceback: secret", "diagnostic_tail": "private log text",
        "path": "/private/model.gguf", "request_id": "request-secret",
        "client_id": "client-secret", "session_id": "session-secret"}
    monkeypatch.setattr(h, "invoke_packaged_runtime_adapter", lambda **_kwargs: failure)
    assert h.main(_packaged_main_args(tmp_path, "--fixture", "small-8k", "--scenario",
        "single-needle", "--report-only")) == 1
    report = json.loads((tmp_path / "long_context_benchmark_report.json").read_text())
    h.validate_report(report)
    assert report["fixture"]["sha256"] == manifest["fixture_sha256"]
    assert report["completed_trial_count"] == 0
    assert report["failure_reason"] in h.PACKAGED_FAILURE_REASONS
    prohibited = {"prompt", "response_text", "diagnostic_tail", "traceback", "path",
        "request_id", "client_id", "session_id", "ciphertext", "key"}
    assert not prohibited.intersection(report)
    assert report["last_safe_phase"] == "operator_ready"
    assert report["elapsed_s"] == 299.0
    assert report["cleanup_succeeded"] is True


@pytest.mark.parametrize("timeout_field", [
    "request_timeout_s", "setup_timeout_s", "runner_timeout_s", "overall_timeout_s",
])
def test_runner_failure_report_rejects_timeout_budget_fields(timeout_field):
    report = {"schema_version": h.SCHEMA_VERSION, "mode": "packaged-runtime",
        "status": "not_run", "fixture": {"id": "small-8k", "version": h.FIXTURE_VERSION,
            "scenario": "single-needle", "sha256": "unavailable"},
        "code": "packaged_runner_failed", "last_safe_phase": "operator_ready",
        "failure_reason": "client_keypair_not_ready", "elapsed_s": 1.0,
        "cleanup_succeeded": True, timeout_field: 600.0}
    with pytest.raises(ValueError, match="report_runner_failure_diagnostics_invalid"):
        h.validate_report(report)


@pytest.mark.parametrize(("field", "value"), [
    ("failure_reason", []), ("failure_reason", {}),
    ("cleanup_succeeded", []), ("cleanup_succeeded", {}),
    ("elapsed_s", []), ("last_safe_phase", {}),
])
def test_runner_failure_report_rejects_malformed_diagnostic_types(field, value):
    report = {"schema_version": h.SCHEMA_VERSION, "mode": "packaged-runtime",
        "status": "not_run", "fixture": {"id": "small-8k", "version": h.FIXTURE_VERSION,
            "scenario": "single-needle", "sha256": "unavailable"},
        "code": "packaged_runner_failed", "last_safe_phase": "operator_ready",
        "failure_reason": "client_keypair_not_ready", "elapsed_s": 1.0,
        "cleanup_succeeded": True}
    report[field] = value
    with pytest.raises(ValueError, match="report_runner_failure_diagnostics_invalid"):
        h.validate_report(report)


def test_not_run_invalid_external_manifest_uses_safe_fixture_sha(tmp_path, monkeypatch):
    prompt = tmp_path / "prompt.txt"
    manifest = tmp_path / "manifest.json"
    prompt.write_text("external prompt", encoding="utf-8")
    manifest.write_text(json.dumps({"fixture_id": "small-8k"}), encoding="utf-8")
    monkeypatch.setattr(h, "invoke_packaged_runtime_adapter", lambda **_kwargs: {
        "pass": False, "runtime_contract_pass": False,
        "code": "manifest_missing_fields"})

    assert h.main(_packaged_main_args(tmp_path, "--prompt", str(prompt),
        "--manifest", str(manifest))) == 1
    report = json.loads((tmp_path / "long_context_benchmark_report.json").read_text())
    assert report["status"] == "not_run"
    assert report["code"] == "manifest_missing_fields"
    assert report["fixture"]["sha256"] == "unavailable"


@pytest.mark.parametrize("manifest_value", ["not-json", "[]"])
def test_not_run_malformed_external_manifest_is_categorical(tmp_path, manifest_value, monkeypatch):
    prompt = tmp_path / "prompt.txt"; prompt.write_text("external prompt", encoding="utf-8")
    manifest = tmp_path / "manifest.json"; manifest.write_text(manifest_value, encoding="utf-8")
    monkeypatch.setattr(h, "invoke_packaged_runtime_adapter", lambda **_kwargs: {
        "pass": False, "runtime_contract_pass": False, "code": "manifest_not_object"})
    assert h.main(_packaged_main_args(tmp_path, "--prompt", str(prompt),
        "--manifest", str(manifest), "--report-only")) == 1
    report = json.loads((tmp_path / "long_context_benchmark_report.json").read_text())
    assert report["status"] == "not_run"
    assert report["code"] == "manifest_not_object"
    assert report["fixture"]["sha256"] == "unavailable"


def test_cancellation_sequence_runs_once_outside_semantic_trial_count(tmp_path, monkeypatch):
    calls = []
    def fake_invoke(**kwargs):
        calls.append(kwargs["cancellation_validation"])
        result = _packaged_main_evidence()
        if kwargs["cancellation_validation"]:
            result["cancellation_recovery"] = {
                **_physical_cancellation_evidence(), "pass": True}
        return result
    monkeypatch.setattr(h, "invoke_packaged_runtime_adapter", fake_invoke)
    args = _packaged_main_args(tmp_path, "--trials", "3", "--cancellation-validation",
        "--prefill-cancel-tokens", "50", "--generation-cancel-tokens", "8")
    assert h.main(args) == 0
    assert calls == [True, False, False]
    report = json.loads((tmp_path / "long_context_benchmark_report.json").read_text())
    assert report["aggregate_semantic"]["trial_count"] == 3
    assert report["cancellation_recovery"]["pass"] is True


@pytest.mark.parametrize("extra", [
    ("--cancellation-validation",),
    ("--cancellation-validation", "--prefill-cancel-tokens", "0"),
    ("--cancellation-validation", "--prefill-cancel-fraction", "1"),
    ("--cancellation-validation", "--prefill-cancel-tokens", "1",
        "--generation-cancel-tokens", "0"),
])
def test_cancellation_cli_configuration_is_bounded(tmp_path, extra):
    with pytest.raises(SystemExit, match="2"):
        h.main(_packaged_main_args(tmp_path, *extra))


def test_packaged_trials_aggregate_mixed_semantics_and_report_only(tmp_path, monkeypatch):
    outcomes = iter([True, False, True])
    monkeypatch.setattr(h, "invoke_packaged_runtime_adapter",
        lambda **kwargs: _packaged_main_evidence(next(outcomes)))
    assert h.main(_packaged_main_args(tmp_path, "--trials", "3", "--report-only")) == 0
    report = json.loads((tmp_path / "long_context_benchmark_report.json").read_text())
    aggregate = report["aggregate_semantic"]
    assert report["overall_pass"] is False and report["report_only_accepted"] is True
    assert aggregate["exact_match_count"] == 2 and aggregate["pass_rate"] == pytest.approx(2 / 3)
    assert aggregate["failure_categories"] == {"exact_match": 1, "target_selection": 1}
    serialized = json.dumps(report).lower()
    assert all(word not in serialized for word in ("response_text", "messages", "ciphertext", "request_id"))


def test_packaged_trials_fail_closed_on_runtime_failure_and_settings_drift(tmp_path, monkeypatch):
    runtime_failure = {"pass":False, "runtime_contract_pass":False, "code":"telemetry_failed"}
    outcomes = iter([_packaged_main_evidence(), runtime_failure, _packaged_main_evidence()])
    monkeypatch.setattr(h, "invoke_packaged_runtime_adapter", lambda **kwargs: next(outcomes))
    assert h.main(_packaged_main_args(tmp_path, "--trials", "3", "--report-only")) == 1
    report = json.loads((tmp_path / "long_context_benchmark_report.json").read_text())
    assert report["requested_trial_count"] == 3 and report["completed_trial_count"] == 1
    assert report["code"] == "telemetry_failed"

    outcomes = iter([_packaged_main_evidence(), _packaged_main_evidence(max_tokens=512)])
    monkeypatch.setattr(h, "invoke_packaged_runtime_adapter", lambda **kwargs: next(outcomes))
    assert h.main(_packaged_main_args(tmp_path, "--trials", "2", "--report-only")) == 1
    assert json.loads((tmp_path / "long_context_benchmark_report.json").read_text())["code"] == "generation_settings_inconsistent"


@pytest.mark.parametrize("trials", ["0", "-1", str(h.MAX_PACKAGED_TRIALS + 1)])
def test_packaged_trials_argument_is_bounded(tmp_path, trials):
    with pytest.raises(SystemExit, match="2"):
        h.main(_packaged_main_args(tmp_path, "--trials", trials))


@pytest.mark.parametrize(("settings", "code"), [
    (None, "generation_settings_malformed"),
    ({"supplied":{}, "omitted_runtime_default":[]}, "generation_settings_malformed"),
    ({"supplied":{"temperature":1}, "omitted_runtime_default":["max_tokens", "seed", "top_p"]},
     "generation_settings_unsupported"),
    ({"supplied":{"max_tokens":float("nan")}, "omitted_runtime_default":["seed", "temperature", "top_p"]},
     "generation_settings_value_invalid"),
    ({"supplied":{"max_tokens":1024, "unknown":1}, "omitted_runtime_default":["seed", "temperature", "top_p"]},
     "generation_settings_unsupported"),
    ({"supplied":{"max_tokens":1024}, "omitted_runtime_default":[]},
     "generation_settings_omissions_invalid"),
])
def test_generation_settings_validation_fails_closed(settings, code):
    with pytest.raises(ValueError, match=code):
        h.validate_generation_settings(settings)
