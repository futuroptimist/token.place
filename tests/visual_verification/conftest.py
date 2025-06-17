"""
Pytest configuration for visual verification tests.
"""
import os
import pytest
from pathlib import Path
from typing import List, Dict, Any
import logging
from .utils import generate_report

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('visual_verification.conftest')

# Store test results
test_results = []

@pytest.fixture(scope="session")
def visual_test_context():
    """
    Fixture that provides a context for visual verification tests.
    
    Returns:
        A dictionary with helper functions and test context
    """
    class VisualContext:
        def __init__(self):
            self.results = []
            self.viewports = {
                "mobile": {"width": 375, "height": 667},
                "tablet": {"width": 768, "height": 1024},
                "desktop": {"width": 1366, "height": 768}
            }
        
        def add_result(self, result: Dict[str, Any]):
            """Add a test result to the collection."""
            self.results.append(result)
            # Also add to the global results
            test_results.append(result)
        
        def get_results(self) -> List[Dict[str, Any]]:
            """Get all test results."""
            return self.results

    yield VisualContext()


@pytest.fixture(scope="session", autouse=True)
def generate_visual_report(request):
    """
    Generate a visual verification report at the end of the test session.
    """
    yield
    # After all tests have completed
    logger.info(f"Generating visual verification report with {len(test_results)} results")
    generate_report(test_results)


@pytest.fixture
def create_baseline_mode():
    """
    Determine if we're in baseline creation mode based on environment variable.
    
    Returns:
        bool: True if CREATE_BASELINE=1 is set
    """
    return os.environ.get("CREATE_BASELINE", "0") == "1" 