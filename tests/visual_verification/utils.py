"""
Utility functions for visual verification testing.
"""
import os
import time
import shutil
import logging
from pathlib import Path
from typing import Optional, Tuple, List
import json

try:
    from PIL import Image, ImageChops, ImageDraw
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('visual_verification')

# Constants
BASELINES_DIR = Path(__file__).parent / "baselines"
CURRENT_DIR = Path(__file__).parent / "current"
DIFF_DIR = Path(__file__).parent / "diffs"
REPORT_FILE = Path(__file__).parent / "visual_report.json"

# Create the necessary directories
BASELINES_DIR.mkdir(exist_ok=True, parents=True)
CURRENT_DIR.mkdir(exist_ok=True, parents=True)
DIFF_DIR.mkdir(exist_ok=True, parents=True)

def ensure_pil_installed():
    """Verify PIL is installed for image comparison."""
    if not HAS_PIL:
        logger.error("PIL is required for image comparison. Install with: pip install pillow")
        raise ImportError("PIL is required for image comparison. Install with: pip install pillow")

def capture_screenshot(page, name: str, viewport: Optional[dict] = None) -> str:
    """
    Capture a screenshot of the current page state.
    
    Args:
        page: Playwright page object
        name: Name of the screenshot (used for the filename)
        viewport: Optional dictionary with width and height for responsive testing
    
    Returns:
        Path to the saved screenshot
    """
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    filename = f"{name}_{timestamp}.png"
    screenshot_path = CURRENT_DIR / filename
    
    # Set viewport if specified
    if viewport:
        page.set_viewport_size(viewport)
        # Allow time for responsive layout to update
        time.sleep(1)
    
    # Capture the screenshot
    page.screenshot(path=str(screenshot_path))
    logger.info(f"Screenshot captured: {screenshot_path}")
    
    return str(screenshot_path)

def save_as_baseline(screenshot_path: str, name: str) -> str:
    """
    Save a screenshot as a baseline for future comparisons.
    
    Args:
        screenshot_path: Path to the screenshot file
        name: Name for the baseline file
    
    Returns:
        Path to the saved baseline
    """
    baseline_path = BASELINES_DIR / f"{name}.png"
    shutil.copy2(screenshot_path, baseline_path)
    logger.info(f"Saved as baseline: {baseline_path}")
    return str(baseline_path)

def compare_with_baseline(screenshot_path: str, baseline_name: str) -> Tuple[bool, Optional[str], float]:
    """
    Compare a screenshot with a baseline image.
    
    Args:
        screenshot_path: Path to the current screenshot
        baseline_name: Name of the baseline to compare against
    
    Returns:
        Tuple of (match_success, diff_path, difference_percentage)
    """
    ensure_pil_installed()
    
    baseline_path = BASELINES_DIR / f"{baseline_name}.png"
    
    # Check if baseline exists
    if not baseline_path.exists():
        logger.warning(f"Baseline not found: {baseline_path}")
        return False, None, 100.0
    
    # Open images
    baseline_img = Image.open(baseline_path)
    current_img = Image.open(screenshot_path)
    
    # Check image sizes
    if baseline_img.size != current_img.size:
        logger.warning(f"Image size mismatch: {baseline_img.size} vs {current_img.size}")
        # Resize current to match baseline for comparison
        current_img = current_img.resize(baseline_img.size)
    
    # Calculate difference
    diff_img = ImageChops.difference(baseline_img, current_img)
    
    # Create a more visible diff image
    diff_bbox = diff_img.getbbox()
    
    # If there's no difference, getbbox returns None
    if diff_bbox is None:
        logger.info("Images are identical")
        return True, None, 0.0
    
    # Create a visual diff by highlighting differences
    visual_diff = current_img.copy()
    draw = ImageDraw.Draw(visual_diff)
    
    # Calculate percentage of different pixels
    diff_pixels = 0
    total_pixels = baseline_img.width * baseline_img.height
    
    for y in range(baseline_img.height):
        for x in range(baseline_img.width):
            # Check if there's a significant difference at this pixel
            diff_val = sum(diff_img.getpixel((x, y)))
            if diff_val > 10:  # Threshold for difference detection
                diff_pixels += 1
                # Draw a red rectangle around the difference
                draw.rectangle([(x-1, y-1), (x+1, y+1)], outline="red")
    
    diff_percentage = (diff_pixels / total_pixels) * 100
    
    # Save the visual diff
    diff_filename = f"diff_{baseline_name}_{Path(screenshot_path).stem}.png"
    diff_path = DIFF_DIR / diff_filename
    visual_diff.save(diff_path)
    
    logger.info(f"Images differ by {diff_percentage:.2f}%. Diff saved to {diff_path}")
    
    # Determine if the difference is acceptable (less than 1%)
    match_success = diff_percentage < 1.0
    
    return match_success, str(diff_path), diff_percentage

def generate_report(test_results: List[dict]):
    """
    Generate a JSON report of all visual verification tests.
    
    Args:
        test_results: List of test result dictionaries
    """
    report = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tests": test_results,
        "summary": {
            "total": len(test_results),
            "passed": sum(1 for r in test_results if r.get("passed", False)),
            "failed": sum(1 for r in test_results if not r.get("passed", False))
        }
    }
    
    with open(REPORT_FILE, 'w') as f:
        json.dump(report, f, indent=2)
    
    logger.info(f"Visual verification report generated: {REPORT_FILE}")
    return report 