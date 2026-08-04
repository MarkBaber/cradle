#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m uvicorn --factory cradle.app:create_app --reload --host 0.0.0.0 --port 8134 --app-dir src
