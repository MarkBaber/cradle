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
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from importlib import util as importutil
from pathlib import Path
from typing import Any

PROTECTED_BRANCH = "main"

BLOCK_MESSAGE = """\
Refusing to edit files in the primary checkout. If this was a read-only \
command, use the Grep/Read tools instead, or run it from a linked worktree. \
CLAUDE.md requires task work to happen in a dedicated git worktree:

    git fetch origin && git checkout main && git pull --ff-only
    git worktree add .claude/worktrees/<id>-<slug> -b <type>/<id>-<slug> main

Then re-run this from inside that worktree.
"""

_MUTATING_VERBS = {"rm", "mv", "cp", "touch", "mkdir", "chmod", "truncate", "dd"}
_SEGMENT_SPLIT = re.compile(r"[;&|]+")
_REDIRECT = re.compile(r">>?(?!&)")
_ARITH_SPAN = re.compile(r"\bawk\b\s+(?:-\S+\s+)*'([^']*)'|\(\(([^)]*)\)\)")
_NUMERIC_OPERAND = re.compile(r"=?\s*-?\d")
_PRINT_STATEMENT = re.compile(r"\bprintf?\b[^;{}]*$")
_LATER_VAR = re.compile(r"\$(?:\w+|\{\w+\})")
_PUNCTUATION = frozenset("();<>|&")
_VAR_PREFIX = re.compile(r"^\$(?:(\w+)|\{(\w+)\})(?=/|$)")
_SHELL_VERBS = frozenset({"bash", "sh", "zsh", "dash"})
_SED_INPLACE = re.compile(r"\bsed\b[^;&|]*\s(-i|--in-place)\b")
_HEREDOC_START = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_]\w*)\1")
_INTERP_INLINE = re.compile(
    r"(?<![\w./-])(?:python[\d.]*|node|nodejs|deno|perl|ruby)\s+(?:-\S+\s+)*?-[A-Za-z]*[ce]\b"
)
_PERL_INPLACE = re.compile(r"(?<![\w./-])perl\s+(?:-\S+\s+)*?-[A-Za-z]*i")
_INTERP_WRITE = re.compile(
    r"open\([^)]*['\"][wax]\+?b?['\"]|\.write(?:_text|_bytes)?\(|writeFile|appendFile"
    r"|createWriteStream|\bunlink|\brmtree|os\.(?:remove|rename|replace|mkdir|makedirs)"
    r"|shutil\.|File\.(?:write|open)|\bFileUtils\b|\bprint\s*\{?\s*\w*\s*['\"]?>"
)


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


def bash_command_is_file_modifying(command: str, root: Path | None = None) -> bool:
    """Best-effort: does this Bash command look like it writes files?

    Deliberately conservative: git/gh plumbing (fetch, checkout, worktree
    add, status, pr create...) is always exempt, since it's exactly what the
    remediation in BLOCK_MESSAGE tells the agent to run.

    The heuristics are textual, so they still match an in-place editor quoted
    inside a payload rather than executed -- requiring the editor to sit at a
    command position would let `find . -exec ... {} +` through. A redirect is
    different (AX-18): a lexer counts only an unquoted `>`, so a quoted
    `grep "<id>"` is not a write, and recurses into the string a `bash -c`,
    `sh -c`, `eval` or `ssh host '...'` executes, so `bash -c "echo x > f"`
    still is. Unbalanced quotes fall back to the plain textual match.

    The one carve-out is a redirect or `tee` target that provably cannot
    touch the repo: /dev/null (DX-47), or, when `root` is given, an absolute
    path resolving outside `root` and every linked worktree (DX-83). A target
    that is relative, unresolvable, or inside the repo keeps the refusal.
    """
    return bash_refusal_reason(command, root) is not None


def _strip_heredoc_bodies(command: str) -> str:
    """`command` without the body lines of any `<<TAG` heredoc (DX-76): the
    body is data handed to a program, so a `>` or a `find` in it is prose,
    not shell syntax. The `<<TAG` line itself stays, so a redirect on it
    (`cat > f <<EOF`) still counts. A body never closed runs to the end."""
    kept: list[str] = []
    tags: list[str] = []
    for line in command.split("\n"):
        if tags:
            if line.strip() == tags[0]:
                tags.pop(0)
            continue
        kept.append(line)
        tags.extend(m.group(2) for m in _HEREDOC_START.finditer(line))
    return "\n".join(kept)


