#!/usr/bin/env python3
"""Desktop UI end-to-end test: relay + Tauri app + operator + inference."""

from __future__ import annotations

import contextlib
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    NoSuchFrameException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait


REPO_ROOT = Path(__file__).resolve().parents[2]
DESKTOP_ROOT = REPO_ROOT / "desktop-tauri"
TAURI_ROOT = DESKTOP_ROOT / "src-tauri"
WEBDRIVER_URL = "http://127.0.0.1:4444"


def reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_http_200(url: str, timeout_seconds: float = 30.0) -> None:
    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urlopen(url, timeout=2) as response:  # noqa: S310
                if response.status == 200:
                    return
        except Exception as exc:  # pragma: no cover
            last_error = exc
        time.sleep(0.25)
    raise RuntimeError(f"timeout waiting for {url}: {last_error}")


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


def fill_input_by_label(driver: webdriver.Remote, label_text: str, value: str) -> None:
    locator = (
        f"(//label[normalize-space()='{label_text}']/following::input[1] | "
        f"//label[normalize-space()='{label_text}']/following::textarea[1])[1]"
    )

    def _set_value(_: webdriver.Remote) -> bool:
        try:
            with contextlib.suppress(WebDriverException):
                driver.switch_to.default_content()
            input_el = driver.find_element(By.XPATH, locator)
            input_el.send_keys(Keys.CONTROL, "a")
            input_el.send_keys(Keys.DELETE)
            input_el.send_keys(value)
            return True
        except (
            NoSuchElementException,
            NoSuchFrameException,
            StaleElementReferenceException,
            WebDriverException,
        ):
            return False

    if not WebDriverWait(driver, 45, poll_frequency=0.25).until(_set_value):
        raise RuntimeError(f"failed to set input for label: {label_text}")


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
            if d.find_elements(By.XPATH, "//label[normalize-space()='Model GGUF path']"):
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


def start_driver(app_binary: Path) -> webdriver.Remote:
    options = webdriver.ChromeOptions()
    options.set_capability("browserName", "wry")
    options.set_capability(
        "tauri:options",
        {
            "application": str(app_binary),
            "args": [],
        },
    )
    return webdriver.Remote(command_executor=WEBDRIVER_URL, options=options)


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


def main() -> int:
    relay_port = reserve_free_port()
    relay_url = f"http://127.0.0.1:{relay_port}"

    logs_dir = REPO_ROOT / ".desktop-e2e-logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    relay_log = logs_dir / "relay.log"
    driver_log = logs_dir / "tauri-driver.log"

    env = os.environ.copy()
    env["USE_MOCK_LLM"] = "1"

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
        stdout=relay_log.open("w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
        text=True,
    )

    tauri_driver = subprocess.Popen(  # noqa: S603
        tauri_driver_command(),
        cwd=DESKTOP_ROOT,
        env=env,
        stdout=driver_log.open("w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
        text=True,
    )

    driver: webdriver.Remote | None = None
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
        app_binary = TAURI_ROOT / "target" / "debug" / f"token-place-desktop-tauri{suffix}"
        if not app_binary.exists():
            raise RuntimeError(f"missing desktop binary: {app_binary}")

        driver = start_driver(app_binary)
        wait = WebDriverWait(driver, 45)
        wait_for_ui_ready(driver)

        fill_input_by_label(driver, "Model GGUF path", "mock.gguf")
        fill_input_by_label(driver, "Relay URL", relay_url)

        wait.until(lambda d: d.find_element(By.XPATH, "//button[.='Start operator']").is_enabled())
        driver.find_element(By.XPATH, "//button[.='Start operator']").click()

        wait.until(
            lambda d: d.find_element(
                By.XPATH,
                "//p[contains(.,'Running:')]//strong[normalize-space()='yes']",
            )
        )
        wait.until(
            lambda d: d.find_element(
                By.XPATH,
                "//p[contains(.,'Registered:')]//strong[normalize-space()='yes']",
            )
        )

        prompt = driver.find_element(
            By.XPATH,
            "//label[normalize-space()='Prompt']/following-sibling::textarea[1]",
        )
        prompt.send_keys("say hello from mock")
        wait.until(
            lambda d: d.find_element(By.XPATH, "//button[.='Start local inference']").is_enabled()
        )
        driver.find_element(By.XPATH, "//button[.='Start local inference']").click()

        wait.until(lambda d: d.find_element(By.XPATH, "//pre").text.strip() != "")
        output_text = driver.find_element(By.XPATH, "//pre").text.strip()
        assert output_text, "inference output is empty"

        last_error_text = driver.find_element(By.XPATH, "//p[contains(.,'Last error:')]").text
        lowered_last_error = last_error_text.lower()
        assert "bridge failure" not in lowered_last_error, (
            f"Last error still indicates bridge failure: {last_error_text}"
        )
        assert "no module named" not in lowered_last_error, (
            f"Last error still indicates import failure: {last_error_text}"
        )
        assert "importerror" not in lowered_last_error, (
            f"Last error still indicates import failure: {last_error_text}"
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
        if driver is not None:
            driver.quit()
        terminate_process(tauri_driver)
        terminate_process(relay)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
