#!/usr/bin/env bash
set -euo pipefail

python scripts/check_doc_links.py

# Run all tests and code quality checks
./run_all_tests.sh
