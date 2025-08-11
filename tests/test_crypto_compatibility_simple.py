import pytest
import base64
import json
import sys
import os
from pathlib import Path
import subprocess
import shutil

# Add the project root to the path for imports
project_root = str(Path(__file__).parent.parent)
sys.path.insert(0, project_root)

from encrypt import generate_keys, encrypt, decrypt
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('crypto_tests')

def find_executable(name):
    """Find the path to an executable"""
    path = shutil.which(name)
    if path:
        logger.info(f"Found {name} at: {path}")
        return path
    else:
        logger.error(f"Could not find {name} in PATH")
        return None

def run_js_test():
    """
    Simple test that runs the JavaScript crypto tests directly
    """
    logger.info("Starting JavaScript crypto tests")

    # Find node executable
    node_path = find_executable('node')
    if not node_path:
        logger.error("Node.js not found. Please ensure Node.js is installed and in your PATH.")
        return False

    # Find the JavaScript test file
    js_test_path = os.path.join(project_root, 'tests', 'test_js_crypto.js')
    if not os.path.exists(js_test_path):
        logger.error(f"JavaScript test file not found at {js_test_path}")
        return False

    try:
        # Run the Node.js script directly
        logger.info(f"Running JavaScript test: {js_test_path}")
        result = subprocess.run([node_path, js_test_path],
                               capture_output=True,
                               text=True,
                               check=True,
                               cwd=project_root)
        js_output = result.stdout
        logger.info("JavaScript tests completed successfully")
        print("JavaScript crypto tests: Success!")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"JavaScript test error: {e.stderr}")
        print(f"JavaScript test error: {e.stderr}")
        return False

def run_python_tests():
    """
    Simple test that runs the Python crypto tests directly
    """
    logger.info("Starting Python crypto tests")

    # Find Python executable (use the same one running this script)
    python_path = sys.executable
    logger.info(f"Using Python at: {python_path}")

    try:
        # Check if test_encrypt.py exists, if not try unit tests
        encrypt_test_path = os.path.join(project_root, 'tests', 'test_encrypt.py')
        if not os.path.exists(encrypt_test_path):
            logger.info("test_encrypt.py not found, trying unit tests instead")
            result = subprocess.run([python_path, '-m', 'pytest', 'tests/unit/test_crypto_manager.py', '-v'],
                                  capture_output=True,
                                  text=True,
                                  check=True,
                                  cwd=project_root)
        else:
            # Run Python tests for encryption
            logger.info("Running Python encrypt tests")
            result = subprocess.run([python_path, '-m', 'pytest', 'tests/test_encrypt.py', '-v'],
                                  capture_output=True,
                                  text=True,
                                  check=True,
                                  cwd=project_root)

        py_output = result.stdout
        logger.info("Python tests completed successfully")
        print("Python encrypt tests: Success!")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Python test error: {e.stderr}")
        print(f"Python test error: {e.stderr}")
        return False

def manual_crypto_compatibility_test():
    """Provide sanitized instructions without exposing sensitive data."""
    print("Manual crypto compatibility test omitted to avoid logging secrets.")

def check_environment():
    """
    Check the environment for required components
    """
    print("\n=== Environment Check ===\n")

    # Check Python
    print(f"Python version: {sys.version}")

    # Check Node.js
    node_path = find_executable('node')
    if node_path:
        try:
            result = subprocess.run([node_path, '--version'],
                                   capture_output=True,
                                   text=True,
                                   check=True)
            print(f"Node.js version: {result.stdout.strip()}")
        except subprocess.CalledProcessError:
            print("Could not determine Node.js version")
    else:
        print("Node.js not found")

    # Check project structure
    print(f"\nProject root: {project_root}")

    js_test_file = os.path.join(project_root, 'tests', 'test_js_crypto.js')
    if os.path.exists(js_test_file):
        print(f"JavaScript test file found: {js_test_file}")
    else:
        print(f"JavaScript test file NOT found: {js_test_file}")

    py_test_file = os.path.join(project_root, 'tests', 'test_encrypt.py')
    if os.path.exists(py_test_file):
        print(f"Python test file found: {py_test_file}")
    else:
        print(f"Python test file NOT found: {py_test_file}")

    crypto_manager_test = os.path.join(project_root, 'tests/unit', 'test_crypto_manager.py')
    if os.path.exists(crypto_manager_test):
        print(f"Crypto Manager test file found: {crypto_manager_test}")
    else:
        print(f"Crypto Manager test file NOT found: {crypto_manager_test}")

    js_shim_file = os.path.join(project_root, 'tests', 'js_test_shim.js')
    if os.path.exists(js_shim_file):
        print(f"JavaScript shim file found: {js_shim_file}")
    else:
        print(f"JavaScript shim file NOT found: {js_shim_file}")

    # Check node_modules
    jsencrypt_module = os.path.join(project_root, 'node_modules', 'jsencrypt')
    if os.path.exists(jsencrypt_module):
        print(f"jsencrypt module found: {jsencrypt_module}")
    else:
        print(f"jsencrypt module NOT found: {jsencrypt_module}")

    cryptojs_module = os.path.join(project_root, 'node_modules', 'crypto-js')
    if os.path.exists(cryptojs_module):
        print(f"crypto-js module found: {cryptojs_module}")
    else:
        print(f"crypto-js module NOT found: {cryptojs_module}")

    return True

if __name__ == "__main__":
    # First check the environment
    check_environment()

    # Run the tests directly when script is executed
    print("\n=== Running JavaScript Tests ===\n")
    run_js_test()

    print("\n=== Running Python Tests ===\n")
    run_python_tests()

    print("\n=== Manual Compatibility Test Instructions ===\n")
    manual_crypto_compatibility_test()
