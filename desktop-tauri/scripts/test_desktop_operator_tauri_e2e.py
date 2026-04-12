#!/usr/bin/env python3
"""WebDriver e2e for the real Tauri desktop operator journey."""

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
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as ec
from selenium.webdriver.support.ui import WebDriverWait


REPO_ROOT = Path(__file__).resolve().parents[2]
DESKTOP_ROOT = REPO_ROOT / "desktop-tauri"
APP_BINARY = DESKTOP_ROOT / "src-tauri" / "target" / "release" / "token-place-desktop-tauri"


def reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_livez(relay: subprocess.Popen[str], port: int, timeout_seconds: float = 30.0) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{port}/livez", timeout=1) as resp:  # noqa: S310
                if resp.status == 200:
                    return
        except Exception:
            pass

        if relay.poll() is not None:
            raise RuntimeError(f"relay exited early with code {relay.returncode}")
        time.sleep(0.25)

    raise RuntimeError(f"relay did not become live on port {port}")


def wait_for_text(driver: webdriver.Remote, text: str, timeout: float = 30.0) -> None:
    WebDriverWait(driver, timeout).until(
        ec.text_to_be_present_in_element((By.TAG_NAME, "body"), text)
    )


def fill_input_for_label(driver: webdriver.Remote, label_text: str, value: str) -> None:
    label = driver.find_element(By.XPATH, f"//label[normalize-space()='{label_text}']")
    field = label.find_element(By.XPATH, "following-sibling::input[1]")
    field.send_keys(Keys.CONTROL, "a")
    field.send_keys(Keys.DELETE)
    field.send_keys(value)


def main() -> int:
    if not APP_BINARY.is_file():
        raise FileNotFoundError(f"built app binary not found at {APP_BINARY}")

    relay_port = reserve_free_port()
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
        [str(Path.home() / ".cargo" / "bin" / "tauri-driver")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    driver: webdriver.Remote | None = None
    try:
        wait_for_livez(relay, relay_port)
        time.sleep(1)

        options = webdriver.ChromeOptions()
        options.set_capability("browserName", "wry")
        options.set_capability("tauri:options", {"application": str(APP_BINARY)})
        driver = webdriver.Remote(command_executor="http://127.0.0.1:4444", options=options)

        wait_for_text(driver, "token.place desktop compute node")
        fill_input_for_label(driver, "Model GGUF path", "mock.gguf")
        fill_input_for_label(driver, "Relay URL", f"http://127.0.0.1:{relay_port}")

        driver.find_element(By.XPATH, "//button[normalize-space()='Start operator']").click()
        wait_for_text(driver, "Running: yes", timeout=45)
        wait_for_text(driver, "Registered: yes", timeout=45)

        prompt = driver.find_element(By.XPATH, "//label[normalize-space()='Prompt']/following-sibling::textarea[1]")
        prompt.send_keys("Say hello from desktop e2e")

        driver.find_element(By.XPATH, "//button[normalize-space()='Start local inference']").click()

        WebDriverWait(driver, 60).until(
            lambda d: len(d.find_element(By.XPATH, "//main/pre").text.strip()) > 0
        )

        body_text = driver.find_element(By.TAG_NAME, "body").text
        if "Last error: bridge failure" in body_text or "No module named 'utils'" in body_text:
            raise AssertionError(f"desktop app reported bridge error:\n{body_text}")

    except TimeoutException as exc:
        raise RuntimeError("desktop e2e timed out waiting for UI state") from exc
    finally:
        if driver is not None:
            driver.quit()

        if tauri_driver.poll() is None:
            tauri_driver.kill()

        if relay.poll() is None:
            relay.kill()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
