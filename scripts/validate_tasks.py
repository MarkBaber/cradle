#!/usr/bin/env python3
"""Parse all TOML + verify tasks.toml dependency graph (acyclic, no dangling)."""

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

raw = (ROOT / "tasks.toml").read_text(encoding="utf-8")
for block in raw.split("[[task]]")[1:]:
    for key in ("id", "status", "notes", "phase"):
        if block.count(f"\n{key} = ") > 1:
            sys.exit(f"duplicate '{key}' key in a task block")

for f in ("pyproject.toml", "tasks.toml", "rules_config.toml"):
    with (ROOT / f).open("rb") as fh:
        tomllib.load(fh)

with (ROOT / "tasks.toml").open("rb") as fh:
    tasks = {t["id"]: t for t in tomllib.load(fh)["task"]}

ALLOWED_STATUS = {"todo", "doing", "review", "blocked", "done"}

for t in tasks.values():
    for key in ("id", "phase", "title", "status", "routing", "depends", "touches",
                "description", "exit_criteria"):
        assert key in t, f"{t.get('id','?')} missing {key}"
    assert t["status"] in ALLOWED_STATUS, f"{t['id']}: bad status {t['status']!r}"
    for d in t["depends"]:
        assert d in tasks, f"dangling depends: {t['id']} -> {d}"

# A task cannot be done while anything it depends on is not.
for t in tasks.values():
    if t["status"] == "done":
        for d in t["depends"]:
            assert tasks[d]["status"] == "done", (
                f"{t['id']} is done but depends on unfinished {d}"
            )

seen: set[str] = set()
stack: set[str] = set()


def dfs(i: str) -> None:
    if i in stack:
        sys.exit(f"dependency cycle at {i}")
    if i in seen:
        return
    stack.add(i)
    seen.add(i)
    for d in tasks[i]["depends"]:
        dfs(d)
    stack.remove(i)


for i in tasks:
    dfs(i)
print(f"tasks.toml OK ({len(tasks)} tasks, graph acyclic)")
