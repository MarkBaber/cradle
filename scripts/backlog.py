#!/usr/bin/env python3
"""Canonical house backlog cockpit.

One stdlib-only file answering three questions: is this manifest legal (a
versioned validation contract, CI gate), is this repo sound (advisory
diagnostics), and what happens next (dispatch commands / fleet plan / which
house skill to reach for).

validate() is not a wrapper around scripts/validate_tasks.py, nor the other
way around -- both stay standalone, single-file tools deployable without the
other (see docs/adr/0024). A parity test in tests/unit/test_backlog.py keeps
the two implementations from silently diverging again.

    python3 scripts/backlog.py              validate. CI gate. exit 0/1
    python3 scripts/backlog.py --human      backlog report + diagnostics
    python3 scripts/backlog.py --doctor     repo health only
    python3 scripts/backlog.py --next       next task, copy-pasteable
    python3 scripts/backlog.py --fleet      parallel lane plan
    python3 scripts/backlog.py --agent      JSON for a toolcall
    python3 scripts/backlog.py --json-schema

Read-only and network-free by construction: this file never writes, never
runs `claude`, and never invokes git in write mode. See docs/adr/0003.

Maintained directly in this repo and expected to keep growing. The only
part of it that is a stability contract is the validation vocabulary
(STATUSES, ROUTINGS, bucket semantics, CONTRACT_VERSION below): it is
consumer-facing (cockpit.py, NightShift's BacklogManager both assert
against it), so widening or renaming it is an architect decision that
bumps CONTRACT_VERSION. Diagnostics, rendering, and dispatch-argv
construction are regular code -- improve them freely.

Internally layered L0..L5, banner-delimited, strictly one-directional: a
function may reference only names defined at its own layer or above. This
is a hand-maintained convention, not machine-checked -- tests/test_layers.py
enforces the src/nightshift/ layer graph, not this file's banners. Do not
reorder the banners.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any, NamedTuple

# =====================================================================
# L0  CONTRACT  --  the versioned public interface (CONTRACT_VERSION,
#     below). STATUSES, ROUTINGS, and bucket semantics are consumer-
#     facing: cockpit.py and NightShift's BacklogManager both assert
#     against them. Widening or renaming any of it bumps
#     CONTRACT_VERSION and is an architect decision -- not an ordinary
#     edit.
# =====================================================================

#: One [[task]] table as tomllib returns it. Deliberately not a dataclass:
#: the manifest is the schema, and a parallel class would be a second one.
Task = dict[str, Any]

REQUIRED = {"id", "phase", "title", "status", "routing", "depends", "touches",
            "description", "exit_criteria"}
STATUSES = {"todo", "in_progress", "review", "done", "blocked"}
ROUTINGS = {"architect", "impl", "tester", "reviewer"}

#: A task in one of these statuses is a candidate for dispatch.
SCHEDULABLE = frozenset({"todo"})
#: A dependency in one of these statuses no longer blocks its dependents.
#: `review` satisfies downstream by design -- see ADR-0001 of the house
#: scaffold: work awaiting merge must not stall the ready set.
SATISFIED = frozenset({"done", "review"})
#: Routings an agent may take unattended. architect/reviewer stay human-gated.
AUTONOMOUS = frozenset({"impl", "tester"})

#: Bumped when the vocabulary sets or bucket semantics change. Consumers
#: (fleet-dispatch ready_set.py, NightShift BacklogManager) assert against
#: this rather than re-implementing the sets. See ADR-0002.
CONTRACT_VERSION = 2


def validate(tasks: list[Task]) -> tuple[dict[str, Task], list[str]]:
    """Return (complete tasks by id, errors). Errors empty == manifest legal.

    Tasks with missing required fields are reported once and excluded from
    `by_id`, but their ids are still known for dependency resolution, so a
    single incomplete task does not cascade an `unknown dependency` error
    into every task that depends on it (ADR-0005).
    """
    errors: list[str] = []
    by_id: dict[str, Task] = {}
    known: set[str] = {t["id"] for t in tasks if isinstance(t.get("id"), str)}

    for t in tasks:
        missing = REQUIRED - t.keys()
        if missing:
            errors.append(f"{t.get('id', '<no id>')}: missing fields {sorted(missing)}")
            continue
        if t["id"] in by_id:
            errors.append(f"duplicate task id {t['id']}")
        else:
            # First declaration wins. The original clobbered it, so dependency
            # resolution silently used the later task's edges.
            by_id[t["id"]] = t
        if t["status"] not in STATUSES:
            errors.append(f"{t['id']}: bad status '{t['status']}' (allowed: {sorted(STATUSES)})")
        if t["routing"] not in ROUTINGS:
            errors.append(f"{t['id']}: bad routing '{t['routing']}' (allowed: {sorted(ROUTINGS)})")
        if not t["exit_criteria"]:
            errors.append(f"{t['id']}: empty exit_criteria")

    for t in by_id.values():
        for dep in t["depends"]:
            if dep not in known:
                errors.append(f"{t['id']}: unknown dependency '{dep}'")

    WHITE, GREY, BLACK = 0, 1, 2
    colour = {tid: WHITE for tid in by_id}
    for start in by_id:
        if colour[start] != WHITE:
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        colour[start] = GREY
        while stack:
            node, i = stack[-1]
            deps = [d for d in by_id[node]["depends"] if d in by_id]
            if i < len(deps):
                stack[-1] = (node, i + 1)
                nxt = deps[i]
                if colour[nxt] == GREY:
                    # One error per back edge. The original marked nxt BLACK
                    # here, which masked further cycles through the same node.
                    errors.append(f"dependency cycle through {nxt}")
                elif colour[nxt] == WHITE:
                    colour[nxt] = GREY
                    stack.append((nxt, 0))
            else:
                colour[node] = BLACK
                stack.pop()

    return by_id, errors


def classify(by_id: dict[str, Task]) -> dict[str, list[Task]]:
    """Bucket tasks. Every bucket is sorted by (phase, id).

    ready         schedulable, autonomously routed, dependencies satisfied
    needs_routing schedulable, dependencies satisfied, human-gated routing
    blocked       schedulable, dependencies not yet satisfied
    in_progress   claimed by an agent
    held          status == 'blocked' -- explicitly parked, needs a decision
    """
    done = {i for i, t in by_id.items() if t["status"] in SATISFIED}
    buckets: dict[str, list[Task]] = {
        "ready": [], "needs_routing": [], "blocked": [], "in_progress": [], "held": [],
    }
    for t in by_id.values():
        status = t["status"]
        if status == "in_progress":
            buckets["in_progress"].append(t)
        elif status == "blocked":
            buckets["held"].append(t)
        elif status in SCHEDULABLE:
            if not all(d in done for d in t["depends"]):
                buckets["blocked"].append(t)
            elif t["routing"] in AUTONOMOUS:
                buckets["ready"].append(t)
            else:
                buckets["needs_routing"].append(t)
    for tasks in buckets.values():
        tasks.sort(key=lambda t: (t["phase"], t["id"]))
    return buckets


# =====================================================================
# L1  DISCOVERY  --  every probe is failure-tolerant. A probe that cannot
#     run returns a sentinel; it never raises and never warns about itself.
# =====================================================================

#: First path segments treated as legitimate homes for `touches` entries.
SOURCE_ROOTS = ("src", "tests", "test", "scripts", "docs", "include", "assets",
                "cmake", "config", "Source", "Content", "Config", "Plugins",
                "benchmarks", "tools", "examples", "client", "cli", "docker",
                "systemd")
#: Root-level files a task may legitimately touch.
ROOT_FILES = ("tasks.toml", "CLAUDE.md", "README.md", ".gitlab-ci.yml",
              "pyproject.toml", "CMakeLists.txt", "setup.cfg", "Makefile",
              ".gitignore", "requirements.txt", "nightshift.toml",
              ".claude/settings.json")
#: Artefacts every mature house repo carries.
EXPECTED_ARTEFACTS = ("CLAUDE.md", "docs/SPEC.md", "docs/adr", ".gitlab-ci.yml",
                      "scripts/test")

META_DEFAULTS: dict[str, Any] = {
    "project": None, "kind": None, "verify": "./scripts/test",
    "branch_prefix": "task/", "lanes": 3,
}


def repo_root(override: str | None = None) -> Path:
    """Repo root: --repo if given, else the parent of this script's dir."""
    if override:
        return Path(override).resolve()
    return Path(__file__).resolve().parent.parent


