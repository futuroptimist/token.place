# Visual Verification Testing for token.place

This directory contains a framework for visual regression testing of the token.place UI. The framework captures screenshots of key UI states and compares them against baseline images to detect unwanted changes.

## How Visual Verification Works

1. The framework captures screenshots of key UI states
2. These screenshots are compared with baseline images
3. Differences are highlighted and quantified
4. A visual report is generated

## Directory Structure

- `baselines/` - Contains the baseline screenshots for comparison
- `current/` - Contains the most recent screenshots from test runs
- `diffs/` - Contains visual diffs highlighting differences between baseline and current screenshots
- `visual_report.json` - Contains results of the most recent test run

## Running Visual Verification Tests

### Creating Baselines

Before running comparison tests, you need to create baseline images. To do this:

```bash
# Windows
set CREATE_BASELINE=1
python -m pytest tests/visual_verification/test_chat_ui.py -m visual

# Unix/Linux/macOS
export CREATE_BASELINE=1
python -m pytest tests/visual_verification/test_chat_ui.py -m visual
```

### Running Comparison Tests

Once baselines are created, you can run the tests to compare against the baselines:

```bash
python -m pytest tests/visual_verification/test_chat_ui.py -m visual
```

### Running All Visual Tests

To run all visual verification tests:

```bash
python -m pytest tests/visual_verification/ -m visual
```

## Adding New Visual Tests

To add a new visual test:

1. Create a new test file in the `tests/visual_verification/` directory
2. Import the necessary utilities:
   ```python
   from .utils import capture_screenshot, save_as_baseline, compare_with_baseline
   ```
3. Use the `@pytest.mark.visual` decorator to mark your test functions
4. Use the test context fixtures:
   ```python
   def test_my_feature(page, visual_test_context, create_baseline_mode, setup_servers):
       # Test code here
   ```
5. Capture screenshots and compare with baselines or create new ones
6. Add results to the test context

## Prerequisites

The visual verification tests require:

1. Python 3.11 or higher
2. Playwright (`pip install playwright`)
3. Pillow (`pip install pillow`)
4. Running servers (relay and server components)

## Sample Test Flow

```python
# Navigate to the page
page.goto("http://localhost:5000")

# Capture a screenshot
screenshot_path = capture_screenshot(page, "feature_name")

# In baseline mode
if create_baseline_mode:
    baseline_path = save_as_baseline(screenshot_path, "feature_name")
    # Log and record the baseline creation
else:
    # Compare with existing baseline
    match_success, diff_path, diff_percentage = compare_with_baseline(
        screenshot_path, "feature_name"
    )
    # Assert and record the comparison results
```

## Thresholds and Configuration

The default threshold for image differences is 1% - images with differences greater than this will fail the test. This can be adjusted in the `utils.py` file if needed.

## Interpreting Results

After running the tests, check:

1. The console output for test results
2. The `visual_report.json` file for detailed information
3. The `diffs/` directory for visual representations of differences
