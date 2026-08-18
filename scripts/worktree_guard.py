#!/usr/bin/env python3
"""PreToolUse guard: refuse Edit/Write/file-modifying Bash in the primary checkout.

CLAUDE.md requires all task work to happen in a dedicated git worktree, never
directly in the primary checkout -- a rule that lived only in prose until a
session was caught editing scripts/cockpit.py straight on main with no
worktree. This makes that rule mechanical.

The predicate is "is this the primary checkout", not "is the branch `main`".
It keyed on the branch until DX-22, and that let the failure mode disable the
guard that would have caught its own consequences: once a session switched
the primary checkout to a task branch, the branch was no longer `main` and
edits there went unrefused -- exactly the state the primary checkout was
found in, sitting on a task branch with three files modified. Asking git
whether this is a linked worktree (`rev-parse --git-dir`) holds regardless of
branch, and regardless of how the worktree was created -- unlike checking cwd
against .claude/worktrees/, which a manually created worktree could dodge.

This is the same test scripts/git-guardrails/hooks/reference-transaction
uses. The two are deliberately redundant: this one needs Claude Code to
invoke it, and DX-22 found agy has no equivalent to invoke it at all.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

PROTECTED_BRANCH = "main"

BLOCK_MESSAGE = """\
Refusing to edit files in the primary checkout. CLAUDE.md requires task work \
to happen in a dedicated git worktree:

    git fetch origin && git checkout main && git pull --ff-only
    git worktree add .claude/worktrees/<id>-<slug> -b <type>/<id>-<slug> main

Then re-run this from inside that worktree.
"""

_MUTATING_VERBS = {"rm", "mv", "cp", "touch", "mkdir", "tee", "chmod", "truncate", "dd"}
_SEGMENT_SPLIT = re.compile(r"[;&|]+")
_REDIRECT = re.compile(r">>?(?!&)")
_SED_INPLACE = re.compile(r"\bsed\b[^;&|]*\s(-i|--in-place)\b")


def current_branch(root: Path) -> str | None:
    """The current branch name, or None if it can't be determined."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def is_primary_checkout(root: Path) -> bool:
    """True when `root` is a repository's primary checkout.

    A linked worktree's git dir is `<common>/worktrees/<name>`; the primary
    checkout's is the common dir itself. Anything that is not a git
    repository at all resolves to False -- it is none of this guard's
    business.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    return "/worktrees/" not in proc.stdout.strip()


def is_blocked(root: Path) -> bool:
    """True when edits under `root` should be refused."""
    return is_primary_checkout(root)


def bash_command_is_file_modifying(command: str) -> bool:
    """Best-effort: does this Bash command look like it writes files?

    Deliberately conservative: git/gh plumbing (fetch, checkout, worktree
    add, status, pr create...) is always exempt, since it's exactly what the
    remediation in BLOCK_MESSAGE tells the agent to run.

    The heuristics are textual, so they also match a redirect or an in-place
    editor quoted inside a payload rather than executed -- documenting this
    guard is enough to trip it. That false positive is kept deliberately:
    every suppression considered (ignoring `>` inside quotes, or requiring the
    editor to sit at a command position) also lets a real write through, via
    `bash -c "echo x > f"` and `find . -exec ... {} +` respectively.
    """
    if _REDIRECT.search(command):
        return True
    if _SED_INPLACE.search(command):
        return True
    for segment in _SEGMENT_SPLIT.split(command):
        words = segment.strip().split()
        if not words:
            continue
        if words[0] in ("git", "gh"):
            continue
        if words[0] in _MUTATING_VERBS:
            return True
    return False


def _linked_worktree_paths(root: Path) -> list[Path]:
    """Paths of `root`'s linked worktrees that are not on the protected branch.

    A file under `.claude/worktrees/` sits inside the primary checkout's
    directory tree, but belongs to a different checkout on a different branch.
    Editing one is exactly the remediation BLOCK_MESSAGE asks for, so those
    paths stay writable while the primary checkout is on `main`.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    paths: list[Path] = []
    pending: Path | None = None
    for line in [*proc.stdout.splitlines(), ""]:
        if line.startswith("worktree "):
            pending = Path(line[len("worktree ") :])
        elif line.startswith("branch refs/heads/"):
            if line[len("branch refs/heads/") :] == PROTECTED_BRANCH:
                pending = None
        elif not line.strip():
            if pending is not None:
                paths.append(pending)
            pending = None
    return paths


def edit_target_is_protected(file_path: str, root: Path) -> bool:
    """True when an Edit/Write at `file_path` touches the checkout at `root`.

    Only files of the protected checkout itself are refused. A target outside
    `root` -- another repository, or somewhere off any repository at all -- and
    a target inside one of `root`'s linked worktrees are both allowed: neither
    is task work on `main`. Anything that cannot be resolved to a path clearly
    outside `root` is treated as protected.
    """
    if not file_path:
        return True
    candidate = Path(file_path)
    if not candidate.is_absolute():
        return True
    try:
        target = candidate.resolve()
        root_resolved = root.resolve()
    except OSError:
        return True
    if target != root_resolved and root_resolved not in target.parents:
        return False
    for worktree in _linked_worktree_paths(root):
        try:
            resolved = worktree.resolve()
        except OSError:
            continue
        if resolved == root_resolved:
            continue
        if target == resolved or resolved in target.parents:
            return False
    return True


def should_block(tool_name: str, tool_input: dict[str, Any], root: Path) -> bool:
    if not is_blocked(root):
        return False
    if tool_name in ("Edit", "Write"):
        return edit_target_is_protected(str(tool_input.get("file_path") or ""), root)
    if tool_name == "Bash":
        return bash_command_is_file_modifying(str(tool_input.get("command", "")))
    return False


def main(argv: list[str] | None = None) -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    if should_block(tool_name, tool_input, Path.cwd()):
        print(BLOCK_MESSAGE, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