def bash_refusal_reason(command: str, root: Path | None = None) -> str | None:
    """Why `bash_command_is_file_modifying` refuses `command` -- the rule that
    fired and the token it matched -- or None when it does not (AX-18)."""
    command = _strip_heredoc_bodies(command)
    redirect = _find_redirect(command)
    tee_segment = any(
        segment.split()[:1] == ["tee"] for segment in _SEGMENT_SPLIT.split(command)
    )
    if redirect is not None or tee_segment:
        if redirect is not None and redirect[1]:
            return f"`>` at col {redirect[0]}{redirect[1]} read as a file redirect"
        targets = _write_targets(command)
        outside = _outside_repo_checker(root)
        if targets is None or not all(outside(target) for target in targets):
            if redirect is not None:
                return f"`>` at col {redirect[0]} read as a file redirect"
            return "`tee` read as a file write"
    if _SED_INPLACE.search(command):
        return "`sed -i` read as an in-place edit"
    if _PERL_INPLACE.search(command):
        return "`perl -i` read as an in-place edit"
    if _INTERP_INLINE.search(command) and _INTERP_WRITE.search(command):
        return "an interpreter -c/-e one-liner read as a file write"
    for segment in _SEGMENT_SPLIT.split(command):
        words = segment.strip().split()
        if not words:
            continue
        if words[0] in ("git", "gh"):
            continue
        if words[0] in _MUTATING_VERBS:
            return f"`{words[0]}` read as a file-mutating command"
    return None


def _quoted_mask(command: str) -> list[bool] | None:
    """Per character: is it inside a quoted span, or backslash-escaped?

    None when the quotes do not balance -- the caller then keeps the
    conservative textual behaviour. A double-quoted span holding `$(` or a
    backtick is left unmasked, since the shell executes what is inside it.
    """
    mask = [False] * len(command)
    quote = ""
    start = 0
    index = 0
    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'":
            for position in (index, index + 1):
                if position < len(command):
                    mask[position] = True
            index += 2
            continue
        if quote:
            mask[index] = True
            if char == quote:
                span = command[start:index]
                if quote == '"' and ("$(" in span or "`" in span):
                    for position in range(start, index + 1):
                        mask[position] = False
                quote = ""
        elif char in "'\"":
            quote, start = char, index
            mask[index] = True
        index += 1
    return None if quote else mask


def _shell_payloads(command: str) -> list[str]:
    """String arguments the shell will itself execute: `bash -c '...'` (also
    sh/zsh/dash), `eval ...`, and `ssh host '...'`."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        return []
    segments: list[list[str]] = [[]]
    for token in tokens:
        if set(token) <= _PUNCTUATION:
            segments.append([])
        else:
            segments[-1].append(token)
    payloads: list[str] = []
    for words in segments:
        if not words:
            continue
        verb, rest = words[0], words[1:]
        if verb in _SHELL_VERBS:
            for position, word in enumerate(rest):
                if word.startswith("-") and not word.startswith("--") and "c" in word:
                    payloads.extend(rest[position + 1 : position + 2])
                    break
        elif verb == "eval" and rest:
            payloads.append(" ".join(rest))
        elif verb == "ssh" and len(rest) >= 2:
            payloads.append(" ".join(rest[1:]))
    return payloads


def _find_redirect(command: str) -> tuple[int, str] | None:
    """(1-based column, where) of the first `>`/`>>` redirect (or an awk-style
    `>` comparison, which the same textual match catches) whose target is not
    literally /dev/null, else None. /dev/null is the one redirect target that
    can never itself be a file write; any other target is left to
    _write_targets and _outside_repo_checker to clear (DX-83).

    Only a `>` outside quotes counts (AX-18), plus one inside the string a
    `bash -c`/`eval`/`ssh` will execute; unbalanced quotes keep the plain
    textual match.
    """
    arith = [
        m.span(g)
        for m in _ARITH_SPAN.finditer(command)
        for g in (1, 2)
        if m.group(g) is not None
    ]
    awk_spans = [m.span(1) for m in _ARITH_SPAN.finditer(command) if m.group(1) is not None]
    mask = _quoted_mask(command)
    for match in _REDIRECT.finditer(command):
        inside_span = any(a <= match.start() < b for a, b in arith)
        # `count++>0` inside an awk program or `(( ))` is a comparison against
        # a number, not a redirect (DX-81); a quoted-string or bare-word
        # target inside the same span (`print > "f"`) still counts.
        if inside_span and _NUMERIC_OPERAND.match(command[match.end():]):
            continue
        # A quoted `>` is an argument, not a redirect -- except in an awk
        # program, where `print > "f"` writes.
        if mask is not None and mask[match.start()] and not inside_span:
            continue
        # In an awk program only a `>` inside a print/printf statement writes;
        # any other (`if (a > b)`) compares (DX-148).
        if any(a <= match.start() < b for a, b in awk_spans) and not any(
            _PRINT_STATEMENT.search(command[a : match.start()])
            for a, b in awk_spans
            if a <= match.start() < b
        ):
            continue
        if not re.match(r"\s*/dev/null\b", command[match.end():]):
            return match.start() + 1, ""
    if mask is not None:
        for payload in _shell_payloads(command):
            inner = _find_redirect(payload)
            if inner is not None:
                return inner[0], " of a bash -c/eval/ssh payload"
    return None


def _write_targets(command: str) -> list[str] | None:
    """Targets of every `>`/`>>` redirect and `tee` in `command`, or None
    when the command cannot be tokenised faithfully (unbalanced quotes, a
    `>` glued into a larger word as in `bash -c "echo x > f"`, a redirect
    with no target)."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    targets: list[str] = []
    command_start = True
    is_tee = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if set(token) <= _PUNCTUATION:
            if ">" not in token:
                if token not in ("<", "<<", "<<<"):
                    command_start = True
                continue
            if index >= len(tokens):
                return None
            target = tokens[index]
            index += 1
            if token == ">&" and (target == "-" or target.isdigit()):
                continue
            targets.append(target)
        elif ">" in token:
            return None
        elif command_start:
            command_start = False
            is_tee = token == "tee"
        elif is_tee and not token.startswith("-"):
            targets.append(token)
    return targets


