# PowerShell script to run all tests for token.place

$ErrorActionPreference = "Stop"
$currentDir = Get-Location

Write-Host "======================================================"
Write-Host " token.place Test Runner"
Write-Host "======================================================"

# Check if Node.js is installed
try {
    $nodeVersion = node -v
    Write-Host $nodeVersion
}
catch {
    Write-Error "Node.js is not installed. Please install Node.js to run these tests."
    exit 1
}

# Create array to track test failures
$failedTests = @()

function RunTest {
    param (
        [string]$TestName,
        [string]$Command,
        [string]$Description
    )

    Write-Host ""
    Write-Host "======================================================"
    Write-Host " Running $TestName"
    Write-Host "======================================================"
    Write-Host $Description
    Write-Host ""

    Invoke-Expression $Command

    if ($LASTEXITCODE -ne 0) {
        Write-Host -ForegroundColor Red "❌ $TestName failed with exit code: $LASTEXITCODE"
        $script:failedTests += $TestName
    } else {
        Write-Host -ForegroundColor Green "✅ $TestName passed"
    }
}

# 1. Run main Python tests
RunTest -TestName "Python Unit Tests" -Command "python -m pytest tests/unit/ -v" -Description "Testing individual components in isolation"

# 2. Run integration tests if they exist
if (Test-Path "tests/integration/") {
    RunTest -TestName "Python Integration Tests" -Command "python -m pytest tests/integration/ -v" -Description "Testing interactions between components"
}

# 3. Run API tests
RunTest -TestName "API Tests" -Command "python -m pytest tests/test_api.py -v" -Description "Testing API functionality and compatibility"

# 4. Run crypto compatibility tests - simple
RunTest -TestName "Crypto Compatibility Tests (Simple)" -Command "python tests/test_crypto_compatibility_simple.py" -Description "Testing cross-language compatibility for encryption (simple tests)"

# 5. Run crypto compatibility tests - local
RunTest -TestName "Crypto Compatibility Tests (Local)" -Command "python tests/test_crypto_compatibility_local.py" -Description "Testing cross-language compatibility for encryption (local tests)"

# 6. Run crypto compatibility tests - Playwright
RunTest -TestName "Crypto Compatibility Tests (Playwright)" -Command "python -m pytest tests/test_crypto_compatibility_playwright.py -v" -Description "Testing cross-language compatibility in browsers with Playwright"

# 7. Run JavaScript tests
RunTest -TestName "JavaScript Tests" -Command "npm run test:js" -Description "Testing JavaScript functionality"

# 8. Run E2E tests
RunTest -TestName "End-to-End Tests" -Command "python -m pytest tests/test_e2e_*.py -v" -Description "Testing complete workflows"

# 9. Run failure recovery tests
RunTest -TestName "Failure Recovery Tests" -Command "python -m pytest tests/test_failure_recovery.py -v" -Description "Testing system resilience against errors"

# 10. Run DSPACE integration tests
if (Test-Path "integration_tests/") {
    Write-Host ""
    Write-Host "======================================================"
    Write-Host " Running DSPACE Integration Tests"
    Write-Host "======================================================"
    Write-Host "Testing token.place as a drop-in replacement for OpenAI in DSPACE"
    Write-Host ""

    Set-Location integration_tests
    .\run_integration_test.ps1

    if ($LASTEXITCODE -ne 0) {
        Write-Host -ForegroundColor Red "❌ DSPACE Integration Tests failed with exit code: $LASTEXITCODE"
        $failedTests += "DSPACE Integration Tests"
    } else {
        Write-Host -ForegroundColor Green "✅ DSPACE Integration Tests passed"
    }

    Set-Location $currentDir
}

# Summary
Write-Host ""
Write-Host "======================================================"
Write-Host " Test Summary"
Write-Host "======================================================"

if ($failedTests.Count -eq 0) {
    Write-Host -ForegroundColor Green "All tests passed! 🎉"
} else {
    Write-Host -ForegroundColor Red "The following tests failed:"
    foreach ($test in $failedTests) {
        Write-Host -ForegroundColor Red "  - $test"
    }
    Write-Host ""
    Write-Host -ForegroundColor Red "$($failedTests.Count) test(s) failed"
    exit 1
}

exit 0
