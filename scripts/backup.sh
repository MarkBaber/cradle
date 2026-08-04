#!/usr/bin/env bash
# Thin wrapper; the logic lives in backup.py so it is testable and needs no
# sqlite3 CLI (absent from minimal Raspberry Pi OS images).
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 scripts/backup.py "$@"