def meta(data: dict[str, Any]) -> dict[str, Any]:
    """Optional [meta] overrides. Missing table -> defaults. Unknown keys are
    ignored, never warned about -- forward compatibility with the schema."""
    out = dict(META_DEFAULTS)
    table = data.get("meta")
    if isinstance(table, dict):
        for key in META_DEFAULTS:
            if key in table:
                out[key] = table[key]
    if not isinstance(out["lanes"], int) or out["lanes"] < 1:
        out["lanes"] = META_DEFAULTS["lanes"]
    return out


def layout(root: Path) -> dict[str, Any]:
    """Pure Path.exists() probing. No content parsing -- prose is not config."""
    present = {name: (root / name).exists() for name in EXPECTED_ARTEFACTS}
    roots = tuple(d for d in SOURCE_ROOTS if (root / d).is_dir())
    mtimes: dict[str, float] = {}
    for name in ("docs/SPEC.md", "docs/adr"):
        p = root / name
        if p.is_dir():
            stamps = [c.stat().st_mtime for c in sorted(p.glob("*.md"))]
            if stamps:
                mtimes[name] = max(stamps)
        elif p.exists():
            mtimes[name] = p.stat().st_mtime
    agents: list[str] = []
    agents_dir = root / ".claude" / "agents"
    if agents_dir.is_dir():
        for path in sorted(agents_dir.rglob("*.md")):
            agents.append(_agent_name(path))
    return {
        "artefacts": present,
        "source_roots": roots,
        "mtimes": mtimes,
        "preamble": (root / "scripts/.dispatch-preamble.md").exists(),
        "agents_dir": agents_dir.is_dir(),
        "agents": tuple(sorted(set(agents))),
    }


