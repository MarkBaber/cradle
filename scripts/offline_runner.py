#!/usr/bin/env python3
"""Offline test-runner fallback: discovers and runs test_* functions with stdlib
only. Used when pytest is unavailable (air-gapped Pi, CI cache miss)."""

import importlib.util
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    passed, failed, skipped = 0, 0, 0
    for path in sorted((ROOT / "tests").rglob("test_*.py")):
        # Local helper modules (e.g. tests/unit/_helpers.py) resolve like pytest's
        # prepend import mode: the test file's own directory goes on sys.path.
        if str(path.parent) not in sys.path:
            sys.path.insert(0, str(path.parent))
        spec = importlib.util.spec_from_file_location(path.stem + "_" + str(passed), path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except ModuleNotFoundError as e:
            print(f"SKIP  {path.name} (missing dep: {e.name})")
            skipped += 1
            continue
        for name in sorted(dir(mod)):
            if not name.startswith("test_"):
                continue
            try:
                getattr(mod, name)()
                print(f"PASS  {path.name}::{name}")
                passed += 1
            except ModuleNotFoundError as e:
                print(f"SKIP  {path.name}::{name} (missing dep: {e.name})")
                skipped += 1
            except Exception:
                print(f"FAIL  {path.name}::{name}")
                traceback.print_exc()
                failed += 1
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
