#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m ruff check src tests
python3 -m mypy