def _agent_name(path: Path) -> str:
    """The `name:` field of a subagent definition; identity comes from the
    field, not the filename. Falls back to the stem on any parse trouble --
    YAML frontmatter is a defined format, but this probe still must not raise."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines()[:15]:
            key, sep, value = line.partition(":")
            if sep and key.strip() == "name" and value.strip():
                return value.strip()
    except OSError:
        pass
    return path.stem


def git(root: Path, enabled: bool = True) -> dict[str, Any] | None:
    """Git porcelain, or None if unavailable for any reason whatsoever."""
    if not enabled:
        return None

    def run(*args: str) -> str | None:
        try:
            proc = subprocess.run(["git", "-C", str(root), *args],
                                  capture_output=True, text=True,
                                  timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.stdout if proc.returncode == 0 else None

    if run("rev-parse", "--is-inside-work-tree") is None:
        return None
    status = run("status", "--porcelain")
    refs = run("for-each-ref", "--format=%(refname:short)\t%(committerdate:unix)",
               "refs/heads/")
    merged = run("branch", "--merged", "HEAD", "--format=%(refname:short)")
    branches: dict[str, int] = {}
    for line in (refs or "").splitlines():
        name, _, stamp = line.partition("\t")
        if name and stamp.strip().isdigit():
            branches[name] = int(stamp)
    return {
        "dirty": bool((status or "").strip()),
        "dirty_count": len([ln for ln in (status or "").splitlines() if ln.strip()]),
        "branches": branches,
        "merged": {ln.strip() for ln in (merged or "").splitlines() if ln.strip()},
        "head": (run("rev-parse", "--abbrev-ref", "HEAD") or "").strip() or None,
    }


# =====================================================================
# L2  DIAGNOSTICS  --  advisory only. Findings never change the exit code
#     (except under opt-in --strict). See ADR-0004.
# =====================================================================

STALE_BRANCH_DAYS = 14
STUCK_TASK_DAYS = 3
REVIEW_PILEUP_AT = 4
DAY = 86400


class Finding(NamedTuple):
    code: str      # stable identifier; agents branch on it. Append-only.
    level: str     # 'info' | 'warn'. No 'error' -- errors are L0 validation.
    subject: str   # task id, path, or branch the finding is about
    message: str   # what is true
    hint: str      # what the operator can do about it


def _norm(path: str) -> str:
    """POSIX-normalised, no leading ./, no trailing /. Textual only: touches
    routinely name files that do not exist yet."""
    p = path.replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p.rstrip("/")


def _branch_for(task_id: str, branches: dict[str, int]) -> str | None:
    for name in sorted(branches):
        head = name.split("/", 1)[-1]
        if head == task_id or head.startswith(task_id + "-"):
            return name
    return None


def _task_for(branch: str, ids: list[str]) -> str | None:
    """Resolve a branch name to a task id. Ids contain hyphens (EX-01), so
    this matches against the declared ids rather than splitting on '-'.
    Longest match wins, so EX-01 never shadows EX-011."""
    head = branch.split("/", 1)[-1]
    hits = [i for i in ids if head == i or head.startswith(i + "-")]
    return max(hits, key=len) if hits else None


def doctor(root: Path, by_id: dict[str, Task], buckets: dict[str, list[Task]],
           lay: dict[str, Any], repo_git: dict[str, Any] | None,
           now: float | None = None) -> list[Finding]:
    """All eleven codes. A producer whose input is unavailable emits nothing."""
    now = time.time() if now is None else now
    out: list[Finding] = []

    for tid in sorted(by_id):
        task = by_id[tid]
        if task["status"] not in SATISFIED:
            continue
        for raw in sorted(task["touches"]):
            if not (root / _norm(raw)).exists():
                out.append(Finding(
                    "TOUCH-MISSING", "warn", tid,
                    f"{tid} is {task['status']} but {_norm(raw)} does not exist",
                    "The task claims work that left no artefact -- verify the "
                    "status, or correct `touches`."))

    if lay["source_roots"]:
        allowed = set(lay["source_roots"])
        for tid in sorted(by_id):
            for raw in sorted(by_id[tid]["touches"]):
                path = _norm(raw)
                if path in ROOT_FILES or path.split("/", 1)[0] in allowed:
                    continue
                out.append(Finding(
                    "TOUCH-ORPHAN", "warn", tid,
                    f"{tid} touches {path}, outside every known source root",
                    f"Known roots: {', '.join(sorted(allowed))}. Typo, or a new "
                    "root that should be created."))

    for name in EXPECTED_ARTEFACTS:
        if not lay["artefacts"][name]:
            out.append(Finding(
                "ARTEFACT-MISSING", "warn", name,
                f"{name} is absent",
                "House scaffolds carry it; agents read it. Generate with the "
                "matching skill (adr-scaffold, scaffold-toolkit)."))

    if not lay["agents_dir"]:
        out.append(Finding(
            "AGENTS-MISSING", "info", ".claude/agents",
            "no subagent definitions in this repo",
            "Emitted prompts fall back to generic delegation wording. Copy "
            "the scaffold-toolkit agents/ asset set into .claude/agents/ "
            "(restart any running session afterwards -- a new agents "
            "directory is not picked up mid-session)."))

    spec_m, adr_m = lay["mtimes"].get("docs/SPEC.md"), lay["mtimes"].get("docs/adr")
    if spec_m is not None and adr_m is not None and adr_m < spec_m:
        out.append(Finding(
            "ADR-STALE", "info", "docs/adr",
            "docs/SPEC.md is newer than the newest ADR",
            "A spec change with no decision record. Run adr-scaffold if a "
            "decision moved."))

    if repo_git is not None:
        if repo_git["dirty"]:
            out.append(Finding(
                "GIT-DIRTY", "warn", repo_git["head"] or "HEAD",
                f"{repo_git['dirty_count']} uncommitted change(s) in the work tree",
                "Commit or stash before dispatching agents -- they will "
                "branch from a dirty tree."))
        ids = sorted(by_id)
        for name in sorted(repo_git["branches"]):
            if not name.startswith("task/"):
                continue
            owner = _task_for(name, ids)
            age = (now - repo_git["branches"][name]) / DAY
            held_by = by_id.get(owner) if owner else None
            if held_by is not None and held_by["status"] == "done" \
                    and name in repo_git["merged"]:
                out.append(Finding(
                    "GIT-BRANCH-ORPHAN", "warn", name,
                    f"{name} is merged and {owner} is done",
                    f"Cleanup candidate: git branch -d {name}"))
            elif age > STALE_BRANCH_DAYS:
                out.append(Finding(
                    "GIT-BRANCH-STALE", "warn", name,
                    f"{name} has no commit in {int(age)} days",
                    "Abandoned work. Merge, rebase, or delete it."))
        for task in buckets["in_progress"]:
            tid = task["id"]
            branch = _branch_for(tid, repo_git["branches"])
            if branch is None:
                out.append(Finding(
                    "GIT-BRANCH-MISSING", "warn", tid,
                    f"{tid} is in_progress but no matching branch exists",
                    "Either the agent never started, or the status is stale. "
                    "Reset it to todo to return it to the ready set."))
            else:
                age = (now - repo_git["branches"][branch]) / DAY
                if age > STUCK_TASK_DAYS:
                    out.append(Finding(
                        "TASK-STUCK", "warn", tid,
                        f"{tid} in_progress; {branch} last moved {int(age)} days ago",
                        "Likely a dead session. Reclaim the task or finish it."))

    reviewing = sorted(t["id"] for t in by_id.values() if t["status"] == "review")
    if len(reviewing) >= REVIEW_PILEUP_AT:
        out.append(Finding(
            "REVIEW-PILEUP", "warn", ", ".join(reviewing),
            f"{len(reviewing)} tasks awaiting review",
            "Merge or close them: unreviewed work starves the ready set."))

    phases = sorted({t["phase"] for t in by_id.values()})
    for phase in phases:
        prior = [t for t in by_id.values() if t["phase"] == phase - 1]
        if prior and not any(t["status"] == "done" for t in prior):
            out.append(Finding(
                "PHASE-GAP", "warn", f"phase {phase}",
                f"phase {phase} has tasks while phase {phase - 1} has none done",
                "Phases are risk-ordered. Finishing phase "
                f"{phase - 1} first is usually the cheaper path."))

    return sorted(out, key=lambda f: (f.code, f.subject))


# =====================================================================
# L3  PLANNING  --  computes decisions and argv. Renders nothing.
# =====================================================================

#: Architect skill to name when a repo needs designing rather than dispatching.
ARCHITECT_SKILL = {
    "python": "agentic-project-architect", "cpp": "agentic-project-architect",
    "mixed": "agentic-project-architect", "cuda": "cuda-kernel-architect",
    "ue5": "ue5-game-architect",
}
MAX_TURNS = 40
MODEL = "sonnet"
#: Selectable `--model` values for dispatch commands. Short CLI aliases, not
#: full model ids -- consistent with MODEL above.
MODELS = ("sonnet", "opus", "haiku")
#: One-line rationale per MODELS value, surfaced as a tooltip by the cockpit's
#: dispatch picker. Not consulted by build_command itself.
MODEL_HELP: dict[str, str] = {
    "sonnet": "Claude Sonnet 5. Recommended default for general-purpose "
              "implementation and test-writing tasks.",
    "opus": "Claude Opus 5. Most capable and most expensive -- reserve for "
            "hard, ambiguous, or high-stakes tasks.",
    "haiku": "Claude Haiku 4.5. Fastest and cheapest -- best for small, "
             "well-scoped, low-risk tasks.",
}

#: agy's default model when none is requested.
AGY_MODEL = "gemini-3.6-flash-medium"
#: Default model for Antigravity's 'other' (bundled allotment) model family.
AGY_OTHER_MODEL = "claude-sonnet-4-6"
#: agy's flat model-slug vocabulary (`agy models`, captured verbatim by AG-01
#: -- see docs/antigravity-cli-contract.md). A separate list from MODELS, not
#: merged into it: agy exits non-zero on an unrecognised slug, so the cockpit
#: picker (AG-10) must offer only the slugs the selected backend accepts.
AGY_MODELS = (
    "gemini-3.6-flash-high", "gemini-3.6-flash-medium", "gemini-3.6-flash-low",
    "gemini-3.5-flash-high", "gemini-3.5-flash-medium", "gemini-3.5-flash-low",
    "gemini-3.1-pro-high", "gemini-3.1-pro-low",
    "claude-sonnet-4-6", "claude-opus-4-6-thinking", "gpt-oss-120b-medium",
)
#: One-line rationale per AGY_MODELS value, same role as MODEL_HELP.
AGY_MODEL_HELP: dict[str, str] = {
    "gemini-3.6-flash-high": "Gemini 3.6 Flash, high effort. agy's own "
                             "default model.",
    "gemini-3.6-flash-medium": "Gemini 3.6 Flash, medium effort.",
    "gemini-3.6-flash-low": "Gemini 3.6 Flash, low effort -- fastest, "
                            "cheapest Gemini option.",
    "gemini-3.5-flash-high": "Gemini 3.5 Flash, high effort.",
    "gemini-3.5-flash-medium": "Gemini 3.5 Flash, medium effort.",
    "gemini-3.5-flash-low": "Gemini 3.5 Flash, low effort.",
    "gemini-3.1-pro-high": "Gemini 3.1 Pro, high effort -- most capable "
                           "Gemini option.",
    "gemini-3.1-pro-low": "Gemini 3.1 Pro, low effort.",
    "claude-sonnet-4-6": "Claude Sonnet, reachable through Antigravity's "
                         "bundled allotment -- a separate quota from the "
                         "native claude backend.",
    "claude-opus-4-6-thinking": "Claude Opus with extended thinking, via "
                                "Antigravity's bundled allotment.",
    "gpt-oss-120b-medium": "Open-weight GPT-OSS 120B, medium effort -- the "
                           "bundled allotment's non-Gemini, non-Claude "
                           "option.",
}

#: Selectable models, keyed by backend (ADR 0018): the cockpit picker (AG-10)
#: must offer only the slugs the selected backend accepts, and the two
#: vocabularies must never merge -- agy exits non-zero on an unknown slug,
#: and so would claude on an agy slug.
BACKEND_MODELS: dict[str, tuple[str, ...]] = {"claude": MODELS, "antigravity": AGY_MODELS}
BACKEND_MODEL_HELP: dict[str, dict[str, str]] = {
    "claude": MODEL_HELP, "antigravity": AGY_MODEL_HELP,
}

#: agy's own --print-timeout default (5 minutes) is far below what a real
#: dispatch needs -- AG-04 hits the identical problem in the executor path.
#: Passed explicitly on every antigravity invocation, matching the largest
#: (L) size-class budget AG-04's notes cite, so a real task is never killed
#: at the CLI's own default.
AGY_PRINT_TIMEOUT = "60m"

#: Selectable `--effort` values for dispatch commands. "default" means the
#: flag is omitted and the CLI's own default applies -- which for Claude Code
#: is `xhigh`, not the midpoint the name suggests. Naming a level below that
#: is therefore a step *down* from omitting the flag; only "max" is a step up.
EFFORT_LEVELS = ("default", "low", "medium", "high", "xhigh", "max")
#: One-line rationale per EFFORT_LEVELS value, surfaced as a tooltip by the
#: cockpit's dispatch picker.
EFFORT_HELP: dict[str, str] = {
    "default": "No --effort flag -- Claude Code's own default, which is xhigh.",
    "low": "Fastest and cheapest. Simple, mechanical changes.",
    "medium": "Balanced speed and thoroughness. A reasonable default when "
              "in doubt.",
    "high": "More deliberate reasoning. Multi-step or judgment-heavy work.",
    "xhigh": "Extra thorough. Complex or high-stakes changes, and the best "
             "setting for most coding and agentic work.",
    "max": "Maximum reasoning effort. Slowest and priciest -- hardest tasks "
           "only.",
}
#: Claude models that accept --effort at all. Haiku 4.5 has no effort control
#: and the flag is an error against it rather than a no-op, so a haiku
#: dispatch must omit it -- the same non-widening posture build_command
#: already takes for a model slug belonging to the wrong backend.
EFFORT_MODELS = ("sonnet", "opus")

#: House subagents, in the order a task moves through them. Values are the
#: fallback phrasing used when the definition is not present in the repo.
#: `Explore` is a Claude Code built-in and is never a fallback case; the house
#: does not redefine it, because a custom agent that duplicates a built-in is
#: one more thing to keep in step.
HOUSE_AGENTS: dict[str, str] = {
    "test-author": "a subagent",
    "gate-runner": "a subagent",
    "failure-analyst": "a read-only subagent",
    "house-reviewer": "a read-only subagent",
    "criteria-auditor": "a read-only subagent",
    "adr-scribe": "a subagent",
}
#: Above this many touch paths, discovery is worth isolating from the main
#: context. Below it, reading the files inline is cheaper than a round trip.
DELEGATE_TOUCH_THRESHOLD = 3
SUBAGENT_MODES = ("auto", "always", "never")
#: One-line rationale per SUBAGENT_MODES value, surfaced as a tooltip by the
#: cockpit's dispatch picker.
SUBAGENT_MODE_HELP: dict[str, str] = {
    "auto": "House heuristic delegates steps by task shape (touch count, "
            "docs/tests/ADR work). The default -- right for most tasks.",
    "always": "Delegate every step to a subagent, even on small tasks. More "
              "round trips, but a fuller audit trail.",
    "never": "Keep every step in the main session. No subagent calls at all.",
}

#: ADR 0018's parity ledger: house features with no non-claude-backend
#: translation, named here rather than silently omitted. AG-12's parity gate
#: reads this -- a feature that gains a real translation is removed from
#: here, not added to it. AG-01's spike (docs/antigravity-cli-contract.md)
#: found no agy mechanism for naming or invoking a custom subagent: --agent
#: and the `agent`/`agents` subcommands select or list agy's own built-in
#: personas for a whole session, not a way to name a HOUSE_AGENTS-style
#: subagent mid-task. Delegation is suppressed for antigravity, equivalent to
#: subagents="never", rather than translated.
PARITY_GAPS: dict[str, dict[str, str]] = {
    "antigravity": {
        "delegation": "no agy subagent mechanism found (AG-01); "
                      "HOUSE_AGENTS delegation is suppressed, not translated",
    },
}


class Lane(NamedTuple):
    # `index` shadows tuple.index. The name is part of the documented --agent
    # payload shape, so it stays; the method is never called on a Lane.
    index: int  # type: ignore[assignment]
    tasks: list[str]
    touches: list[str]
    command: list[str]


class Deferred(NamedTuple):
    task: str
    lane: int      # the lane this task must wait behind
    reason: str    # 'collision' (shares paths) | 'cap' (no free lane)


class NextAction(NamedTuple):
    kind: str            # decision-table branch
    reason: str          # why this branch, in operator language
    command: list[str] | None
    skill: str | None    # exactly one of command/skill may be None, never both


def _slug(text: str, cap: int = 40) -> str:
    """Lowercase hyphenated slug, hard-capped at `cap` characters."""
    out: list[str] = []
    length = 0
    for raw in text.lower().split():
        word = "".join(c for c in raw if c.isalnum())
        if not word:
            continue
        extra = len(word) + (1 if out else 0)
        if length + extra > cap:
            break
        out.append(word)
        length += extra
    return "-".join(out or ["task"])


def conflicts(a: str, b: str) -> bool:
    """True if two touch paths overlap. Directory prefix counts: src/api/
    conflicts with src/api/routes.py. Textual, no filesystem resolution."""
    x, y = _norm(a), _norm(b)
    return x == y or x.startswith(y + "/") or y.startswith(x + "/")


def _agent_ref(name: str, lay: dict[str, Any]) -> str:
    """Name the subagent when the repo defines it, otherwise describe the shape
    of agent wanted. A prompt naming an agent that does not exist sends the
    session looking for something it will not find."""
    if name == "Explore" or name in lay["agents"]:
        return f"the {name} subagent"
    return HOUSE_AGENTS.get(name, "a subagent")


def delegation(task: Task, cfg: dict[str, Any], lay: dict[str, Any],
               mode: str = "auto") -> list[tuple[str, str]]:
    """Which steps of this task are worth handing to a subagent.

    The test is context economy, not tidiness: delegate work whose *output* is
    bulky and whose *answer* is small -- discovery, gate runs, failure traces,
    audits. Implementation is the opposite shape, so it stays in the main
    session; a summary of code you are about to edit is worse than the code.
    """
    if mode == "never":
        return []
    always = mode == "always"
    touches = [_norm(p) for p in task["touches"]]
    criteria = " ".join(task["exit_criteria"]).lower()
    # A directory touch has unbounded breadth: one entry, unknown file count.
    directory = any("." not in p.rsplit("/", 1)[-1] for p in touches)
    wide = len(touches) >= DELEGATE_TOUCH_THRESHOLD or directory
    docs_work = any(p.startswith("docs/") for p in touches)
    steps: list[tuple[str, str]] = []

    if always or wide or docs_work or "spec" in criteria or "adr" in criteria:
        steps.append(("Explore",
                      "map the code you are about to change and report back "
                      "the call sites, the conventions in use, and anything "
                      "that contradicts the task description. Explore does "
                      "not load CLAUDE.md, so restate any house rule it needs "
                      "in the delegation prompt."))
    if always or task["routing"] == "tester" or any(
            p.startswith(("tests/", "test/")) for p in touches):
        steps.append(("test-author",
                      "turn each exit criterion into a failing test before "
                      "any implementation exists."))
    steps.append(("gate-runner",
                  f"run {cfg['verify']} and report only the failures. Do not "
                  "let the full output back into this session."))
    steps.append(("failure-analyst",
                  "if the gate fails, get the root cause and the smallest "
                  "viable fix. Apply the fix yourself; do not delegate edits."))
    if always or task["routing"] == "reviewer" or wide:
        steps.append(("house-reviewer",
                      "review the diff against CLAUDE.md and the layering "
                      "rules before you hand the task on."))
    steps.append(("criteria-auditor",
                  "confirm every exit criterion is met and that nothing "
                  "outside the touches list changed. Do this before you touch "
                  "the status field."))
    if always or task["routing"] == "architect" or any(
            p.startswith("docs/adr") for p in touches):
        steps.append(("adr-scribe",
                      "record any decision made here as an ADR, including the "
                      "alternatives that were rejected and why."))
    return steps


def build_prompt(task: Task, cfg: dict[str, Any], lay: dict[str, Any],
                 branch: str, subagents: str = "auto",
                 backend: str = "claude") -> str:
    """The dispatch prompt. Carries exit_criteria verbatim -- an agent that
    paraphrases its own definition of done has no definition of done."""
    # No apostrophes or single quotes anywhere below: the whole prompt is one
    # single-quoted shell argument, and every ' in it becomes '"'"' on paste.
    lines = [
        f"Implement task {task['id']} from tasks.toml: {task['title']}",
        "",
        task["description"],
        "",
    ]
    notes = (task.get("notes") or "").strip()
    if notes:
        lines += [
            "Notes recorded on this task in tasks.toml -- read these first: "
            "they may be feedback from a prior review that this run must "
            "address, not just background context.",
            f"  {notes}",
            "",
        ]
    lines += [
        "Exit criteria (every one must pass before you stop):",
        *[f"  - {c}" for c in task["exit_criteria"]],
        "",
        "Constraints:",
        f"  - Touch only these paths: {', '.join(task['touches'])}",
        f"  - Work on branch {branch}; create it from the current HEAD if absent.",
        f"  - Verify with: {cfg['verify']}",
        "  - Then run: python3 scripts/backlog.py  (must exit 0)",
        "  - When the exit criteria pass, set the status of this task to"
        " review in tasks.toml. Never set it to done -- only CI and a human"
        " reviewer do that.",
        "  - Follow CLAUDE.md. Minimal diffs, no speculative refactors, no"
        " changes outside the paths above.",
    ]
    steps = [] if "delegation" in PARITY_GAPS.get(backend, {}) \
        else delegation(task, cfg, lay, subagents)
    if steps:
        lines += ["", "Delegation (keep this context clean -- verbose work "
                  "belongs in a subagent, edits belong here):"]
        lines += [f"  - {_agent_ref(name, lay)}: {what}" for name, what in steps]
        lines.append("  - Everything else, including the implementation "
                     "itself, stays in this session.")
    return "\n".join(lines).replace("'", "’")


def build_command(task: Task, cfg: dict[str, Any], lay: dict[str, Any],
                  mode: str = "next", budget: float | None = None,
                  subagents: str = "auto", dangerous: bool = False,
                  model: str | None = None,
                  effort: str | None = None,
                  backend: str = "claude") -> list[str]:
    """argv for one task. Never a shell string -- the consumer owns quoting.

    `backend` selects the CLI this argv targets (ADR 0015/0017); every
    existing caller omits it and gets the claude behaviour below, unchanged.

    claude, mode 'next'  -> print mode, budget-capped, single shot.
    claude, mode 'fleet' -> background session in a Claude Code worktree.
    acceptEdits is the ceiling unless the operator opts into --dangerous,
    which emits --dangerously-skip-permissions instead, in every mode.

    antigravity -> always print mode: `claude --bg -w` has no documented agy
    counterpart (ADR 0017), so 'fleet' collapses onto the same argv as 'next'
    and cockpit's existing detached dispatch() carries the backgrounding.
    --print-timeout is always explicit (see AGY_PRINT_TIMEOUT). --effort,
    --max-turns, --add-dir, --append-system-prompt-file and --max-budget-usd
    are claude-only flags (AG-01's captured `agy --help` has no equivalents)
    and are never emitted here.

    `model` is an explicit override; absent that, the task's own `model`
    field (tasks.toml may set one per task) wins, else the backend's own
    default (MODEL for claude, AGY_MODEL for antigravity). Whichever model is
    chosen must belong to `backend`'s own vocabulary (BACKEND_MODELS) -- a
    claude slug offered to antigravity or vice versa is a hard failure here,
    not a silent fallback, the same non-widening posture AG-05 takes for tool
    grants (agy would exit non-zero on an unknown slug regardless).
    `effort` is a dispatch-time-only choice (see EFFORT_LEVELS) -- unlike
    `model`, tasks.toml carries no per-task field for it. None or "default"
    omits --effort, so the CLI's own default applies. A named level against a
    model with no effort control (EFFORT_MODELS) is a hard failure here for
    the same reason a wrong-backend slug is: the CLI rejects the flag, so
    emitting it anyway trades a clear error here for a failed dispatch later.
    Ignored entirely for the antigravity backend.
    """
    allowed_models = BACKEND_MODELS.get(backend)
    if allowed_models is None:
        raise ValueError(f"unknown backend {backend!r} "
                         f"(expected one of {sorted(BACKEND_MODELS)})")
    default_model = MODEL if backend == "claude" else AGY_MODEL
    chosen_model = model or task.get("model") or default_model
    if chosen_model not in allowed_models:
        raise ValueError(f"model {chosen_model!r} is not a {backend!r} slug "
                         f"(expected one of {allowed_models})")

    branch = f"{cfg['branch_prefix']}{task['id']}-{_slug(task['title'])}"
    prompt = build_prompt(task, cfg, lay, branch, subagents, backend)

    if backend == "antigravity":
        perm = ["--dangerously-skip-permissions"] if dangerous \
            else ["--mode", "accept-edits"]
        # `-p`/`--prompt` takes the prompt as its own value (AG-01's spike,
        # docs/antigravity-cli-contract.md #1, matched by agy_runner.py's
        # tested _build_argv) -- unlike claude's `-p`, which is a bare mode
        # switch with the prompt as a trailing positional. `-p` must stay the
        # last flag, directly before the prompt, so downstream splicing
        # (_apply_agy_permission_mode) can keep the pair adjacent.
        return ["agy", "--model", chosen_model, *perm,
                "--output-format", "text", "--print-timeout", AGY_PRINT_TIMEOUT,
                "-p", prompt]

    perm = ["--dangerously-skip-permissions"] if dangerous \
        else ["--permission-mode", "acceptEdits"]
    if mode == "fleet":
        # --bg cannot be combined with -p, so a fleet is n background sessions.
        argv = ["claude", "--bg", "-w", f"{task['id']}-{_slug(task['title'], 24)}",
                "--model", chosen_model, *perm]
    else:
        argv = ["claude", "-p", "--model", chosen_model, *perm,
                "--max-turns", str(MAX_TURNS), "--add-dir", "."]
        if lay["preamble"]:
            argv += ["--append-system-prompt-file", "scripts/.dispatch-preamble.md"]
    if budget is not None:
        argv += ["--max-budget-usd", str(budget)]
    if effort and effort != "default":
        if chosen_model not in EFFORT_MODELS:
            raise ValueError(f"model {chosen_model!r} does not accept --effort "
                             f"(expected one of {EFFORT_MODELS})")
        argv += ["--effort", effort]
    return [*argv, prompt]


def components(ready: list[Task]) -> list[list[Task]]:
    """Group the ready set into must-serialize clusters: two tasks whose
    touches conflict land in the same cluster. Clusters are pairwise
    non-conflicting by construction, so they are safe to run concurrently.
    Order is the (phase, id) order of each cluster's earliest task."""
    groups: list[list[Task]] = []
    paths: list[list[str]] = []
    for task in ready:
        mine = [_norm(p) for p in task["touches"]]
        hits = [i for i, held in enumerate(paths)
                if any(conflicts(p, q) for p in mine for q in held)]
        if not hits:
            groups.append([task])
            paths.append(list(mine))
            continue
        keep, *merge = hits
        groups[keep].append(task)
        paths[keep].extend(mine)
        for i in reversed(merge):          # transitive closure: this task may
            groups[keep].extend(groups[i])  # bridge two previously separate
            paths[keep].extend(paths[i])    # clusters
            del groups[i], paths[i]
    for group in groups:
        group.sort(key=lambda t: (t["phase"], t["id"]))
    return groups


def lanes(ready: list[Task], cfg: dict[str, Any], lay: dict[str, Any],
          cap: int | None = None, budget: float | None = None,
          subagents: str = "auto", dangerous: bool = False,
          model: str | None = None) -> tuple[list[Lane], list[Deferred]]:
    """Pack must-serialize clusters into at most `cap` concurrent lanes.

    Deterministic, no optimality claim. Lane touch sets are pairwise disjoint:
    two agents never hold the same path, because conflicting tasks share a
    lane and run in sequence. Anything that cannot start immediately is
    reported as deferred with the reason it must wait.
    """
    width = cap if cap is not None else cfg["lanes"]
    clusters = components(ready)
    assigned: list[list[Task]] = []
    held: list[list[str]] = []
    deferred: list[Deferred] = []
    for cluster in clusters:
        paths = [_norm(p) for t in cluster for p in t["touches"]]
        if len(assigned) < width:
            index = len(assigned)
            assigned.append(list(cluster))
            held.append(paths)
        else:
            # Cap reached: queue behind the shortest lane. Safe -- the cluster
            # conflicts with nothing already there, it simply waits its turn.
            index = min(range(len(assigned)), key=lambda i: (len(assigned[i]), i))
            assigned[index].extend(cluster)
            held[index].extend(paths)
            for task in cluster:
                deferred.append(Deferred(task["id"], index, "cap"))
        for task in cluster[1:]:
            deferred.append(Deferred(task["id"], index, "collision"))
    out = [Lane(i, [t["id"] for t in group], sorted(set(held[i])),
                build_command(group[0], cfg, lay, "fleet", budget, subagents,
                              dangerous, model))
           for i, group in enumerate(assigned)]
    return out, sorted(deferred, key=lambda d: (d.lane, d.task))


def next_action(has_manifest: bool, by_id: dict[str, Task],
                buckets: dict[str, list[Task]], cfg: dict[str, Any],
                lay: dict[str, Any], repo_git: dict[str, Any] | None,
                budget: float | None = None,
                subagents: str = "auto", dangerous: bool = False,
                model: str | None = None) -> NextAction:
    """Decision table. Evaluated in order, first match wins, exactly one
    action returned. Each row is commented with its rationale below."""
    skill = ARCHITECT_SKILL.get(cfg["kind"] or "", "agentic-project-architect")

    if not has_manifest:                                                  # 1
        return NextAction("architect", "No tasks.toml here -- nothing to "
                          "dispatch until the backlog exists.", None,
                          "requirements-interviewer")
    if not by_id:                                                         # 3
        return NextAction("architect", "tasks.toml is valid but empty.", None,
                          f"requirements-interviewer, then {skill}")
    if buckets["ready"]:                                                  # 4
        task = buckets["ready"][0]
        return NextAction("dispatch",
                          f"{task['id']} ({task['title']}) is ready: phase "
                          f"{task['phase']}, routing {task['routing']}, "
                          "dependencies satisfied.",
                          build_command(task, cfg, lay, "next", budget, subagents,
                                        dangerous, model),
                          None)
    if buckets["in_progress"]:                                            # 5
        names = ", ".join(t["id"] for t in buckets["in_progress"])
        return NextAction("wait", f"Nothing ready; {names} in progress. Let "
                          "them land, or reclaim a stuck one.", None, None)
    review = sorted(t["id"] for t in by_id.values() if t["status"] == "review")
    if review:                                                            # 6
        return NextAction("review", "Nothing ready; the ready set is starving "
                          f"behind unmerged work: {', '.join(review)}. Merge or "
                          "close, then re-run.", None, None)
    if buckets["needs_routing"]:                                          # 7
        names = ", ".join(f"{t['id']} ({t['routing']})"
                          for t in buckets["needs_routing"])
        return NextAction("route", "Nothing autonomously dispatchable. Human-"
                          f"gated work is next: {names}.", None, None)
    if buckets["blocked"]:                                                # 8
        names = ", ".join(t["id"] for t in buckets["blocked"])
        return NextAction("deadlock", f"{names} blocked with nothing in flight "
                          "to unblock them -- suspect a bad `depends` edge.",
                          None, None)
    if buckets["held"]:                                                   # 9
        names = ", ".join(t["id"] for t in buckets["held"])
        return NextAction("unblock", f"{names} explicitly held. They need a "
                          "decision before anything else moves.", None, None)
    if lay["artefacts"]["docs/adr"]:                                      # 10
        return NextAction("next-phase", "All tasks done. Architect the next "
                          "phase.", None, skill)
    return NextAction("next-phase", "All tasks done, but the rationale was "
                      "never logged.", None, "adr-scaffold")


def recommend(action: NextAction) -> str:
    """The one-line `recommendation` string of the --agent payload."""
    if action.command:
        return f"{action.kind}: {action.reason}"
    if action.skill:
        return f"{action.kind}: {action.reason} Use the {action.skill} skill."
    return f"{action.kind}: {action.reason}"


# =====================================================================
# L4  RENDER  --  formats what L0-L3 computed. Computes nothing.
# =====================================================================

WIDTH = 80          # fixed: output is diffable between runs and terminals
TL, TR, BL, BR, H, V = "\u256d", "\u256e", "\u2570", "\u256f", "\u2500", "\u2502"
FULL, EMPTY = "\u2588", "\u2591"
DOT, ARROW = "\u25cf", "\u2192"
ANSI_RE = re.compile(r"\033\[[0-9;]*m")

ANSI = {"reset": "\033[0m", "dim": "\033[2m", "bold": "\033[1m",
        "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
        "blue": "\033[34m", "cyan": "\033[36m",
        "id": "\033[1;35m"}  # bold magenta -- the one accent reserved for
                             # identifiers (task ids, finding codes, branch
                             # names), kept off the bucket palette so it never
                             # collides with a status colour.
BUCKET_ORDER = ("ready", "in_progress", "needs_routing", "blocked", "held")
BUCKET_COLOUR = {"ready": "green", "in_progress": "cyan", "needs_routing": "blue",
                 "blocked": "yellow", "held": "red"}


def use_colour(stream: Any = None) -> bool:
    stream = stream or sys.stdout
    return bool(getattr(stream, "isatty", lambda: False)()) and not os.environ.get("NO_COLOR")


def paint(text: str, name: str, on: bool) -> str:
    return f"{ANSI[name]}{text}{ANSI['reset']}" if on and name in ANSI else text


def render_validation(by_id: dict[str, Task], errors: list[str]) -> tuple[str, int]:
    """The frozen no-arg output. Byte-identical to validate_tasks.py."""
    if errors:
        return "\n".join(["tasks.toml INVALID:", *[f"  - {e}" for e in errors]]), 1
    return f"OK: {len(by_id)} tasks, graph acyclic, schema complete.", 0


def visible(text: str) -> int:
    """Printable width. Padding computed on raw length would be wrong the
    moment colour is on, and every box in this file would skew."""
    return len(ANSI_RE.sub("", text))


def clip(text: str, width: int) -> str:
    """Truncate to `width` printable characters. Only ever called on
    uncoloured text, so it does not have to survive splitting an escape."""
    return text if len(text) <= width else text[:max(0, width - 1)] + "\u2026"


def rule(left: str, right: str = "", colour: bool = False) -> str:
    """A section rule: left label, dashes, optional right label."""
    lhs = f"{H}{H} {paint(left, 'bold', colour)} " if left else H * 3
    rhs = f" {paint(right, 'dim', colour)} {H}{H}" if right else ""
    fill = max(0, WIDTH - visible(lhs) - visible(rhs))
    return f"{lhs}{H * fill}{rhs}"


def box(rows: list[str], title: str = "", right: str = "",
        colour: bool = False) -> list[str]:
    """A framed block, exactly WIDTH columns on every line."""
    lhs = f"{TL}{H} {paint(title, 'bold', colour)} " if title else TL + H
    rhs = f" {paint(right, 'dim', colour)} {H}" if right else H
    fill = max(0, WIDTH - visible(lhs) - visible(rhs) - 1)
    out = [f"{lhs}{H * fill}{rhs}{TR}"]
    for row in rows:
        if visible(row) > WIDTH - 4:
            # Never let a long row break the frame. Colour is dropped first,
            # because clipping mid-escape would leak the sequence into stdout.
            row = clip(ANSI_RE.sub("", row), WIDTH - 4)
        pad = max(0, WIDTH - visible(row) - 4)
        out.append(f"{V} {row}{' ' * pad} {V}")
    out.append(BL + H * (WIDTH - 2) + BR)
    return out


def bar(segments: list[tuple[int, str]], width: int = 28,
        colour: bool = False) -> str:
    """A proportional bar. Every non-empty segment gets at least one cell, so
    a single held task does not vanish into a rounding error."""
    total = sum(n for n, _ in segments)
    if not total:
        return paint(EMPTY * width, "dim", colour)
    cells, used = [], 0
    for i, (count, name) in enumerate(segments):
        if not count:
            continue
        size = width - used if i == len(segments) - 1 else max(
            1, round(count / total * width))
        size = min(size, max(0, width - used))
        cells.append(paint(FULL * size, name, colour))
        used += size
    if used < width:
        cells.append(paint(EMPTY * (width - used), "dim", colour))
    return "".join(cells)


def render_doctor(findings: list[Finding], colour: bool = False) -> str:
    if not findings:
        return "\n".join(box([paint("No findings. Repo looks sound.", "green", colour)],
                             "DOCTOR", colour=colour))
    rows = []
    for f in findings:
        mark = paint("!" if f.level == "warn" else "i",
                     "yellow" if f.level == "warn" else "blue", colour)
        code = clip(f.code, 20)
        rows.append(f"{mark} {paint(code, 'id', colour)}"
                    f"  {clip(f.message, WIDTH - 10 - len(code))}")
        rows.append(f"  {paint(clip(f.hint, WIDTH - 8), 'dim', colour)}")
    warns = sum(1 for f in findings if f.level == "warn")
    return "\n".join(box(rows, "DOCTOR",
                         f"{len(findings)} finding(s), {warns} warn", colour))


def render_human(cfg: dict[str, Any], by_id: dict[str, Task],
                 buckets: dict[str, list[Task]], findings: list[Finding],
                 lane_plan: list[Lane], deferred: list[Deferred],
                 action: NextAction, repo_git: dict[str, Any] | None,
                 colour: bool = False) -> str:
    counts = {k: len(v) for k, v in buckets.items()}
    done = sum(1 for t in by_id.values() if t["status"] == "done")
    review = sum(1 for t in by_id.values() if t["status"] == "review")
    segments = [(done, "green"), (review, "cyan"), (counts["ready"], "blue"),
                (counts["in_progress"], "yellow"),
                (counts["blocked"] + counts["held"], "red")]
    pct = round(100 * done / len(by_id)) if by_id else 0
    tally = "  ".join([
        paint(f"{done} done", "green", colour),
        paint(f"{review} review", "cyan", colour),
        paint(f"{counts['ready']} ready", "blue", colour),
        paint(f"{counts['in_progress']} wip", "yellow", colour),
        paint(f"{counts['blocked'] + counts['held']} stuck", "red", colour)])
    head = cfg["kind"] or "kind?"
    if repo_git and repo_git["head"]:
        head += f" {DOT} {repo_git['head']}"
    lines = box([f"{bar(segments, width=24, colour=colour)}  {pct}% done", tally],
                cfg["project"] or "(unnamed project)",
                f"{head} {DOT} {len(by_id)} tasks", colour)

    for bucket in BUCKET_ORDER:
        if not buckets[bucket]:
            continue
        lines += ["", rule(bucket.replace("_", " ").upper(),
                           str(len(buckets[bucket])), colour)]
        for t in buckets[bucket]:
            note = ""
            if bucket == "blocked":
                waiting = sorted(d for d in t["depends"]
                                 if d in by_id and by_id[d]["status"] not in SATISFIED)
                note = f"waits on {', '.join(waiting)}"
            elif bucket == "held":
                note = "explicitly blocked"
            elif bucket == "needs_routing":
                note = f"{t['routing']} - human gated"
            elif bucket == "in_progress":
                note = t["routing"]
            marker = paint(DOT, BUCKET_COLOUR[bucket], colour)
            phase = paint(f"p{t['phase']}", "dim", colour)
            tid = paint(f"{t['id']:<9}", "id", colour)
            title = clip(t["title"], 40)
            body = f"{marker} {phase} {tid} {title:<40}"
            lines.append(f"{body} {paint(clip(note, 20), 'dim', colour)}".rstrip())

    lines += ["", *render_doctor(findings, colour).splitlines()]

    panel = [paint(clip(action.reason, WIDTH - 4), "bold", colour)]
    if action.skill:
        panel.append(f"{ARROW} skill: {paint(action.skill, 'cyan', colour)}")
    if action.command:
        branch = next((a for a in action.command if a.startswith("Implement")), "")
        for line in branch.splitlines():
            if line.strip().startswith("- Work on branch"):
                branch_name = line.split('branch ')[1].split(';')[0]
                panel.append(f"{ARROW} {paint(branch_name, 'id', colour)}")
        panel.append(f"{ARROW} {paint('backlog.py --next', 'green', colour)}"
                     " prints the full invocation")
    lines += ["", *box(panel, "NEXT", action.kind, colour)]

    if lane_plan and len(lane_plan) > 1:
        rows = []
        for lane in lane_plan:
            rows.append(f"{paint(f'lane {lane.index}', 'id', colour)}  "
                        + clip(" ".join(lane.tasks), WIDTH - 14))
        for d in deferred:
            why = "collides with" if d.reason == "collision" else "queued behind"
            rows.append(paint(f"  {d.task} {why} lane {d.lane}", "dim", colour))
        lines += ["", *box(rows, "FLEET", f"{len(lane_plan)} lanes", colour)]
    return "\n".join(lines)


def render_next(action: NextAction, colour: bool = False) -> str:
    if action.command:
        return shlex.join(action.command)
    lines = [f"# no dispatchable task [{action.kind}]", f"# {action.reason}"]
    if action.skill:
        lines.append(f"# use the {action.skill} skill")
    return "\n".join(lines)


def render_fleet(lane_plan: list[Lane], deferred: list[Deferred],
                 action: NextAction, colour: bool = False) -> str:
    if not lane_plan:
        return render_next(action, colour)
    lines = []
    for lane in lane_plan:
        lines.append(paint(f"# lane {lane.index}: {', '.join(lane.tasks)}", "dim", colour))
        lines.append(shlex.join(lane.command))
        if len(lane.tasks) > 1:
            lines.append(paint("#   then, after it lands: "
                               + ", ".join(lane.tasks[1:]), "dim", colour))
        lines.append("")
    for d in deferred:
        why = ("would collide with" if d.reason == "collision"
               else "queues behind (lane cap)")
        lines.append(paint(f"# deferred: {d.task} {why} lane {d.lane}", "dim", colour))
    lines.append("claude agents --json          # monitor")
    lines.append("claude logs <session-id>      # drill in")
    return "\n".join(lines)


def render_agent(cfg: dict[str, Any], by_id: dict[str, Task],
                 buckets: dict[str, list[Task]], findings: list[Finding],
                 lane_plan: list[Lane], action: NextAction,
                 repo_git: dict[str, Any] | None) -> str:
    def task_view(t: Task) -> dict[str, Any]:
        return {"id": t["id"], "phase": t["phase"], "title": t["title"],
                "routing": t["routing"], "status": t["status"],
                "depends": list(t["depends"]), "touches": list(t["touches"])}

    payload = {
        "contract": {
            "schedulable": sorted(SCHEDULABLE), "satisfied": sorted(SATISFIED),
            "autonomous": sorted(AUTONOMOUS), "statuses": sorted(STATUSES),
            "routings": sorted(ROUTINGS), "version": CONTRACT_VERSION,
        },
        "ready": [task_view(t) for t in buckets["ready"]],
        "in_progress": [task_view(t) for t in buckets["in_progress"]],
        "needs_routing": [task_view(t) for t in buckets["needs_routing"]],
        "blocked": [task_view(t) for t in buckets["blocked"]],
        "held": [task_view(t) for t in buckets["held"]],
        "recommendation": recommend(action),
        "findings": [f._asdict() for f in findings],
        "lanes": [{"index": lane.index, "tasks": lane.tasks,
                   "touches": lane.touches, "command": lane.command}
                  for lane in lane_plan],
        "next_action": {"kind": action.kind, "reason": action.reason,
                        "command": action.command, "skill": action.skill},
        "meta": {"project": cfg["project"], "kind": cfg["kind"],
                 "git": repo_git is not None, "tasks": len(by_id)},
    }
    return json.dumps(payload, indent=2, sort_keys=False)


AGENT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "house backlog --agent payload",
    "type": "object",
    "required": ["contract", "ready", "in_progress", "needs_routing", "blocked",
                 "held", "recommendation", "findings", "lanes", "next_action", "meta"],
    "properties": {
        "contract": {"type": "object", "required": ["schedulable", "satisfied",
                     "autonomous", "statuses", "routings", "version"],
                     "properties": {"version": {"type": "integer"}}},
        "ready": {"type": "array", "items": {"$ref": "#/$defs/task"}},
        "in_progress": {"type": "array", "items": {"$ref": "#/$defs/task"}},
        "needs_routing": {"type": "array", "items": {"$ref": "#/$defs/task"}},
        "blocked": {"type": "array", "items": {"$ref": "#/$defs/task"}},
        "held": {"type": "array", "items": {"$ref": "#/$defs/task"}},
        "recommendation": {"type": "string"},
        "findings": {"type": "array", "items": {"type": "object",
                     "required": ["code", "level", "subject", "message", "hint"]}},
        "lanes": {"type": "array", "items": {"type": "object",
                  "required": ["index", "tasks", "touches", "command"]}},
        "next_action": {"type": "object",
                        "required": ["kind", "reason", "command", "skill"]},
        "meta": {"type": "object", "required": ["project", "kind", "git", "tasks"]},
    },
    "$defs": {"task": {"type": "object", "required": ["id", "phase", "title",
              "routing", "status", "depends", "touches"]}},
}


