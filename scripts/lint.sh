#!/usr/bin/env bash
# The local gate must check exactly what CI checks (task Q6).
#
# It did not: this ran the linter and mypy but never the formatter, while the
# only place `ruff format --check` ran was a GitLab pipeline that never
# executed. A contributor could get this script green and still be red in CI,
# which is the drift CLAUDE.md's "use scripts/" rule exists to prevent, and
# the same one-place-not-two argument O3's workflow makes by shelling out to
# these scripts instead of reimplementing the gates in YAML.
#
# The format check goes first because it is by far the cheapest of the three:
# a gate that fails should fail fast. `src tests` matches CI's own scope.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m ruff format --check src tests
python3 -m ruff check src tests
python3 -m mypy
