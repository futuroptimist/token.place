#!/bin/bash
# Shell script to run all tests for token.place

# Fail on errors, unset variables, and pipeline failures
set -euo pipefail

# Enable coverage collection if TEST_COVERAGE=1
if [ "${TEST_COVERAGE:-0}" = "1" ]; then
    echo "Coverage mode enabled"
    COVERAGE_ARGS="--cov=. --cov-append"
    COVERAGE_MODE=true
else
    COVERAGE_ARGS=""
    COVERAGE_MODE=false
fi

echo "======================================================"
echo " token.place Test Runner"
echo "======================================================"

# Determine Python executable
if command -v python >/dev/null 2>&1; then
    PYTHON_CMD=python
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_CMD=python3
else
    echo "Error: Python is not installed. Please install Python to run these tests."
    exit 1
fi

# Check if Node.js is installed
if ! command -v node &> /dev/null; then
    echo "Error: Node.js is not installed. Please install Node.js to run these tests."
    exit 1
fi

# Get Node.js version
node -v

# Ensure Playwright browsers are installed. The --with-deps flag attempts to
# install system packages via apt, which is not available in GitHub Actions.
# Fallback to a plain browser install if system dependency installation fails.
if command -v playwright >/dev/null 2>&1; then
    if ! playwright install --with-deps chromium >/dev/null 2>&1; then
        echo "Warning: playwright install --with-deps failed; retrying without system deps"
        PLAYWRIGHT_BROWSERS_PATH=0 playwright install chromium >/dev/null 2>&1 || \
            echo "Warning: playwright browser installation failed"
    fi
fi

# Array to track test failures
FAILED_TESTS=()

# Function to run tests
run_test() {
    TEST_NAME=$1
    COMMAND=$2
    DESCRIPTION=$3

    echo ""
    echo "======================================================"
    echo " Running $TEST_NAME"
    echo "======================================================"
    echo "$DESCRIPTION"
    echo ""

    if eval $COMMAND; then
        echo -e "\e[32m✅ $TEST_NAME passed\e[0m"
    else
        echo -e "\e[31m❌ $TEST_NAME failed\e[0m"
        FAILED_TESTS+=("$TEST_NAME")
    fi
}

# 1. Run main Python tests
run_test "Python Unit Tests" "$PYTHON_CMD -m pytest tests/unit/ -v $COVERAGE_ARGS" "Testing individual components in isolation"

# 2. Run integration tests if they exist
if [ -d "tests/integration/" ]; then
    run_test "Python Integration Tests" "$PYTHON_CMD -m pytest tests/integration/ -v $COVERAGE_ARGS" "Testing interactions between components"
fi

# 3. Run API tests
run_test "API Tests" "$PYTHON_CMD -m pytest tests/test_api.py -v $COVERAGE_ARGS" "Testing API functionality and compatibility"

# 3b. Run security audits (Bandit)
run_test "Security Audit (Bandit)" "$PYTHON_CMD -m pytest tests/test_security_bandit.py -v $COVERAGE_ARGS" "Scanning the codebase for medium/high Bandit findings"

# 4. Run crypto compatibility tests - simple
run_test "Crypto Compatibility Tests (Simple)" "$PYTHON_CMD tests/test_crypto_compatibility_simple.py $COVERAGE_ARGS" "Testing cross-language compatibility for encryption (simple tests)"

# 5. Run crypto compatibility tests - local
run_test "Crypto Compatibility Tests (Local)" "$PYTHON_CMD tests/test_crypto_compatibility_local.py $COVERAGE_ARGS" "Testing cross-language compatibility for encryption (local tests)"

# 6. Run crypto compatibility tests - Playwright
run_test "Crypto Compatibility Tests (Playwright)" "$PYTHON_CMD -m pytest tests/test_crypto_compatibility_playwright.py -v $COVERAGE_ARGS" "Testing cross-language compatibility in browsers with Playwright"

# 7. Run JavaScript tests
run_test "JavaScript Tests" "npm run test:js" "Testing JavaScript functionality"

# 7b. Test Raspberry Pi cgroup setup script
run_test "Cgroup Setup Script Tests" "bash tests/test_cgroup.sh" "Validating prepare-pi-cgroups.sh logic"

# 8. Run E2E tests
if [ "${RUN_E2E:-0}" = "1" ]; then
    run_test "End-to-End Tests" "$PYTHON_CMD -m pytest tests/test_e2e_*.py -v $COVERAGE_ARGS" "Testing complete workflows"
else
    echo "Skipping End-to-End Tests (set RUN_E2E=1 to enable)"
fi

# 9. Run failure recovery tests
if [ "${RUN_E2E:-0}" = "1" ]; then
    run_test "Failure Recovery Tests" "$PYTHON_CMD -m pytest tests/test_failure_recovery.py -v $COVERAGE_ARGS" "Testing system resilience against errors"
else
    echo "Skipping Failure Recovery Tests (set RUN_E2E=1 to enable)"
fi

# 10. Run DSPACE integration tests
if [ -d "integration_tests/" ]; then
    echo ""
    echo "======================================================"
    echo " Running DSPACE Integration Tests"
    echo "======================================================"
    echo "Testing token.place as a drop-in replacement for OpenAI in DSPACE"
    echo ""

    cd integration_tests
    ./run_integration_test.sh
    DSPACE_STATUS=$?
    cd ..

    if [ $DSPACE_STATUS -ne 0 ]; then
        echo -e "\e[31m❌ DSPACE Integration Tests failed with exit code: $DSPACE_STATUS\e[0m"
        FAILED_TESTS+=("DSPACE Integration Tests")
    else
        echo -e "\e[32m✅ DSPACE Integration Tests passed\e[0m"
    fi
fi

# Generate coverage report if enabled
if [ "$COVERAGE_MODE" = true ]; then
    coverage xml
fi

# Summary
echo ""
echo "======================================================"
echo " Test Summary"
echo "======================================================"

if [ ${#FAILED_TESTS[@]} -eq 0 ]; then
    echo -e "\e[32mAll tests passed! 🎉\e[0m"
    exit 0
else
    echo -e "\e[31mThe following tests failed:\e[0m"
    for test in "${FAILED_TESTS[@]}"; do
        echo -e "\e[31m  - $test\e[0m"
    done
    echo ""
    echo -e "\e[31m${#FAILED_TESTS[@]} test(s) failed\e[0m"
    exit 1
fi
