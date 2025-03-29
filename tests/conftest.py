"""
Pytest configuration for token.place tests.
Contains fixtures and configuration for cross-platform testing.
"""

import os
import sys
import pytest
import platform
import tempfile
import shutil
import subprocess
import time
import signal
import requests
from pathlib import Path
from typing import Dict, Any, Generator, List, Optional, Tuple
from playwright.sync_api import Page, sync_playwright, Browser, BrowserContext

# Add the project root to the path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import our path handling utilities
from utils.path_handling import (
    IS_WINDOWS, IS_MACOS, IS_LINUX, 
    ensure_dir_exists, normalize_path
)

# Import config
from config import Config, get_config

@pytest.fixture(scope="session")
def platform_info() -> Dict[str, Any]:
    """Fixture providing platform information"""
    return {
        "system": platform.system().lower(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "is_windows": IS_WINDOWS,
        "is_macos": IS_MACOS,
        "is_linux": IS_LINUX,
    }

@pytest.fixture(scope="session")
def temp_data_dir() -> Generator[Path, None, None]:
    """
    Fixture providing a temporary directory for test data.
    The directory is cleaned up after all tests are run.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        data_dir = Path(temp_dir) / "token_place_test_data"
        ensure_dir_exists(data_dir)
        yield data_dir

@pytest.fixture(scope="session")
def test_config(temp_data_dir: Path) -> Config:
    """
    Fixture providing a test configuration.
    Uses a temporary directory for data and sets the environment to testing.
    """
    os.environ["TOKEN_PLACE_ENV"] = "testing"
    
    # Create a test config file
    config_path = temp_data_dir / "test_config.json"
    config_content = {
        "paths": {
            "data_dir": str(temp_data_dir),
            "models_dir": str(temp_data_dir / "models"),
            "logs_dir": str(temp_data_dir / "logs"),
            "cache_dir": str(temp_data_dir / "cache"),
            "keys_dir": str(temp_data_dir / "keys"),
        }
    }
    
    # Create all the directories
    for dir_path in config_content["paths"].values():
        ensure_dir_exists(dir_path)
    
    # Create and initialize the config
    config = Config(env="testing")
    
    # Override paths with temp directories
    for key, path in config_content["paths"].items():
        config.set(f"paths.{key}", path)
    
    return config

@pytest.fixture(scope="function")
def temp_file() -> Generator[Path, None, None]:
    """
    Fixture providing a temporary file.
    The file is cleaned up after the test.
    """
    with tempfile.NamedTemporaryFile(delete=False) as temp_f:
        temp_path = Path(temp_f.name)
        yield temp_path
        
        # Clean up after the test
        if temp_path.exists():
            temp_path.unlink()

@pytest.fixture(scope="function")
def temp_dir() -> Generator[Path, None, None]:
    """
    Fixture providing a temporary directory.
    The directory is cleaned up after the test.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        yield temp_path

@pytest.fixture(scope="session", autouse=True)
def configure_test_environment():
    """
    Configure the test environment before running tests.
    This fixture runs automatically before any tests.
    """
    # Set environment variables for testing
    os.environ["TOKEN_PLACE_ENV"] = "testing"
    
    # Detect platform
    platform_name = platform.system().lower()
    os.environ["PLATFORM"] = platform_name
    
    yield
    
    # Clean up after all tests
    # (Nothing to do here, temp directories and files are cleaned up by their fixtures)

# Helper function to print console messages
def print_console_message(msg):
    """Callback function to print console messages."""
    # Avoid printing the noisy [Server Poll] messages coming from server.py
    if "[Server Poll]" not in msg.text:
        print(f"Browser Console [{msg.type}]: {msg.text}")

# CONSTANTS USED BY THE TEST FIXTURES
E2E_SERVER_PORT = 8010
E2E_RELAY_PORT = 5010
E2E_BASE_URL = f"http://localhost:{E2E_RELAY_PORT}"

@pytest.fixture(scope="module")
def setup_servers() -> Generator[Tuple[subprocess.Popen, subprocess.Popen], None, None]:
    """
    Start the server and relay processes for end-to-end testing.
    
    This fixture:
    1. Starts the relay on port 5010 with --use_mock_llm flag
    2. Starts the server on port 8010 (with USE_MOCK_LLM=1 environment variable)
    3. Waits for both to be ready and the server to register with the relay
    4. Yields the processes
    5. Cleans up the processes after tests
    """
    print("\nSetting up servers for E2E tests...")
    
    # Ensure environment variables are set properly
    test_env = os.environ.copy()
    test_env["TOKEN_PLACE_ENV"] = "testing"
    test_env["USE_MOCK_LLM"] = "1"  # This is the key setting for mocking the LLM
    
    # Start the relay server with the --use_mock_llm flag
    relay_process = subprocess.Popen(
        [sys.executable, "relay.py", "--port", str(E2E_RELAY_PORT), "--use_mock_llm"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=test_env
    )
    print(f"Started relay server on port {E2E_RELAY_PORT} with --use_mock_llm flag")
    
    # Wait for relay to start - increased wait time
    time.sleep(3)
    
    # Check if relay is running
    relay_ready = False
    for _ in range(15):  # Try for 15 seconds
        try:
            response = requests.get(f"{E2E_BASE_URL}/")
            if response.status_code == 200:
                relay_ready = True
                print("✓ Relay server is running")
                break
        except requests.RequestException:
            time.sleep(1)
    
    if not relay_ready:
        print("✗ Relay server failed to start")
        relay_process.terminate()
        # Print relay output for debugging
        stdout, stderr = relay_process.communicate(timeout=1)
        print(f"Relay stdout: {stdout}")
        print(f"Relay stderr: {stderr}")
        pytest.skip("Relay server failed to start")
    
    # Start the server with mock LLM enabled via environment variable
    # Note: server.py doesn't accept --use_mock_llm flag, it uses the environment variable
    server_process = subprocess.Popen(
        [sys.executable, "server.py", "--server_port", str(E2E_SERVER_PORT), 
         "--relay_port", str(E2E_RELAY_PORT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=test_env
    )
    print(f"Started server on port {E2E_SERVER_PORT} with USE_MOCK_LLM=1 (via environment variable)")
    
    # Wait longer for server to start and register with relay
    time.sleep(5)
    print("Waiting for server to register with relay...")
    server_registered = False
    for _ in range(30):  # 30 seconds timeout
        try:
            response = requests.get(f"{E2E_BASE_URL}/next_server")
            if response.status_code == 200 and response.json().get('server_public_key'):
                server_registered = True
                print("✓ Server registered with relay")
                break
        except requests.RequestException:
            pass
        except KeyError:
            # If we get a response but it has an 'error' key, not 'server_public_key'
            if response.status_code == 200:
                data = response.json()
                if 'error' in data:
                    print(f"Error from relay: {data['error'].get('message', 'Unknown error')}")
            time.sleep(1)
        time.sleep(1)
    
    if not server_registered:
        print("✗ Server failed to register with relay")
        # Print server output for debugging
        stdout, stderr = server_process.communicate(timeout=1)
        print(f"Server stdout: {stdout}")
        print(f"Server stderr: {stderr}")
        relay_process.terminate()
        server_process.terminate()
        pytest.skip("Server failed to register with relay")
    
    # Additional wait after server registration
    time.sleep(3)
    print("Server and relay are ready for tests")
    
    # Return the processes
    yield relay_process, server_process
    
    # Cleanup
    print("\nCleaning up E2E test servers...")
    server_process.terminate()
    relay_process.terminate()
    
    # Wait for processes to terminate gracefully or force kill
    for proc, name in [(server_process, "Server"), (relay_process, "Relay")]:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            print(f"{name} did not terminate gracefully, forcing...")
            if IS_WINDOWS:
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)], check=False)
            else:
                proc.kill()
    
    print("Server and relay processes terminated")

@pytest.fixture(scope="module")
def browser_context(setup_servers) -> Generator[Tuple[Browser, BrowserContext], None, None]:
    """
    Create a browser context for Playwright tests.
    
    This fixture:
    1. Uses the setup_servers fixture to ensure servers are running
    2. Creates a browser and context
    3. Yields the browser and context
    4. Cleans up after tests
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        
        # Add console log handler
        context.on("console", print_console_message)
        
        yield browser, context
        
        # Cleanup
        context.close()
        browser.close()

@pytest.fixture(scope="function")
def page(browser_context) -> Generator[Page, None, None]:
    """
    Create a page for Playwright tests.
    
    This fixture:
    1. Uses the browser_context fixture to get the browser and context
    2. Creates a new page
    3. Yields the page
    4. Cleans up after the test
    """
    _, context = browser_context
    page = context.new_page()
    yield page
    page.close()

# Provide base_url as a fixture
@pytest.fixture(scope="session")
def base_url() -> str:
    """Return the base URL for E2E tests"""
    return E2E_BASE_URL 