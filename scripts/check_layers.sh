#!/usr/bin/env bash
# Layering contract: import-linter if available, AST fallback always.
set -euo pipefail
cd "$(dirname "$0")/.."
if python3 -c "import importlinter" 2>/dev/null; then
  python3 -m importlinter.cli lint
fi
python3 - << 'PY'
import sys
sys.path.insert(0, "src")
sys.path.insert(0, "tests/unit")
from test_layers import test_layering
test_layering()
print("AST layering check: OK")
PY
