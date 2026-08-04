#!/usr/bin/env bash
# Run tests: pytest if available, otherwise stdlib offline runner.
set -euo pipefail
cd "$(dirname "$0")/.."
if python3 -c "import pytest" 2>/dev/null; then
  python3 -m pytest -q --junitxml=junit.xml "$@"
else
  echo "pytest unavailable -> offline runner"
  python3 scripts/offline_runner.py
fi