def _expand_target(target: str) -> str | None:
    """`~` and a leading `$VAR`/`${VAR}` expanded from the hook's own
    environment; None when the target still depends on anything unknown."""
    if target.startswith("~"):
        target = os.path.expanduser(target)
        if target.startswith("~"):
            return None
    else:
        match = _VAR_PREFIX.match(target)
        if match:
            value = os.environ.get(match.group(1) or match.group(2))
            if not value:
                return None
            rest = target[match.end() :]
            # A later `$var` segment cannot lift the path back into the repo
            # unless it is itself a `..`, which a literal `..` would show.
            if ".." not in rest:
                rest = _LATER_VAR.sub("x", rest)
            target = value + rest
    if any(char in target for char in "$`*?[{"):
        return None
    return target


def _outside_repo_checker(root: Path | None) -> Callable[[str], bool]:
    """A predicate: True when a write target provably cannot touch the repo
    at `root` or any of its linked worktrees. Without a `root`, only
    /dev/null qualifies."""
    protected: list[Path] = []
    if root is not None:
        try:
            top = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).stdout.strip()
            protected = [Path(top or root).resolve()]
            protected += [path.resolve() for path in _linked_worktree_paths(root)]
        except (OSError, subprocess.SubprocessError):
            root = None

    def outside(target: str) -> bool:
        if target == "/dev/null":
            return True
        if root is None:
            return False
        expanded = _expand_target(target)
        if expanded is None or not os.path.isabs(expanded):
            return False
        try:
            resolved = Path(expanded).resolve()
        except (OSError, RuntimeError):
            return False
        return not any(resolved == repo or repo in resolved.parents for repo in protected)

    return outside


def _record_hook_blocked(rule: str, session_id: str | None) -> None:
    """Best-effort friction record so DX-38's ladder counts this refusal.

    Mirrors scripts/split_guard.py's own helper of the same name -- loaded
    by file path, not `import friction`, since scripts/ is not a package. A
    recording failure must never break the guard itself, per
    scripts/friction.py's own contract.
    """
    path = Path(__file__).resolve().parent / "friction.py"
    spec = importutil.spec_from_file_location("_worktree_guard_friction", path)
    if spec is None or spec.loader is None:
        return
    friction = importutil.module_from_spec(spec)
    try:
        spec.loader.exec_module(friction)
        friction.record_event(
            friction.friction_log_path(Path(__file__).resolve().parent.parent),
            "hook_blocked",
            session_id=session_id,
            payload={"hook": "worktree_guard", "rule": rule},
        )
    except (OSError, ValueError):
        pass


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
        return bash_command_is_file_modifying(str(tool_input.get("command", "")), root)
    return False


def block_message(root: Path, reason: str | None = None) -> str:
    """BLOCK_MESSAGE, or -- when the session started inside one of `root`'s
    linked worktrees (CLAUDE_PROJECT_DIR) and only its shell cwd drifted back
    to the primary checkout -- the path to cd back into. A `reason` (the rule
    that fired and its token, AX-18) leads the message."""
    message = _session_block_message(root)
    return f"worktree_guard: {reason}.\n{message}" if reason else message


def _session_block_message(root: Path) -> str:
    project = os.environ.get("CLAUDE_PROJECT_DIR")
    if project:
        try:
            start = Path(project).resolve()
            primary = root.resolve()
            for worktree in _linked_worktree_paths(root):
                resolved = worktree.resolve()
                if resolved != primary and (start == resolved or resolved in start.parents):
                    return (
                        "Refusing to edit files in the primary checkout. This session's "
                        f"worktree already exists; cd back into it:\n\n    cd {resolved}\n\n"
                        "Then re-run this from inside it.\n"
                    )
        except OSError:
            pass
    return BLOCK_MESSAGE


def main(argv: list[str] | None = None) -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    if should_block(tool_name, tool_input, Path.cwd()):
        reason = (
            bash_refusal_reason(str(tool_input.get("command", "")), Path.cwd())
            if tool_name == "Bash"
            else None
        )
        print(block_message(Path.cwd(), reason), file=sys.stderr)
        _record_hook_blocked("primary_checkout", payload.get("session_id"))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