# =====================================================================
# L5  CLI
# =====================================================================

USAGE_EPILOG = """\
exit codes: 0 valid, 1 invalid manifest, 2 warn findings under --strict.
This tool never writes and never touches the network."""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="backlog.py", description="House backlog cockpit: validate, "
        "diagnose, dispatch.", epilog=USAGE_EPILOG)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--human", action="store_true", help="backlog report")
    mode.add_argument("--agent", action="store_true", help="JSON payload")
    mode.add_argument("--doctor", action="store_true", help="diagnostics only")
    mode.add_argument("--next", action="store_true", help="next dispatch command")
    mode.add_argument("--fleet", action="store_true", help="parallel lane plan")
    mode.add_argument("--json-schema", action="store_true", dest="json_schema",
                      help="print the --agent payload schema")
    p.add_argument("--lanes", type=int, default=None, metavar="N")
    p.add_argument("--budget", type=float, default=None, metavar="USD",
                   help="emit --max-budget-usd (API-key billing only)")
    p.add_argument("--repo", default=None, metavar="PATH")
    p.add_argument("--no-git", action="store_true", dest="no_git")
    p.add_argument("--subagents", choices=SUBAGENT_MODES, default="auto",
                   help="delegation guidance in emitted prompts (default auto)")
    p.add_argument("--model", choices=MODELS, default=None,
                   help="--model flag in emitted commands (default: the "
                   "task's own `model` field, else 'sonnet')")
    p.add_argument("--dangerous", action="store_true",
                   help="emit --dangerously-skip-permissions instead of "
                   "--permission-mode acceptEdits, in every mode. The "
                   "operator still has to run the printed command.")
    p.add_argument("--strict", action="store_true",
                   help="promote warn findings to exit 2")
    return p


