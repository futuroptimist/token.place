"""
Visual verification tests for the token.place chat UI.

These tests capture screenshots of the chat interface in different states
and compare them with baseline images to detect visual regressions.
"""
import pytest
import logging
import time
from playwright.sync_api import Page
from .utils import capture_screenshot, save_as_baseline, compare_with_baseline

# Skip tests if Pillow is not installed
pytest.importorskip("PIL", reason="Pillow is required for image comparison")

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('visual_verification.chat_ui')

# Constants
HOME_URL = "http://localhost:5010"  # Relay UI (serves the web interface)

@pytest.mark.visual
def test_chat_ui_initial_state(page: Page, visual_test_context, create_baseline_mode, setup_servers):
    """
    Test the initial state of the chat UI.

    This test:
    1. Navigates to the home page
    2. Captures a screenshot of the initial chat UI
    3. Compares it with the baseline or creates a new baseline
    """
    # Navigate to the home page
    page.goto(HOME_URL)
    page.wait_for_load_state("networkidle")

    # Ensure the page is fully loaded
    assert page.title() != "", "Page title should not be empty"

    # Capture screenshot
    test_name = "chat_ui_initial"
    screenshot_path = capture_screenshot(page, test_name)

    result = {
        "name": test_name,
        "screenshot": screenshot_path,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
    }

    # If in baseline creation mode, save this as the baseline
    if create_baseline_mode:
        baseline_path = save_as_baseline(screenshot_path, test_name)
        result["baseline_created"] = True
        result["baseline_path"] = baseline_path
        result["passed"] = True
        logger.info(f"Created baseline: {baseline_path}")
    else:
        # Compare with baseline
        match_success, diff_path, diff_percentage = compare_with_baseline(screenshot_path, test_name)
        result["match_success"] = match_success
        result["diff_percentage"] = f"{diff_percentage:.2f}%"

        if diff_path:
            result["diff_path"] = diff_path

        result["passed"] = match_success

        # Assert that the images match closely enough
        assert match_success, f"Visual verification failed: Images differ by {diff_percentage:.2f}%"

    # Add the result to the visual test context
    visual_test_context.add_result(result)

@pytest.mark.visual
def test_chat_ui_responsive(page: Page, visual_test_context, create_baseline_mode, setup_servers):
    """
    Test the responsiveness of the chat UI at different viewport sizes.

    This test:
    1. Navigates to the home page
    2. Resizes the viewport to different dimensions
    3. Captures screenshots at each size
    4. Compares them with baselines or creates new baselines
    """
    # Navigate to the home page
    page.goto(HOME_URL)
    page.wait_for_load_state("networkidle")

    # Test each viewport size
    for device_name, viewport in visual_test_context.viewports.items():
        # Set viewport size
        page.set_viewport_size(viewport)

        # Allow time for responsive layout to adjust
        time.sleep(1)

        # Capture screenshot
        test_name = f"chat_ui_{device_name}"
        screenshot_path = capture_screenshot(page, test_name)

        result = {
            "name": test_name,
            "viewport": viewport,
            "screenshot": screenshot_path,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
        }

        # If in baseline creation mode, save this as the baseline
        if create_baseline_mode:
            baseline_path = save_as_baseline(screenshot_path, test_name)
            result["baseline_created"] = True
            result["baseline_path"] = baseline_path
            result["passed"] = True
            logger.info(f"Created baseline for {device_name}: {baseline_path}")
        else:
            # Compare with baseline
            match_success, diff_path, diff_percentage = compare_with_baseline(screenshot_path, test_name)
            result["match_success"] = match_success
            result["diff_percentage"] = f"{diff_percentage:.2f}%"

            if diff_path:
                result["diff_path"] = diff_path

            result["passed"] = match_success

            # Assert that the images match closely enough
            assert match_success, f"Visual verification failed for {device_name}: Images differ by {diff_percentage:.2f}%"

        # Add the result to the visual test context
        visual_test_context.add_result(result)

@pytest.mark.visual
def test_chat_ui_with_message(page: Page, visual_test_context, create_baseline_mode, setup_servers):
    """
    Test the chat UI with a message displayed.

    This test:
    1. Navigates to the home page
    2. Enters a test message
    3. Captures a screenshot of the UI with the message
    4. Compares it with the baseline or creates a new baseline
    """
    # Navigate to the home page
    page.goto(HOME_URL)
    page.wait_for_load_state("networkidle")

    # Try to find and interact with the chat input
    try:
        # Wait for chat UI to be ready
        time.sleep(2)

        # Look for common chat input selectors
        input_selector = "#chatInput, .chat-input, input[type='text'], textarea"
        chat_input = page.locator(input_selector).first

        # Enter a test message
        if chat_input.count() > 0:
            chat_input.fill("This is a test message for visual verification")
            logger.info("Entered test message in chat input")
        else:
            logger.warning("Could not find chat input field")
    except Exception as e:
        logger.error(f"Error interacting with chat UI: {e}")

    # Capture screenshot regardless of whether we could enter text
    test_name = "chat_ui_with_message"
    screenshot_path = capture_screenshot(page, test_name)

    result = {
        "name": test_name,
        "screenshot": screenshot_path,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
    }

    # If in baseline creation mode, save this as the baseline
    if create_baseline_mode:
        baseline_path = save_as_baseline(screenshot_path, test_name)
        result["baseline_created"] = True
        result["baseline_path"] = baseline_path
        result["passed"] = True
        logger.info(f"Created baseline: {baseline_path}")
    else:
        # Compare with baseline
        match_success, diff_path, diff_percentage = compare_with_baseline(screenshot_path, test_name)
        result["match_success"] = match_success
        result["diff_percentage"] = f"{diff_percentage:.2f}%"

        if diff_path:
            result["diff_path"] = diff_path

        result["passed"] = match_success

        # Assert that the images match closely enough
        assert match_success, f"Visual verification failed: Images differ by {diff_percentage:.2f}%"

    # Add the result to the visual test context
    visual_test_context.add_result(result)
