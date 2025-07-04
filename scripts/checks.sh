#!/usr/bin/env bash
set -e

# python checks
flake8 . || true
isort --check-only . || true
black --check . || true

# js checks
if [ -f package.json ]; then
  npm install --no-audit --no-fund
  npm run lint || true
  npm run format:check || true
fi

# run tests
./run_all_tests.sh

# docs checks
if command -v pyspelling >/dev/null 2>&1 && [ -f spellcheck.yaml ]; then
  pyspelling -c spellcheck.yaml || true
fi
linkchecker README.md docs/ || true
