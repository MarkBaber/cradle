#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m ruff format src tests
python3 -m ruff check --fix src tests
