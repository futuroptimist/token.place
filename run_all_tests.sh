#!/bin/bash
# Shell script to run all tests for token.place

set -e

# Enable coverage collection if TEST_COVERAGE=1
if [ "$TEST_COVERAGE" = "1" ]; then
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

# Check if Node.js is installed
if ! command -v node &> /dev/null; then
    echo "Error: Node.js is not installed. Please install Node.js to run these tests."
    exit 1
fi

# Get Node.js version
node -v

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
run_test "Python Unit Tests" "python -m pytest tests/unit/ -v $COVERAGE_ARGS" "Testing individual components in isolation"

# 2. Run integration tests if they exist
if [ -d "tests/integration/" ]; then
    run_test "Python Integration Tests" "python -m pytest tests/integration/ -v $COVERAGE_ARGS" "Testing interactions between components"
fi

# 3. Run API tests
run_test "API Tests" "python -m pytest tests/test_api.py -v $COVERAGE_ARGS" "Testing API functionality and compatibility"

# 4. Run crypto compatibility tests - simple
run_test "Crypto Compatibility Tests (Simple)" "python tests/test_crypto_compatibility_simple.py $COVERAGE_ARGS" "Testing cross-language compatibility for encryption (simple tests)"

# 5. Run crypto compatibility tests - local
run_test "Crypto Compatibility Tests (Local)" "python tests/test_crypto_compatibility_local.py $COVERAGE_ARGS" "Testing cross-language compatibility for encryption (local tests)"

# 6. Run crypto compatibility tests - Playwright
run_test "Crypto Compatibility Tests (Playwright)" "python -m pytest tests/test_crypto_compatibility_playwright.py -v $COVERAGE_ARGS" "Testing cross-language compatibility in browsers with Playwright"

# 7. Run JavaScript tests
run_test "JavaScript Tests" "npm run test:js" "Testing JavaScript functionality"

# 8. Run E2E tests
if [ "$RUN_E2E" = "1" ]; then
    run_test "End-to-End Tests" "python -m pytest tests/test_e2e_*.py -v $COVERAGE_ARGS" "Testing complete workflows"
else
    echo "Skipping End-to-End Tests (set RUN_E2E=1 to enable)"
fi

# 9. Run failure recovery tests
if [ "$RUN_E2E" = "1" ]; then
    run_test "Failure Recovery Tests" "python -m pytest tests/test_failure_recovery.py -v $COVERAGE_ARGS" "Testing system resilience against errors"
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