def main(argv: list[str] | None = None, out: Any = None) -> int:
    args = build_parser().parse_args(argv)
    out = out or sys.stdout
    if args.json_schema:
        print(json.dumps(AGENT_SCHEMA, indent=2), file=out)
        return 0

    root = repo_root(args.repo)
    path = root / "tasks.toml"
    lay = layout(root)
    colour = use_colour(out)

    if not path.exists():
        cfg = meta({})
        action = next_action(False, {}, classify({}), cfg, lay, None,
                             args.budget, args.subagents, args.dangerous, args.model)
        if args.next:
            print(render_next(action, colour), file=out)
            return 0
        if args.human or args.doctor or args.fleet:
            print(f"no tasks.toml at {path}\n{action.reason}\n"
                  f"Skill: {action.skill}", file=out)
            return 0
        if args.agent:
            print(render_agent(cfg, {}, classify({}), [], [], action, None), file=out)
            return 0
        print("tasks.toml INVALID:", file=out)
        print(f"  - no tasks.toml at {path}", file=out)
        return 1

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        print("tasks.toml INVALID:", file=out)
        print(f"  - {exc}", file=out)
        return 1

    by_id, errors = validate(data.get("task", []))
    if errors:
        text, code = render_validation(by_id, errors)
        print(text, file=out)
        return code

    cfg = meta(data)
    buckets = classify(by_id)
    repo_git = git(root, enabled=not args.no_git)
    findings = doctor(root, by_id, buckets, lay, repo_git)
    action = next_action(True, by_id, buckets, cfg, lay, repo_git, args.budget,
                         args.subagents, args.dangerous, args.model)
    lane_plan, deferred = lanes(buckets["ready"], cfg, lay, args.lanes,
                                args.budget, args.subagents, args.dangerous,
                                args.model)

    if args.doctor:
        print(render_doctor(findings, colour), file=out)
    elif args.next:
        print(render_next(action, colour), file=out)
    elif args.fleet:
        print(render_fleet(lane_plan, deferred, action, colour), file=out)
    elif args.agent:
        print(render_agent(cfg, by_id, buckets, findings, lane_plan, action, repo_git),
              file=out)
    elif args.human:
        print(render_human(cfg, by_id, buckets, findings, lane_plan, deferred,
                           action, repo_git, colour), file=out)
    else:
        text, code = render_validation(by_id, errors)
        print(text, file=out)
        return code

    if args.strict and any(f.level == "warn" for f in findings):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())