#!/usr/bin/env python3
"""Desktop app e2e: relay + operator start + prompt inference through real Tauri UI."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


REPO_ROOT = Path(__file__).resolve().parents[2]
DESKTOP_ROOT = REPO_ROOT / "desktop-tauri"


def reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_livez(relay: subprocess.Popen[str], port: int, timeout_seconds: float = 25.0) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{port}/livez", timeout=1) as resp:  # noqa: S310
                if resp.status == 200:
                    return
        except Exception:
            pass

        if relay.poll() is not None:
            raise RuntimeError(
                f"relay exited early with code {relay.returncode}; stderr={relay.stderr.read() if relay.stderr else ''}"
            )
        time.sleep(0.25)

    raise RuntimeError(f"relay did not become live on port {port}")


def desktop_binary_path() -> Path:
    candidates = [
        DESKTOP_ROOT / "src-tauri" / "target" / "release" / "token-place-desktop-tauri",
        DESKTOP_ROOT / "src-tauri" / "target" / "release" / "token.place desktop",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"desktop binary not found; checked: {candidates}")


def wait_for_strong_text(driver: WebDriver, xpath: str, timeout_seconds: float = 30.0) -> None:
    WebDriverWait(driver, timeout_seconds).until(
        EC.presence_of_element_located((By.XPATH, xpath))
    )


def main() -> int:
    relay_port = reserve_free_port()
    relay_url = f"http://127.0.0.1:{relay_port}"

    mock_model_path = REPO_ROOT / "desktop-tauri" / "tmp.mock.gguf"
    mock_model_path.write_text("mock")

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
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    tauri_driver = subprocess.Popen(  # noqa: S603
        ["tauri-driver", "--port", "4444"],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    driver: WebDriver | None = None
    try:
        wait_for_livez(relay, relay_port)
        caps = {
            "browserName": "wry",
            "tauri:options": {
                "application": str(desktop_binary_path()),
            },
        }
        driver = webdriver.Remote(command_executor="http://127.0.0.1:4444", desired_capabilities=caps)

        model_input = WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.XPATH, "(//label[contains(., 'Model GGUF path')]/following::input)[1]"))
        )
        model_input.clear()
        model_input.send_keys(str(mock_model_path))

        relay_input = driver.find_element(
            By.XPATH,
            "(//label[contains(., 'Relay URL')]/following::input)[1]",
        )
        relay_input.clear()
        relay_input.send_keys(relay_url)

        driver.find_element(By.XPATH, "//button[normalize-space()='Start operator']").click()
        wait_for_strong_text(driver, "//p[contains(., 'Running:')]/strong[normalize-space()='yes']")
        wait_for_strong_text(driver, "//p[contains(., 'Registered:')]/strong[normalize-space()='yes']")

        prompt = driver.find_element(By.XPATH, "//label[contains(., 'Prompt')]/following::textarea[1]")
        prompt.clear()
        prompt.send_keys("Say hello from desktop e2e")

        driver.find_element(By.XPATH, "//button[normalize-space()='Start local inference']").click()
        WebDriverWait(driver, 45).until(
            lambda drv: len(drv.find_element(By.XPATH, "//main/pre").text.strip()) > 0
        )

        last_error = driver.find_element(By.XPATH, "//p[contains(., 'Last error:')]/code").text
        if "No module named 'utils'" in last_error or "bridge failure" in last_error:
            raise RuntimeError(f"desktop app reported bridge failure in Last error: {last_error}")

    except TimeoutException as exc:
        raise RuntimeError("desktop operator e2e timed out waiting for expected UI state") from exc
    finally:
        if driver is not None:
            driver.quit()
        if tauri_driver.poll() is None:
            tauri_driver.kill()
        if relay.poll() is None:
            relay.kill()
        if mock_model_path.exists():
            mock_model_path.unlink()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
