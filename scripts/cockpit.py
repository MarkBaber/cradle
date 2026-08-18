#!/usr/bin/env python3
"""Interactive house backlog cockpit. Standalone: runs in any project directory.

The read-only half of this job already exists: scripts/backlog.py validates
the manifest, classifies the ready set, runs diagnostics and *computes*
dispatch argv -- but never writes, never runs `claude`, and never invokes
git in write mode. It is maintained directly in this repo (see its own
module docstring for what is actually a stability contract vs. open to
change). This file is the other half. It imports backlog.py as a library
(never forks it) and owns everything that acts: applying doctor fixes,
writing task status, launching agents, and watching the ones already
running.

    cockpit.py                     full-screen TUI in the repo containing cwd
    cockpit.py --repo PATH         explicit repo
    cockpit.py --no-tui            one-shot text summary (pipes, CI, cron)

Every write is confirmed: each action previews the exact argv or diff and waits
for a keypress. Nothing here runs unattended.

Layered L0..L5, banner-delimited, one-directional, mirroring backlog.py's own
convention: a function may reference only names at its own layer or above.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import time
import tomllib
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from importlib import util as importutil
from pathlib import Path
from types import ModuleType
from typing import Any, NamedTuple

# =====================================================================
# L0  BRIDGE  --  locate the repo, load backlog.py, build a snapshot.
#     backlog.py is the single source of truth for classification; this
#     file never re-implements a rule it already owns.
# =====================================================================

#: Minimum backlog.py CONTRACT_VERSION this cockpit understands. Bumped only
#: when a bucket or vocabulary change actually breaks an assumption here.
CONTRACT_MIN = 2

#: Marks the repo root. tasks.toml wins over .git: a worktree of a manifest
#: repo is still that project, and a repo with no manifest has nothing to show.
ROOT_MARKERS = ("tasks.toml", ".git")


class CockpitError(Exception):
    """Fatal, reportable, and never a traceback in the user's face."""


def find_repo(start: Path) -> Path | None:
    """Walk up from `start` for a project root. tasks.toml first, then .git."""
    chain = [start, *start.parents]
    for marker in ROOT_MARKERS:
        for directory in chain:
            if (directory / marker).exists():
                return directory
    return None


def load_backlog(repo: Path, override: str | None = None) -> ModuleType:
    """Import backlog.py as a module, preferring the copy the repo ships.

    Search order: --backlog, the repo's own scripts/backlog.py, this file's
    sibling, then $HOUSE_BACKLOG. The repo's copy wins so a project pinned to
    an older contract is read by the validator it was written against.
    """
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override).expanduser())
    candidates.append(repo / "scripts" / "backlog.py")
    candidates.append(Path(__file__).resolve().parent / "backlog.py")
    env = os.environ.get("HOUSE_BACKLOG")
    if env:
        candidates.append(Path(env).expanduser())

    for path in candidates:
        if not path.is_file():
            continue
        spec = importutil.spec_from_file_location("house_backlog", path)
        if spec is None or spec.loader is None:
            continue
        module = importutil.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:  # a broken copy is not a reason to crash
            raise CockpitError(f"{path} failed to import: {exc}") from exc
        version = getattr(module, "CONTRACT_VERSION", 0)
        if version < CONTRACT_MIN:
            raise CockpitError(
                f"{path} is contract version {version}; this cockpit needs "
                f"{CONTRACT_MIN} or newer.")
        return module

    tried = "\n  ".join(str(p) for p in candidates)
    raise CockpitError("cannot find backlog.py. Looked in:\n  " + tried
                       + "\nPass --backlog PATH or set HOUSE_BACKLOG.")


def load_usage(repo: Path, override: str | None = None) -> ModuleType | None:
    """Import usage.py, same by-path mechanism and search order as
    load_backlog (--usage-module override, the repo's own scripts/usage.py,
    this file's sibling, then $HOUSE_USAGE). Unlike backlog.py, usage.py is
    optional: not found or a broken import returns None instead of raising,
    so a project without one still runs the original four panes."""
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override).expanduser())
    candidates.append(repo / "scripts" / "usage.py")
    candidates.append(Path(__file__).resolve().parent / "usage.py")
    env = os.environ.get("HOUSE_USAGE")
    if env:
        candidates.append(Path(env).expanduser())

    for path in candidates:
        if not path.is_file():
            continue
        spec = importutil.spec_from_file_location("house_usage", path)
        if spec is None or spec.loader is None:
            continue
        module = importutil.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception:  # a broken copy degrades, it doesn't crash cockpit
            continue
        return module
    return None


class Snapshot(NamedTuple):
    """One consistent read of the repo. Rebuilt wholesale on refresh -- there
    is no incremental update path, because a stale half-view is worse than a
    slightly slower keypress."""
    repo: Path
    cfg: dict[str, Any]
    by_id: dict[str, Any]
    buckets: dict[str, list[Any]]
    findings: list[Any]
    lanes: list[Any]
    deferred: list[Any]
    action: Any
    layout: dict[str, Any]
    git: dict[str, Any] | None
    errors: list[str]
    taken_at: float


class TTLCache:
    """In-memory time-to-live (TTL) state cache for probe and subprocess results."""

    def __init__(self, default_ttl: float = 5.0) -> None:
        self.default_ttl = default_ttl
        self._store: dict[Any, tuple[float, Any]] = {}

    def get(self, key: Any, ttl: float | None = None) -> Any | None:
        eff_ttl = ttl if ttl is not None else self.default_ttl
        if key in self._store:
            ts, val = self._store[key]
            if time.time() - ts <= eff_ttl:
                return val
            del self._store[key]
        return None

    def set(self, key: Any, value: Any) -> None:
        self._store[key] = (time.time(), value)

    def clear(self) -> None:
        self._store.clear()


class AsyncSubprocessPool:
    """Background thread pool executor for offloading deterministic git and
    subprocess operations."""

    def __init__(self, max_workers: int = 4) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="cockpit_async_worker")

    def submit(self, fn: Any, *args: Any, **kwargs: Any) -> Future:
        return self._executor.submit(fn, *args, **kwargs)

    def shutdown(self, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait)


_SUBPROCESS_POOL = AsyncSubprocessPool()
_SNAPSHOT_CACHE: dict[tuple[str, int | None, bool], tuple[float, Snapshot]] = {}
_PR_CACHE = TTLCache(default_ttl=5.0)
_AGENTS_CACHE = TTLCache(default_ttl=2.0)
_LOG_TAIL_CACHE: dict[tuple[str, int, int], list[str]] = {}
#: The USAGE pane's source data is a full re-scan of every transcript under
#: CLAUDE_HOME (tens of megabytes, ~1s) -- far too slow to redo on every
#: redraw. 15s trades a little staleness for the pane no longer dominating
#: every poll tick.
_USAGE_CACHE = TTLCache(default_ttl=15.0)
#: The WORKFLOW pane's source data (.claude/settings.json's hooks,
#: .claude/agents/*.md frontmatter) is read fresh and re-parsed on every
#: call -- like _SNAPSHOT_CACHE, keyed by repo and invalidated by mtime
#: signature rather than a TTL, since this data changes only when a house
#: agent or hook is actually edited, not on some clock.
_WORKFLOW_CACHE: dict[str, tuple[tuple[float, tuple[tuple[str, float], ...]],
                                 list[Row]]] = {}


def get_subprocess_pool() -> AsyncSubprocessPool:
    """Return the global background worker thread pool."""
    return _SUBPROCESS_POOL


def offload_async(fn: Any, *args: Any, **kwargs: Any) -> Future:
    """Offload a deterministic function call or subprocess to non-blocking background workers."""
    return _SUBPROCESS_POOL.submit(fn, *args, **kwargs)


def clear_snapshot_cache() -> None:
    """Clear manifest snapshot cache."""
    _SNAPSHOT_CACHE.clear()


def clear_log_tail_cache() -> None:
    """Clear transcript log tail cache."""
    _LOG_TAIL_CACHE.clear()


def clear_probe_caches() -> None:
    """Clear PR and agent probe TTL caches."""
    _PR_CACHE.clear()
    _AGENTS_CACHE.clear()


def clear_usage_cache() -> None:
    """Clear the USAGE pane's transcript-scan TTL cache."""
    _USAGE_CACHE.clear()


def clear_workflow_cache() -> None:
    """Clear the WORKFLOW pane's hooks/agent-roster mtime cache."""
    _WORKFLOW_CACHE.clear()


def clear_all_caches() -> None:
    """Clear all in-memory caches across all tiers."""
    clear_snapshot_cache()
    clear_log_tail_cache()
    clear_usage_cache()
    clear_probe_caches()
    clear_workflow_cache()


def land_task_done_async(bl: ModuleType, repo: Path, task_id: str) -> Future[str]:
    """Offload land_task_done to a background worker."""
    return offload_async(land_task_done, bl, repo, task_id)


def land_tasks_done_async(bl: ModuleType, repo: Path, task_ids: list[str]) -> Future[str]:
    """Offload land_tasks_done to a background worker."""
    return offload_async(land_tasks_done, bl, repo, task_ids)


def probe_pr_async(repo: Path, branch: str) -> Future[dict[str, Any] | None]:
    """Offload probe_pr / gh pr view to a background worker."""
    return offload_async(probe_pr, repo, branch)


def probe_agents_async(repo: Path, binary: str) -> Future[list[Agent]]:
    """Offload probe_agents to a background worker."""
    return offload_async(probe_agents, repo, binary)


def ensure_landing_worktree_async(repo: Path) -> Future[Path]:
    """Offload git worktree / ensure_landing_worktree to a background worker."""
    return offload_async(ensure_landing_worktree, repo)


def gh_pr_view_async(repo: Path, branch: str) -> Future[dict[str, Any] | None]:
    """Offload gh pr view to a background worker."""
    return offload_async(probe_pr, repo, branch)


def _git_signature(repo: Path) -> float:
    """Cheap mtime-based signature for the git ref tree under `repo`.

    Captures branch creates, deletes, and HEAD moves without running a git
    subprocess. Returns 0.0 when the repo has no .git dir (bare, or not a
    git repo at all).
    """
    git_dir = repo / ".git"
    if not git_dir.is_dir():
        return 0.0
    sig = 0.0
    for name in ("HEAD", "index", "packed-refs", "COMMIT_EDITMSG"):
        p = git_dir / name
        with contextlib.suppress(OSError):
            sig = max(sig, p.stat().st_mtime)
    refs_heads = git_dir / "refs" / "heads"
    if refs_heads.is_dir():
        try:
            sig = max(sig, refs_heads.stat().st_mtime)
            for ref in refs_heads.iterdir():
                with contextlib.suppress(OSError):
                    sig = max(sig, ref.stat().st_mtime)
        except OSError:
            pass
    return sig


def snapshot(bl: ModuleType, repo: Path, lanes_cap: int | None = None,
             use_git: bool = True, bypass_cache: bool = False) -> Snapshot:
    """Run backlog.py's whole pipeline and capture the result."""
    path = repo / "tasks.toml"
    repo_key = (str(repo.resolve()), lanes_cap, use_git)
    mtime = 0.0
    if path.exists():
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0

    git_sig = _git_signature(repo) if use_git else 0.0
    if not bypass_cache and mtime > 0 and repo_key in _SNAPSHOT_CACHE:
        cached_mtime, cached_git_sig, cached_snap = _SNAPSHOT_CACHE[repo_key]
        if cached_mtime == mtime and cached_git_sig == git_sig:
            return cached_snap

    lay = bl.layout(repo)
    empty = bl.classify({})
    base = dict(repo=repo, layout=lay, by_id={}, buckets=empty, findings=[],
                lanes=[], deferred=[], git=None, taken_at=time.time())

    if not path.exists():
        cfg = bl.meta({})
        action = bl.next_action(False, {}, empty, cfg, lay, None)
        return Snapshot(cfg=cfg, action=action,
                        errors=[f"no tasks.toml at {path}"], **base)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        cfg = bl.meta({})
        action = bl.next_action(False, {}, empty, cfg, lay, None)
        return Snapshot(cfg=cfg, action=action, errors=[str(exc)], **base)

    by_id, errors = bl.validate(data.get("task", []))
    cfg = bl.meta(data)
    if errors:
        action = bl.next_action(False, {}, empty, cfg, lay, None)
        return Snapshot(cfg=cfg, action=action, errors=errors, **base)

    buckets = bl.classify(by_id)
    repo_git = bl.git(repo, enabled=use_git)
    findings = bl.doctor(repo, by_id, buckets, lay, repo_git)
    action = bl.next_action(True, by_id, buckets, cfg, lay, repo_git)
    lane_plan, deferred = bl.lanes(buckets["ready"], cfg, lay, lanes_cap)
    snap = Snapshot(repo=repo, cfg=cfg, by_id=by_id, buckets=buckets,
                    findings=findings, lanes=lane_plan, deferred=deferred,
                    action=action, layout=lay, git=repo_git, errors=[],
                    taken_at=time.time())
    if mtime > 0:
        _SNAPSHOT_CACHE[repo_key] = (mtime, git_sig, snap)
    return snap


# =====================================================================
# L1  PROBES  --  live agent and usage state. Every probe is failure
#     tolerant: an unavailable source yields an empty result, never a
#     traceback and never a warning about itself.
# =====================================================================

#: Where Claude Code keeps per-project session transcripts.
CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
#: Only the tail of a transcript is read: the newest usage record is the only
#: one that matters, and these files reach tens of megabytes.
TAIL_BYTES = 512 * 1024

#: The advisory price/context tables are owned by usage.py (DX-06: two price
#: tables drifting apart is exactly the failure it exists to clean up).
#: Loaded once at import time from cockpit's own sibling copy -- not
#: per-repo, since these are Anthropic's rates, not project-specific rules.
MODEL_PRICES: dict[str, tuple[float, float]]
CACHE_READ_RATE: float
CACHE_WRITE_RATE: float
CONTEXT_WINDOWS: dict[str, int]
DEFAULT_WINDOW: int

_own_usage = load_usage(Path(__file__).resolve().parent.parent)
if _own_usage is not None:
    MODEL_PRICES = _own_usage.MODEL_PRICES
    CACHE_READ_RATE = _own_usage.CACHE_READ_RATE
    CACHE_WRITE_RATE = _own_usage.CACHE_WRITE_RATE
    CONTEXT_WINDOWS = _own_usage.CONTEXT_WINDOWS
    DEFAULT_WINDOW = _own_usage.DEFAULT_WINDOW
else:
    MODEL_PRICES, CONTEXT_WINDOWS = {}, {}
    CACHE_READ_RATE, CACHE_WRITE_RATE, DEFAULT_WINDOW = 0.1, 1.25, 1_000_000


class Usage(NamedTuple):
    model: str
    input: int
    output: int
    cache_read: int
    cache_write: int

    @property
    def context(self) -> int:
        """Tokens occupying the window on the last turn."""
        return self.input + self.cache_read + self.cache_write

    @property
    def window(self) -> int:
        return CONTEXT_WINDOWS.get(self.model, DEFAULT_WINDOW)

    @property
    def cost(self) -> float:
        rate = MODEL_PRICES.get(self.model)
        if rate is None:
            return 0.0
        inp, out = rate
        return (self.input * inp
                + self.output * out
                + self.cache_read * inp * CACHE_READ_RATE
                + self.cache_write * inp * CACHE_WRITE_RATE) / 1_000_000


class Agent(NamedTuple):
    session_id: str
    name: str
    kind: str          # 'background' | 'interactive'
    status: str        # 'busy' | 'idle' | ...
    state: str         # secondary state, e.g. 'blocked'; '' when absent
    cwd: str
    pid: int
    started_at: float  # epoch seconds
    usage: Usage | None

    @property
    def age(self) -> float:
        return max(0.0, time.time() - self.started_at)


def claude_bin(override: str | None = None) -> str:
    """Resolve the claude executable. Overridable so tests never touch the
    real CLI (CLAUDE.md forbids invoking it while building NightShift)."""
    return (override or os.environ.get("COCKPIT_CLAUDE_BIN")
            or shutil.which("claude") or "claude")


def agy_bin(override: str | None = None) -> str:
    """Resolve the agy (Antigravity) executable. Same override chain as
    claude_bin, same reason: ADR 0017 extends CLAUDE.md's real-CLI ban to
    agy, so tests must be able to stand in a fake here too."""
    return (override or os.environ.get("COCKPIT_AGY_BIN")
            or shutil.which("agy") or "agy")


def read_agy_families(repo: Path) -> dict[str, str]:
    """AG-06's `defaults.antigravity_model_families` from nightshift.toml,
    read directly rather than through the nightshift package -- cockpit.py
    is standalone (see module docstring) and must not require AG-06 to have
    landed. Missing file, missing table, or a malformed value all fall back
    to {}, the same single-slug-fallback behaviour AG-06 itself defines,
    never a traceback here."""
    try:
        data = tomllib.loads((repo / "nightshift.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    families = data.get("defaults", {}).get("antigravity_model_families", {})
    return families if isinstance(families, dict) else {}


def _agy_family(slug: str) -> str:
    """The family of an agy model slug -- 'gemini-*' belongs to 'gemini',
    'claude-*' and 'gpt-*' belong to 'other' (Antigravity's bundled
    allotment, ADR 0018)."""
    if slug.startswith("gemini-"):
        return "gemini"
    if slug.startswith(("claude-", "gpt-")):
        return "other"
    return slug.split("-", 1)[0]


def agy_family_groups(models: tuple[str, ...],
                      families: dict[str, str]) -> dict[str, tuple[str, ...]]:
    """Group agy's model slugs by family. When `families` is provided by
    config, restrict grouping to those families. When `families` is empty,
    fall back to the default two-family split ('gemini' and 'other', ADR 0018).
    Empty unless at least two families have at least one matching slug --
    ADR 0018 wants the picker's family row skipped, not shown with a single
    option nobody needs to confirm."""
    if not families:
        grouped_dict: dict[str, list[str]] = {}
        for m in models:
            fam = _agy_family(m)
            grouped_dict.setdefault(fam, []).append(m)
        grouped = {name: tuple(opts) for name, opts in grouped_dict.items() if opts}
    else:
        grouped = {}
        for name in families:
            opts = tuple(m for m in models if _agy_family(m) == name or m.startswith(f"{name}-"))
            if opts:
                grouped[name] = opts
    return grouped if len(grouped) > 1 else {}


def project_slug(cwd: str) -> str:
    """Claude Code's transcript directory name for a working directory: every
    non-alphanumeric byte becomes a hyphen."""
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def transcript(cwd: str, session_id: str) -> Path | None:
    path = CLAUDE_HOME / "projects" / project_slug(cwd) / f"{session_id}.jsonl"
    return path if path.is_file() else None


def read_usage(path: Path) -> Usage | None:
    """Newest assistant usage record in a transcript, or None."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > TAIL_BYTES:
                handle.seek(size - TAIL_BYTES)
            blob = handle.read()
    except OSError:
        return None
    lines = blob.split(b"\n")
    if size > TAIL_BYTES and lines:
        lines = lines[1:]        # first line is a fragment after the seek
    for raw in reversed(lines):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        used = message.get("usage")
        if not isinstance(used, dict):
            continue
        return Usage(
            model=str(message.get("model") or "unknown"),
            input=int(used.get("input_tokens") or 0),
            output=int(used.get("output_tokens") or 0),
            cache_read=int(used.get("cache_read_input_tokens") or 0),
            cache_write=int(used.get("cache_creation_input_tokens") or 0))
    return None


_TRACKED_ANTIGRAVITY_DISPATCHES: list[dict[str, Any]] = []


def clear_tracked_dispatches() -> None:
    _TRACKED_ANTIGRAVITY_DISPATCHES.clear()


def _extract_task_id(argv: list[str]) -> str:
    prompt = ""
    if "-p" in argv:
        idx = argv.index("-p")
        if idx + 1 < len(argv):
            prompt = argv[idx + 1]
    elif argv and "\n" in argv[-1]:
        prompt = argv[-1]

    if prompt:
        m = re.search(r"Implement task ([A-Za-z0-9_-]+) from tasks\.toml", prompt)
        if m:
            return m.group(1)
        m = re.search(r"task/([A-Za-z0-9_-]+)", prompt)
        if m:
            return m.group(1)
        m = re.search(r"\btask ([A-Za-z0-9_-]+)\b", prompt)
        if m:
            return m.group(1)

    # argv[0] is the executable path — never a legitimate source of a task id
    # (worktrees are named `<TASK-ID>-<slug>`, so the interpreter path inside
    # one would otherwise match and misattribute the dispatch).
    for arg in argv[1:]:
        m = re.search(r"\b([A-Z0-9]{2,}-\d+)\b", arg)
        if m:
            return m.group(1)
    return "antigravity"


def recover_persistent_antigravity_dispatches(repo: Path) -> None:
    """Recover persistent antigravity dispatch metadata from .cockpit/logs/*.json."""
    logs_dir = repo / ".cockpit" / "logs"
    if not logs_dir.is_dir():
        return
    repo_resolved = repo.resolve()
    for meta_path in sorted(logs_dir.glob("*.json")):
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        item_repo_str = data.get("repo")
        if item_repo_str and Path(item_repo_str).resolve() != repo_resolved:
            continue
        pid = int(data.get("pid") or 0)
        if pid <= 0:
            continue

        log_path = meta_path.with_suffix(".log")

        already_tracked = False
        for item in _TRACKED_ANTIGRAVITY_DISPATCHES:
            item_pid = item.get("pid")
            item_log = item.get("log_path")
            item_meta = item.get("meta_path")
            if (
                (item_meta and item_meta == meta_path)
                or (item_pid and item_pid == pid)
                or (item_log and item_log == log_path)
            ):
                already_tracked = True
                break
        if already_tracked:
            continue

        command = data.get("command") or []
        task_id = str(data.get("task_id") or _extract_task_id(command))
        started_at = float(data.get("started_at") or 0.0)

        _TRACKED_ANTIGRAVITY_DISPATCHES.append({
            "task_id": task_id,
            "pid": pid,
            "proc": None,
            "log_path": log_path if log_path.is_file() else None,
            "started_at": started_at,
            "command": command,
            "binary": command[0] if command else agy_bin(),
            "repo": repo,
            "meta_path": meta_path,
        })


def _get_ancestor_pids() -> set[int]:
    """Return the set of PIDs in the process hierarchy of the current process up to root."""
    ancestors: set[int] = set()
    curr = os.getpid()
    while curr > 1:
        ancestors.add(curr)
        try:
            raw = Path(f"/proc/{curr}/stat").read_text(encoding="utf-8")
            parts = raw.split(")")
            if len(parts) >= 2:
                fields = parts[-1].split()
                ppid = int(fields[1])
                if ppid in ancestors or ppid <= 0:
                    break
                curr = ppid
            else:
                break
        except Exception:
            break
    return ancestors


def probe_os_process_table(repo: Path) -> None:
    """Probe running OS process table for active agy commands belonging to repo."""
    repo_resolved = repo.resolve()
    repo_str = str(repo_resolved)
    found_procs: list[tuple[int, list[str], str]] = []

    # 1. /proc directory inspection — preferred: cmdline is NUL-separated, so
    #    argument boundaries survive and a multi-word `-p <prompt>` stays intact.
    proc_dir_path = Path("/proc")
    if proc_dir_path.is_dir():
        for pdir in proc_dir_path.iterdir():
            if not pdir.name.isdigit():
                continue
            pid = int(pdir.name)
            cmdline_file = pdir / "cmdline"
            if not cmdline_file.is_file():
                continue
            try:
                raw = cmdline_file.read_bytes()
                cmd_args = [p.decode("utf-8", "replace") for p in raw.split(b"\x00") if p]
                if cmd_args:
                    cmd_str = " ".join(cmd_args)
                    found_procs.append((pid, cmd_args, cmd_str))
            except OSError:
                pass

    # 2. ps aux — fallback for PIDs /proc could not supply (and the only source
    #    at all on platforms without /proc). Lossy: ps renders the command
    #    unquoted, so shlex.split cannot recover the original argument split.
    try:
        proc = subprocess.run(["ps", "aux"], capture_output=True, text=True, check=False, timeout=5)
        if proc.returncode == 0:
            for line in proc.stdout.splitlines()[1:]:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(None, 10)
                if len(parts) < 11:
                    continue
                try:
                    pid = int(parts[1])
                except ValueError:
                    continue
                if any(p == pid for p, _, _ in found_procs):
                    continue
                cmd_str = parts[10]
                try:
                    cmd_args = shlex.split(cmd_str)
                except ValueError:
                    cmd_args = cmd_str.split()
                found_procs.append((pid, cmd_args, cmd_str))
    except (OSError, subprocess.SubprocessError):
        pass

    ancestors = _get_ancestor_pids()
    for pid, cmd_args, cmd_str in found_procs:
        if pid in ancestors:
            continue
        try:
            os.kill(pid, 0)
        except OSError:
            continue

        if "pytest" in cmd_str.lower():
            continue

        first_arg = cmd_args[0] if cmd_args else ""
        first_arg_name = Path(first_arg).name.lower()
        is_agy_cmd = (
            first_arg_name in ("agy", "fake_agy") or
            first_arg_name.startswith("fake_agy") or
            first_arg == agy_bin() or
            any(
                "fake_agy" in arg or Path(arg).name.lower().startswith("fake_agy")
                for arg in cmd_args
            )
            or
            ("agy" in cmd_args)
        )
        if not is_agy_cmd:
            continue

        belongs = False
        try:
            proc_cwd = os.readlink(f"/proc/{pid}/cwd")
            resolved_proc_cwd = Path(proc_cwd).resolve()
            if resolved_proc_cwd == repo_resolved or repo_str in str(resolved_proc_cwd):
                belongs = True
        except OSError:
            pass

        if not belongs and (repo_str in cmd_str or str(repo) in cmd_str):
            belongs = True

        if not belongs:
            continue

        task_id = _extract_task_id(cmd_args)

        already_tracked = False
        for item in _TRACKED_ANTIGRAVITY_DISPATCHES:
            if item.get("pid") == pid:
                already_tracked = True
                break
        if already_tracked:
            continue

        log_path = None
        logs_dir = repo / ".cockpit" / "logs"
        if logs_dir.is_dir():
            for lfile in logs_dir.glob("*.log"):
                if task_id != "antigravity" and task_id in lfile.name:
                    log_path = lfile
                    break

        _TRACKED_ANTIGRAVITY_DISPATCHES.append({
            "task_id": task_id,
            "pid": pid,
            "proc": None,
            "log_path": log_path,
            "started_at": time.time(),
            "command": cmd_args,
            "binary": first_arg or agy_bin(),
            "repo": repo,
        })


def probe_antigravity_dispatches(repo: Path) -> list[Agent]:
    """Probe tracked antigravity dispatches for `repo`, returning Agent representations.

    Tracks PID liveness (via os.kill(pid, 0) / proc.poll()), reads log tail
    heuristics (last non-empty line, FAIL if non-zero exit code), and falls back
    to branch/PR probing if process tracking is inconclusive.
    """
    recover_persistent_antigravity_dispatches(repo)
    probe_os_process_table(repo)

    repo_resolved = repo.resolve()
    agents: list[Agent] = []

    for item in list(_TRACKED_ANTIGRAVITY_DISPATCHES):
        item_repo = item.get("repo")
        if item_repo and item_repo.resolve() != repo_resolved:
            continue

        pid = int(item.get("pid") or 0)
        proc: subprocess.Popen[bytes] | None = item.get("proc")
        log_path = Path(item["log_path"]) if item.get("log_path") else None
        task_id = str(item.get("task_id") or "antigravity")
        started_at = float(item.get("started_at") or 0.0)

        is_alive = False
        returncode = None

        if proc is not None:
            returncode = proc.poll()
            if returncode is None:
                is_alive = True
        elif pid > 0:
            try:
                os.kill(pid, 0)
                is_alive = True
            except OSError as err:
                is_alive = (err.errno == errno.EPERM)

        log_tail = ""
        has_fail = False
        if log_path and log_path.is_file():
            try:
                text = log_path.read_text(encoding="utf-8", errors="replace")
                non_empty = [line.strip() for line in text.splitlines() if line.strip()]
                if non_empty:
                    log_tail = non_empty[-1]
                if "FAIL" in text or "error" in text.lower():
                    has_fail = True
            except OSError:
                pass

        if is_alive:
            status = "busy"
            state = log_tail or "running"
        else:
            if returncode is not None and returncode != 0:
                status = f"FAIL({returncode})"
                state = log_tail or f"exit code {returncode}"
            elif returncode == 0:
                status = "ok"
                state = log_tail or "finished"
            elif has_fail or (log_tail and "FAIL" in log_tail):
                status = "FAIL"
                state = log_tail
            elif log_tail:
                status = "ok"
                state = log_tail
            else:
                status = "unknown"
                state = ""

        # Requirement 3 fallback if status is unknown and process not alive
        if not is_alive and status == "unknown":
            try:
                git_proc = subprocess.run(["git", "branch", "--list"], cwd=repo,
                                          capture_output=True, text=True, timeout=5, check=False)
                branch_dict = {
                    line.strip().lstrip("* "): 1
                    for line in git_proc.stdout.splitlines()
                    if line.strip()
                }
            except (OSError, subprocess.SubprocessError):
                branch_dict = {}
            branch_name = branch_for_task(task_id, branch_dict)
            if branch_name:
                pr = probe_pr(repo, branch_name)
                if pr and pr.get("state") == "OPEN":
                    status = "in_pr"
                    state = f"PR #{pr.get('number')}"
                else:
                    status = "branch_created"
                    state = branch_name
            else:
                status = "FAIL"
                state = "no branch/PR created"

        worktree_path = str(repo / ".claude" / "worktrees" / f"{task_id}-dispatch")

        agents.append(Agent(
            session_id=log_path.name if log_path else f"agy-{pid}",
            name=f"agy:{task_id}",
            kind="agy",
            status=status,
            state=state,
            cwd=worktree_path,
            pid=pid,
            started_at=started_at,
            usage=None,
        ))

    return agents


def probe_agents(repo: Path, binary: str, ttl: float = 2.0,
                 bypass_cache: bool = False) -> list[Agent]:
    """Live sessions under `repo`, newest first. `claude agents --json` works
    headless and covers worktrees, which live under the repo root.
    Antigravity dispatches (which have no `claude agents --json` equivalent) are
    tracked locally by PID and log path and included alongside."""
    key = (str(repo.resolve()), binary)
    if not bypass_cache and ttl > 0:
        cached: list[Agent] | None = _AGENTS_CACHE.get(key, ttl=ttl)
        if cached is not None:
            return cached

    rows: list[Any] = []
    is_agy_bin = ("agy" in Path(binary).name.lower()) or (binary == agy_bin())
    if not is_agy_bin:
        try:
            proc = subprocess.run([binary, "agents", "--json", "--cwd", str(repo)],
                                  capture_output=True, text=True, timeout=10,
                                  check=False)
            if proc.returncode == 0:
                parsed = json.loads(proc.stdout or "[]")
                if isinstance(parsed, list):
                    rows = parsed
        except (OSError, subprocess.SubprocessError, ValueError):
            rows = []

    agents: list[Agent] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        session = str(row.get("sessionId") or "")
        cwd = str(row.get("cwd") or "")
        path = transcript(cwd, session) if session and cwd else None
        agents.append(Agent(
            session_id=session,
            name=str(row.get("name") or session[:8] or "?"),
            kind=str(row.get("kind") or "?"),
            status=str(row.get("status") or "?"),
            state=str(row.get("state") or ""),
            cwd=cwd,
            pid=int(row.get("pid") or 0),
            started_at=float(row.get("startedAt") or 0) / 1000.0,
            usage=read_usage(path) if path else None))

    agy_agents = probe_antigravity_dispatches(repo)
    agents.extend(agy_agents)

    agents.sort(key=lambda a: a.started_at, reverse=True)
    if ttl > 0:
        _AGENTS_CACHE.set(key, agents)
    return agents


def daily_usage() -> tuple[int, float] | None:
    """(tokens, USD) for today from Claude Code's own stats cache, or None.

    This is the honest substitute for the subscription usage bar: the 5-hour
    and weekly limits shown by /usage have no on-disk source, so the cockpit
    reports what it can actually see rather than inventing a gauge.
    """
    path = CLAUDE_HOME / "stats-cache.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    today = time.strftime("%Y-%m-%d")
    tokens = 0
    for entry in data.get("dailyModelTokens") or []:
        if isinstance(entry, dict) and entry.get("date") == today:
            for count in (entry.get("tokensByModel") or {}).values():
                tokens += int(count or 0)
    cost = 0.0
    for stats in (data.get("modelUsage") or {}).values():
        if isinstance(stats, dict):
            cost += float(stats.get("costUSD") or 0.0)
    return tokens, cost


def branch_for_task(task_id: str, branches: dict[str, int]) -> str | None:
    """Best-effort match of a task id to one of its local branches.

    Mirrors backlog.py's own `_branch_for` (duplicated on purpose: this is a
    display lookup for the Review pane, not a classification rule the two
    files must share)."""
    for name in sorted(branches):
        head = name.split("/", 1)[-1]
        if head == task_id or head.startswith(task_id + "-"):
            return name
    return None


def agent_for_task(task_id: str, agents: Sequence[Agent]) -> Agent | None:
    """Best-effort match of a task id to a live fleet agent.

    build_command's 'fleet' mode passes `-w <task-id>-<slug>` (the -w flag),
    which names the *worktree*, not the session -- Claude Code labels the
    session itself with its own auto-generated summary, unrelated to the
    task id. The task id only survives in `agent.cwd`'s worktree directory
    name, so that's what this matches on: same shape as a branch head, same
    match rule as branch_for_task, on purpose."""
    for agent in agents:
        head = Path(agent.cwd).name if agent.cwd else ""
        if head == task_id or head.startswith(task_id + "-"):
            return agent
        if agent.kind == "agy" and (
            agent.name == f"agy:{task_id}" or agent.name.endswith(f":{task_id}")
        ):
            return agent
    return None


#: gh fields needed for the Review pane's detail view: identity, size, and
#: the file list a "tree of changed files" is built from.
_PR_FIELDS = ("number,title,url,state,body,additions,deletions,"
             "changedFiles,files,baseRefName")


def probe_pr(repo: Path, branch: str, ttl: float = 5.0,
             bypass_cache: bool = False) -> dict[str, Any] | None:
    """The open-or-closed PR for `branch`'s head, via `gh`. None if `gh` is
    missing, unauthenticated, or no PR exists -- never raises. Advisory
    only, same contract as every other L1 probe."""
    key = (str(repo.resolve()), branch)
    if not bypass_cache and ttl > 0:
        cached = _PR_CACHE.get(key, ttl=ttl)
        if cached is not None:
            return cached

    try:
        proc = subprocess.run(["gh", "pr", "view", branch, "--json", _PR_FIELDS],
                              capture_output=True, text=True, timeout=10,
                              check=False, cwd=repo)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        return None
    res = data if isinstance(data, dict) else None
    if res is not None and ttl > 0:
        _PR_CACHE.set(key, res)
    return res


#: `<type>/<ID>-<slug>` -- the branch-name convention CLAUDE.md itself
#: prescribes for every task/fix/chore branch, so a landed commit's task ID
#: reliably shows up in its subject (a squash-merge title carries the PR
#: title, which for this repo's own workflow is the branch's own name or a
#: title referencing it) or, for a true merge commit, `Merge pull request
#: #N from <user>/<type>/<ID>-<slug>`.
_TASK_ID_RE = re.compile(r"\b([A-Z]{2,6}-\d{1,4})\b")
_COMMIT_LOG_RE = re.compile(r"^([0-9a-f]+)\|(\d{4}-\d{2}-\d{2})\|(.*)$")
_LANDED_CACHE = TTLCache(default_ttl=60.0)


class LandedCommit(NamedTuple):
    """One first-parent commit on `main`, with the task ID parsed from its
    subject (None when nothing matches) and its net diff against its own
    first parent -- the same stat for a plain commit or a merge commit
    either way, since `sha^1` is well-defined for both."""
    sha: str
    date: str
    subject: str
    task_id: str | None
    insertions: int
    deletions: int


def _diff_numstat_total(repo: Path, sha: str) -> tuple[int, int]:
    """Net (insertions, deletions) for `sha` against its first parent.
    (0, 0) on any git failure, including a root commit with no parent --
    same failure-tolerant contract as every other L1 probe."""
    try:
        proc = subprocess.run(
            ["git", "diff", "--numstat", f"{sha}^1", sha],
            capture_output=True, text=True, timeout=15, check=False, cwd=repo)
    except (OSError, subprocess.SubprocessError):
        return (0, 0)
    if proc.returncode != 0:
        return (0, 0)
    insertions = deletions = 0
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        added, removed = parts[0], parts[1]
        if added.isdigit():
            insertions += int(added)
        if removed.isdigit():
            deletions += int(removed)
    return (insertions, deletions)


def probe_landed_commits(repo: Path, days: int, ttl: float = 60.0,
                          bypass_cache: bool = False) -> list[LandedCommit]:
    """First-parent commits on `main` over the trailing `days` days, oldest
    first, each with its task ID (if its subject carries one) and net diff
    stat. This is the ANALYTICS pane's only data source for task-completion
    and code-churn series: cockpit.py never merges a task's PR itself (see
    land_task_done's docstring), so there is no live event to capture stats
    at -- both series are reconstructed from `main`'s own history instead.
    [] on any git failure, never raises."""
    key = (str(repo.resolve()), days)
    if not bypass_cache and ttl > 0:
        cached = _LANDED_CACHE.get(key, ttl=ttl)
        if cached is not None:
            return cached

    # Single invocation: --numstat emits insertions/deletions after each
    # commit header.  "COMMIT " prefix + %P (parent hashes) distinguishes
    # header lines from numstat lines; empty %P identifies root commits,
    # which have no parent to diff against -- (0, 0) matches the contract
    # that _diff_numstat_total honours for the same root-commit case.
    _HEADER_RE = re.compile(
        r"^([0-9a-f]+)\|(\d{4}-\d{2}-\d{2})\|(.*)\|(.*)$")
    try:
        proc = subprocess.run(
            ["git", "log", "main", "--first-parent", f"--since={days}.days.ago",
             "--date=short", "--pretty=format:COMMIT %H|%ad|%s|%P", "--numstat",
             "--reverse"],
            capture_output=True, text=True, timeout=30, check=False, cwd=repo)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []

    commits: list[LandedCommit] = []
    current_sha: str | None = None
    current_date: str = ""
    current_subject: str = ""
    has_parent: bool = True
    insertions = deletions = 0
    for line in proc.stdout.splitlines():
        if line.startswith("COMMIT "):
            # Flush previous commit if any
            if current_sha is not None:
                task_match = _TASK_ID_RE.search(current_subject)
                commits.append(LandedCommit(
                    sha=current_sha, date=current_date,
                    subject=current_subject,
                    task_id=task_match.group(1) if task_match else None,
                    insertions=insertions if has_parent else 0,
                    deletions=deletions if has_parent else 0))
            m = _HEADER_RE.match(line[len("COMMIT "):])
            if m is None:
                current_sha = None
            else:
                current_sha, current_date, current_subject, parents = m.groups()
                has_parent = bool(parents.strip())
            insertions = deletions = 0
        elif current_sha is not None and line:
            # numstat line: "<ins>\t<del>\t<path>"
            parts = line.split("\t")
            if len(parts) >= 2:
                if parts[0].isdigit():
                    insertions += int(parts[0])
                if parts[1].isdigit():
                    deletions += int(parts[1])
    # Flush last commit
    if current_sha is not None:
        task_match = _TASK_ID_RE.search(current_subject)
        commits.append(LandedCommit(
            sha=current_sha, date=current_date, subject=current_subject,
            task_id=task_match.group(1) if task_match else None,
            insertions=insertions if has_parent else 0,
            deletions=deletions if has_parent else 0))
    if ttl > 0:
        _LANDED_CACHE.set(key, commits)
    return commits


def landed_by_day(commits: Sequence[LandedCommit]) -> dict[str, int]:
    """Count of distinct task IDs landed per calendar day. A commit whose
    subject carries no task ID (a non-task merge, a direct chore commit)
    doesn't count -- there's nothing to attribute it to."""
    by_day: dict[str, set[str]] = {}
    for c in commits:
        if c.task_id is None:
            continue
        by_day.setdefault(c.date, set()).add(c.task_id)
    return {date: len(ids) for date, ids in by_day.items()}


def churn_by_day(commits: Sequence[LandedCommit]) -> dict[str, tuple[int, int]]:
    """(insertions, deletions) summed per calendar day across every commit
    that day, task-attributed or not -- churn counts the code, not just the
    tasks it belongs to."""
    by_day: dict[str, tuple[int, int]] = {}
    for c in commits:
        ins, dele = by_day.get(c.date, (0, 0))
        by_day[c.date] = (ins + c.insertions, dele + c.deletions)
    return by_day


# =====================================================================
# L2  ACTIONS  --  everything that writes. Each action is planned first
#     and applied second, so the confirm screen shows exactly what will
#     run. Nothing below is invoked without a keypress.
# =====================================================================

AUTO, GUIDED, EXPLAIN = "auto", "guided", "explain"


class EditError(Exception):
    """A manifest edit could not be made safely. Never partially applied."""


class Step(NamedTuple):
    """One unit of work. Exactly one of argv/edit/routing_edit is set; edit
    is (task_id, new_status) applied to tasks.toml. `notes`, when set
    alongside edit, rewrites the task's `notes` field in the same pass --
    used to send review feedback back with the status change. routing_edit
    is (task_id, new_routing) -- the Approve action's rewrite, the one place
    this file edits `routing` rather than `status`."""
    describe: str
    argv: list[str] | None = None
    edit: tuple[str, str] | None = None
    notes: str | None = None
    routing_edit: tuple[str, str] | None = None


class Plan(NamedTuple):
    tier: str            # AUTO | GUIDED | EXPLAIN
    title: str
    steps: list[Step]
    caution: str = ""    # extra warning shown on the confirm screen


def set_status(text: str, task_id: str, status: str) -> str:
    """Rewrite one task's status in tasks.toml source text.

    A surgical line edit, not a re-serialisation: tomllib is read-only, and
    round-tripping through a writer would reflow the file and drop every
    comment. Raises EditError rather than guessing.
    """
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.strip() == "[[task]]"]
    if not starts:
        raise EditError("no [[task]] tables in tasks.toml")

    id_re = re.compile(r"""^\s*id\s*=\s*(["'])(.+?)\1""")
    status_re = re.compile(r"""^(\s*status\s*=\s*)(["'])(.*?)\2(.*)$""")

    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(lines)
        block = lines[start:end]
        if not any((m := id_re.match(ln)) and m.group(2) == task_id
                   for ln in block):
            continue
        hits = [i for i, ln in enumerate(block) if status_re.match(ln)]
        if len(hits) != 1:
            raise EditError(
                f"{task_id}: expected exactly one status line, found {len(hits)}")
        offset = hits[0]
        match = status_re.match(block[offset])
        assert match is not None
        quote = match.group(2)
        lines[start + offset] = (f"{match.group(1)}{quote}{status}{quote}"
                                 f"{match.group(4)}\n")
        return "".join(lines)
    raise EditError(f"{task_id}: no [[task]] table with that id")


def set_routing(text: str, task_id: str, routing: str) -> str:
    """Rewrite one task's routing in tasks.toml source text.

    Same surgical-line-edit contract as set_status: tomllib is read-only,
    and round-tripping through a writer would reflow the file and drop
    every comment.
    """
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.strip() == "[[task]]"]
    if not starts:
        raise EditError("no [[task]] tables in tasks.toml")

    id_re = re.compile(r"""^\s*id\s*=\s*(["'])(.+?)\1""")
    routing_re = re.compile(r"""^(\s*routing\s*=\s*)(["'])(.*?)\2(.*)$""")

    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(lines)
        block = lines[start:end]
        if not any((m := id_re.match(ln)) and m.group(2) == task_id
                   for ln in block):
            continue
        hits = [i for i, ln in enumerate(block) if routing_re.match(ln)]
        if len(hits) != 1:
            raise EditError(
                f"{task_id}: expected exactly one routing line, found {len(hits)}")
        offset = hits[0]
        match = routing_re.match(block[offset])
        assert match is not None
        quote = match.group(2)
        lines[start + offset] = (f"{match.group(1)}{quote}{routing}{quote}"
                                 f"{match.group(4)}\n")
        return "".join(lines)
    raise EditError(f"{task_id}: no [[task]] table with that id")


def _string_span(block: list[str], key: str) -> tuple[int, int] | None:
    """Line range [lo, hi] (inclusive, indices into `block`) of `key`'s
    assignment, or None if the key is absent. Handles both a single-line
    value and a `\"\"\"`-delimited multi-line one -- the only two shapes
    tasks.toml ever uses for a string field."""
    pattern = re.compile(rf"""^\s*{key}\s*=\s*(.*)$""")
    for i, ln in enumerate(block):
        m = pattern.match(ln)
        if not m:
            continue
        stripped = m.group(1).rstrip("\n").lstrip()
        if not (stripped.startswith('"""') or stripped.startswith("'''")):
            return i, i
        quote, remainder = stripped[:3], stripped[3:]
        if quote in remainder:
            return i, i
        for j in range(i + 1, len(block)):
            if quote in block[j]:
                return i, j
        return i, len(block) - 1
    return None


def _wrap_toml_string(text: str, width: int = 88) -> str:
    """Word-wrap `text` into a TOML multi-line-string body, escaped and
    joined with backslash line-continuations so it reads like the rest of
    the file (TOML drops a continued newline and the following whitespace --
    the parsed value has no literal line breaks). Escaping happens before
    wrapping, so an escaped sequence is never split across a wrap boundary.
    """
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    words = escaped.split()
    lines: list[str] = []
    line = ""
    for word in words:
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        lines.append(line)
    return " \\\n".join(lines)


def set_notes(text: str, task_id: str, notes: str) -> str:
    """Rewrite (or add) one task's `notes` field in tasks.toml source text.

    Same surgical-edit contract as set_status: never a re-serialisation.
    `notes` already carries a task's current reason for its status (ADR
    0009's blocked/notes convention), so this replaces the whole prior
    assignment rather than appending to it.
    """
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.strip() == "[[task]]"]
    if not starts:
        raise EditError("no [[task]] tables in tasks.toml")

    id_re = re.compile(r"""^\s*id\s*=\s*(["'])(.+?)\1""")
    field = 'notes = """' + _wrap_toml_string(notes) + '"""\n'

    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(lines)
        block = lines[start:end]
        id_index = next((i for i, ln in enumerate(block)
                         if (m := id_re.match(ln)) and m.group(2) == task_id), None)
        if id_index is None:
            continue
        span = _string_span(block, "notes")
        if span is None:
            new_block = block[:id_index + 1] + [field] + block[id_index + 1:]
        else:
            lo, hi = span
            new_block = block[:lo] + [field] + block[hi + 1:]
        return "".join(lines[:start]) + "".join(new_block) + "".join(lines[end:])
    raise EditError(f"{task_id}: no [[task]] table with that id")


def _persist(bl: ModuleType, repo: Path, transform: Any) -> None:
    """Apply `transform` to tasks.toml's source text and write it back,
    refusing to leave the manifest invalid. Shared by every writer below."""
    path = repo / "tasks.toml"
    try:
        original = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EditError(str(exc)) from exc
    updated = transform(original)
    try:
        data = tomllib.loads(updated)
    except tomllib.TOMLDecodeError as exc:
        raise EditError(f"edit would break tasks.toml: {exc}") from exc
    _, errors = bl.validate(data.get("task", []))
    if errors:
        raise EditError("edit would fail validation: " + errors[0])
    try:
        path.write_text(updated, encoding="utf-8")
        clear_snapshot_cache()
    except OSError as exc:
        raise EditError(str(exc)) from exc


def write_status(bl: ModuleType, repo: Path, task_id: str, status: str) -> str:
    """Apply set_status to tasks.toml on disk, refusing to leave it invalid."""
    _persist(bl, repo, lambda text: set_status(text, task_id, status))
    return f"{task_id}: status -> {status}"


def write_status_and_notes(bl: ModuleType, repo: Path, task_id: str,
                           status: str, notes: str) -> str:
    """Like write_status, but rewrites `notes` in the same pass -- one
    validate, one write. Used when sending a reviewed task back to `todo`:
    the status and the reviewer's feedback change together."""
    _persist(bl, repo, lambda text: set_notes(
        set_status(text, task_id, status), task_id, notes))
    return f"{task_id}: status -> {status}, notes updated"


def write_routing(bl: ModuleType, repo: Path, task_id: str, routing: str) -> str:
    """Apply set_routing to tasks.toml on disk, refusing to leave it invalid."""
    _persist(bl, repo, lambda text: set_routing(text, task_id, routing))
    return f"{task_id}: routing -> {routing}"


def autonomous_routing_for(task: Any) -> str:
    """The routing Approve rewrites a HUMAN GATED task to: 'tester' when
    every touched path is under tests/ (the same touches-shape signal
    backlog.py's build_prompt uses to route tester delegation), 'impl'
    otherwise -- the default AUTONOMOUS routing for ordinary implementation
    work."""
    touches = task["touches"]
    if touches and all(p.startswith(("tests/", "test/")) for p in touches):
        return "tester"
    return "impl"


#: Shared branch every "Mark task done" verdict lands on, so a run of them in
#: one cockpit session becomes one PR instead of a worktree-and-PR per click.
#: A new batch starts only once this branch's PR has merged or closed.
LANDING_BRANCH = "chore/land-reviewed-tasks"


class LandError(Exception):
    """A step in landing a 'done' verdict (worktree/commit/push/PR) failed."""


def landing_worktree_path(repo: Path) -> Path:
    return repo / ".claude" / "worktrees" / "land-reviewed-tasks"


def _git_out(cwd: Path, *args: str) -> str:
    """Run git in `cwd`, returning stdout. Raises LandError on failure --
    every caller here is mid-landing with no safe way to half-apply."""
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                              text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise LandError(f"git {' '.join(args)}: {exc}") from exc
    if proc.returncode != 0:
        raise LandError(f"git {' '.join(args)} failed: "
                        f"{(proc.stderr or proc.stdout).strip()}")
    return proc.stdout.strip()


def ensure_landing_worktree(repo: Path) -> Path:
    """The shared worktree for LANDING_BRANCH: fresh off origin/main if the
    previous batch already merged or closed (or never existed), reused as-is
    while its PR is still open. Reusing it while open is what turns a run of
    "Mark task done" clicks into one PR instead of one per click."""
    worktree = landing_worktree_path(repo)
    pr = probe_pr(repo, LANDING_BRANCH)
    open_pr = pr is not None and pr.get("state") == "OPEN"
    _git_out(repo, "worktree", "prune")
    if worktree.exists() and not open_pr:
        # Stale: the previous batch's PR landed or was closed. Nothing here
        # is ever left uncommitted (every write below is followed by a
        # commit), so discarding it is always safe.
        _git_out(repo, "worktree", "remove", "--force", str(worktree))
    if worktree.exists():
        return worktree
    _git_out(repo, "fetch", "origin", "main")
    if open_pr:
        # The PR is open but this machine has no local worktree for it
        # (pruned, or a different clone landed the first task) -- track the
        # remote branch instead of re-branching off main.
        _git_out(repo, "fetch", "origin", LANDING_BRANCH)
        _git_out(repo, "worktree", "add", str(worktree), "-B", LANDING_BRANCH,
                f"origin/{LANDING_BRANCH}")
    else:
        _git_out(repo, "worktree", "add", str(worktree), "-B", LANDING_BRANCH,
                "origin/main")
    return worktree


def _landing_pr_body(worktree: Path) -> str:
    """One bullet per commit already on the branch -- the running list of
    tasks this batch lands, regenerated (not appended to) on every push."""
    log = _git_out(worktree, "log", "origin/main..HEAD", "--format=%s")
    subjects = [ln for ln in log.splitlines() if ln.strip()]
    return ("Human-reviewed tasks landing as `done` (ADR 0009) via cockpit's "
           "Review pane.\n\n" + "\n".join(f"- {s}" for s in subjects))


def land_task_done(bl: ModuleType, repo: Path, task_id: str) -> str:
    """Mark task_id 'done' in the shared LANDING_BRANCH worktree, commit and
    push it there, and open (or refresh) that branch's PR -- never touching
    `repo`'s own tasks.toml, which reflects only what main has actually
    merged. Never raises; a failure is a FAIL-prefixed result line, the same
    contract as apply_steps."""
    return land_tasks_done(bl, repo, [task_id])


def land_tasks_done(bl: ModuleType, repo: Path, task_ids: list[str]) -> str:
    """Bulk form of land_task_done: lands every id in `task_ids` into the
    same LANDING_BRANCH batch, committing each task individually, then
    pushing once and opening/refreshing the PR once at the end of the batch.
    A mid-batch failure still leaves whichever tasks committed before it,
    reporting which ids landed. Never raises, same never-raises contract as
    land_task_done."""
    if not task_ids:
        return f"ok    0 task(s) -> done ({LANDING_BRANCH})"

    try:
        worktree = ensure_landing_worktree(repo)
    except (LandError, EditError) as exc:
        if len(task_ids) == 1:
            return f"FAIL  {task_ids[0]}: {exc}"
        return f"0/{len(task_ids)} landed; FAIL  setup: {exc}"

    committed_ids: list[str] = []
    already_queued_ids: list[str] = []
    failed_result: str | None = None

    for task_id in task_ids:
        try:
            write_status(bl, worktree, task_id, "done")
            if not _git_out(worktree, "status", "--porcelain", "--", "tasks.toml"):
                already_queued_ids.append(task_id)
                continue
            _git_out(worktree, "add", "tasks.toml")
            _git_out(worktree, "commit", "-m",
                    f"tasks.toml: {task_id} status review -> done")
            committed_ids.append(task_id)
        except (LandError, EditError) as exc:
            failed_result = f"FAIL  {task_id}: {exc}"
            break

    successful_ids = committed_ids + already_queued_ids
    if successful_ids:
        try:
            _git_out(worktree, "push", "-u", "origin", LANDING_BRANCH)
            pr = probe_pr(repo, LANDING_BRANCH)
            body = _landing_pr_body(worktree)
            if pr is None or pr.get("state") != "OPEN":
                proc = subprocess.run(
                    ["gh", "pr", "create", "--head", LANDING_BRANCH, "--base", "main",
                     "--title", "Land reviewed tasks", "--body", body],
                    cwd=repo, capture_output=True, text=True, timeout=30, check=False)
                if proc.returncode != 0:
                    raise LandError((proc.stderr or proc.stdout).strip())
            else:
                subprocess.run(["gh", "pr", "edit", LANDING_BRANCH, "--body", body],
                               cwd=repo, capture_output=True, text=True,
                               timeout=30, check=False)
        except (LandError, EditError) as exc:
            if failed_result is None:
                failed_result = f"FAIL  {task_ids[0] if len(task_ids) == 1 else 'push/pr'}: {exc}"

    if failed_result is not None:
        if len(task_ids) == 1:
            return failed_result
        return f"{len(successful_ids)}/{len(task_ids)} landed; {failed_result}"

    if len(task_ids) == 1 and task_ids[0] in already_queued_ids:
        return f"ok    {task_ids[0]}: already queued in {LANDING_BRANCH}"
    if len(task_ids) == 1:
        return f"ok    {task_ids[0]}: status -> done ({LANDING_BRANCH})"
    return f"ok    {len(successful_ids)} task(s) -> done ({LANDING_BRANCH})"


def pending_landed_task_ids(bl: ModuleType, repo: Path) -> frozenset[str]:
    """Task ids already 'done' in the shared LANDING_BRANCH worktree but not
    yet in repo's own tasks.toml -- queued for that branch's PR, not stuck.
    Local file reads only, no network, so refresh() can call this every
    tick; empty if the worktree does not exist or fails to parse, the same
    failure-tolerant contract as the L1 probes."""
    worktree = landing_worktree_path(repo)
    if not worktree.is_dir():
        return frozenset()
    queued = snapshot(bl, worktree, use_git=False)
    if queued.errors:
        return frozenset()
    landed = snapshot(bl, repo, use_git=False)
    if landed.errors:
        return frozenset()
    return frozenset(tid for tid, t in queued.by_id.items()
                     if t["status"] == "done"
                     and landed.by_id.get(tid, {}).get("status") != "done")


def review_feedback_notes(reason: str, changes: str, interview: bool,
                          when: str | None = None) -> str:
    """Compose the `notes` text for a task a human review sends back to
    `todo`. This is what backlog.py's build_prompt() surfaces verbatim to
    the next dispatch, so it is written as instructions to the agent, not
    just a log entry -- and reads that way whether an operator opens
    tasks.toml directly or watches it arrive in a prompt."""
    stamp = when or time.strftime("%Y-%m-%d")
    text = (f"REVIEW FAILED ({stamp}): {reason.strip()} "
           f"Requested changes: {changes.strip()}")
    if interview:
        text += (" Before making changes, ask the operator clarifying "
                 "questions about what is required and wait for a reply "
                 "rather than guessing -- this session can be resumed with "
                 "`claude --resume <session-id>`.")
    return text


def _git(repo: Path, *args: str) -> list[str]:
    return ["git", "-C", str(repo), *args]


def plan_fix(finding: Any, repo: Path, by_id: dict[str, Any]) -> Plan:
    """Map a doctor finding to a repair plan.

    The eleven codes are documented as stable and append-only, which makes
    them the intended extension point. They are not uniformly fixable: some
    are a command, some are a decision, and the honest answer for the rest is
    an explanation. Nothing here guesses at intent.
    """
    code, subject = finding.code, finding.subject

    if code == "GIT-BRANCH-ORPHAN":
        return Plan(AUTO, f"Delete merged branch {subject}",
                    [Step(f"delete {subject} (merged, task done)",
                          argv=_git(repo, "branch", "-d", subject))])

    if code == "GIT-BRANCH-STALE":
        return Plan(GUIDED, f"Force-delete stale branch {subject}",
                    [Step(f"force-delete {subject}",
                          argv=_git(repo, "branch", "-D", subject))],
                    caution="This branch is NOT merged. Unmerged commits on it "
                            "are lost. Rebase or merge instead if unsure.")

    if code in ("GIT-BRANCH-MISSING", "TASK-STUCK"):
        task = by_id.get(subject)
        current = task["status"] if task else "?"
        tier = AUTO if code == "GIT-BRANCH-MISSING" else GUIDED
        caution = ("A branch exists and may hold work in progress; reclaiming "
                   "the task does not touch it." if code == "TASK-STUCK" else "")
        return Plan(tier, f"Return {subject} to the ready set",
                    [Step(f"tasks.toml: {subject} status {current} -> todo",
                          edit=(subject, "todo"))], caution=caution)

    if code == "GIT-DIRTY":
        stamp = time.strftime("%Y-%m-%d %H:%M")
        return Plan(GUIDED, "Stash the working tree",
                    [Step("stash tracked and untracked changes",
                          argv=_git(repo, "stash", "push", "-u", "-m",
                                    f"cockpit {stamp}"))],
                    caution="Stashing hides work in progress. `git stash pop` "
                            "restores it.")

    if code in ("ARTEFACT-MISSING", "AGENTS-MISSING", "ADR-STALE"):
        prompt = (f"{finding.message}. {finding.hint} Work only on this; make "
                  "no other changes.")
        return Plan(GUIDED, f"Dispatch an agent to address {code}",
                    [Step("launch a background Claude Code session",
                          argv=["claude", "--bg", "--permission-mode",
                                "acceptEdits", prompt])],
                    caution="This starts a real agent session in this repo.")

    return Plan(EXPLAIN, f"{code} needs a decision, not a command", [])


#: Codes whose fix is identical for every finding of that code, so a whole
#: class can be confirmed once and applied together.
BATCHABLE = frozenset({"GIT-BRANCH-ORPHAN", "GIT-BRANCH-MISSING"})


def apply_steps(bl: ModuleType, repo: Path, steps: list[Step],
                binary: str) -> list[str]:
    """Run a plan. Returns one result line per step; never raises."""
    results: list[str] = []
    for step in steps:
        if step.edit is not None:
            try:
                if step.notes is not None:
                    results.append("ok    " + write_status_and_notes(
                        bl, repo, *step.edit, step.notes))
                else:
                    results.append("ok    " + write_status(bl, repo, *step.edit))
            except EditError as exc:
                results.append(f"FAIL  {exc}")
            continue
        if step.routing_edit is not None:
            try:
                results.append("ok    " + write_routing(bl, repo, *step.routing_edit))
            except EditError as exc:
                results.append(f"FAIL  {exc}")
            continue
        if step.argv is None:
            continue
        argv = list(step.argv)
        if argv[0] == "claude":
            argv[0] = binary
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=120, check=False, cwd=repo)
        except (OSError, subprocess.SubprocessError) as exc:
            results.append(f"FAIL  {shlex.join(argv)}: {exc}")
            continue
        output = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = output[-1] if output else ""
        mark = "ok   " if proc.returncode == 0 else f"FAIL({proc.returncode})"
        results.append(f"{mark} {step.describe}{'  ' + tail if tail else ''}")
    return results


def dispatch(repo: Path, argv: list[str], binary: str) -> str:
    """Launch a dispatch command detached, logging to .cockpit/.

    Detached because the TUI owns the terminal: an agent that inherits stdout
    would paint over the interface. Backgrounded sessions show up in the FLEET
    pane through `claude agents --json` for Claude Code. For Antigravity
    dispatches (AG-15), which have no `claude agents --json` equivalent (ADR 0017),
    cockpit tracks the PID and log path to surface PID liveness, log tail
    outcomes, and branch/PR status in the FLEET pane and task inspector.
    """
    command = list(argv)
    if command and command[0] in ("claude", "agy"):
        command[0] = binary
    logs = repo / ".cockpit" / "logs"
    try:
        logs.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"FAIL  cannot create {logs}: {exc}"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log = logs / f"dispatch-{stamp}.log"
    try:
        handle = log.open("wb")
    except OSError as exc:
        return f"FAIL  cannot open {log}: {exc}"
    try:
        proc = subprocess.Popen(command, cwd=repo, stdout=handle,
                                stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)
    except (OSError, subprocess.SubprocessError) as exc:
        handle.close()
        return f"FAIL  {exc}"
    finally:
        handle.close()

    is_agy = (argv and argv[0] == "agy") or ("agy" in str(binary)) or ("agy" in str(command[0]))
    if is_agy:
        task_id = _extract_task_id(argv)
        started_at = time.time()
        meta = {
            "pid": proc.pid,
            "task_id": task_id,
            "command": list(argv),
            "started_at": started_at,
            "repo": str(repo.resolve()),
        }
        meta_path = logs / f"dispatch-{stamp}.json"
        try:
            meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        except OSError:
            meta_path = None

        _TRACKED_ANTIGRAVITY_DISPATCHES.append({
            "task_id": task_id,
            "pid": proc.pid,
            "proc": proc,
            "log_path": log,
            "started_at": started_at,
            "command": list(argv),
            "binary": binary,
            "repo": repo,
            "meta_path": meta_path,
        })

    try:
        code = proc.wait(timeout=1.0)
        if code != 0:
            try:
                err_text = log.read_text(encoding="utf-8", errors="replace").strip()
                first_line = err_text.splitlines()[0] if err_text else f"exit code {code}"
            except OSError:
                first_line = f"exit code {code}"
            return f"FAIL  {first_line}"
    except subprocess.TimeoutExpired:
        pass
    return f"ok    launched pid {proc.pid}, log {log.name}"


def talk_to_claude(repo: Path, binary: str, task: Any) -> str:
    """Launch an interactive `claude` session about `task`, attached to the
    real terminal -- not apply_steps' subprocess.run(capture_output=True)
    (that's for unattended runs, which capture output instead of painting
    it), and not dispatch()'s detached Popen-to-logfile pattern (that's for
    backgrounded runs the TUI doesn't own the terminal for). This is the
    first foreground/attached subprocess this file runs; the caller is
    responsible for suspending curses around it.
    """
    prompt = (f"Let's talk about task {task['id']}: {task['title']}\n\n"
             f"{task['description']}\n\n"
             "This is a discussion, not a dispatch -- no code changes are "
             "expected. I may ask you to help me edit this task's notes, "
             "description, or exit_criteria in tasks.toml before I dispatch it.")
    try:
        subprocess.run([binary, prompt], cwd=repo, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"FAIL  {exc}"
    return "ok    talk session ended"


# =====================================================================
# L3  PRESENTATION MODEL  --  rows and text. Computes what to show;
#     draws nothing.
# =====================================================================

#: ANALYTICS and WORKFLOW both sit before USAGE so active_panes()'s
#: trailing-slice fallback (drop the last entry when usage.py can't be
#: loaded) still only ever drops USAGE -- neither pane is gated on an
#: optional sibling module: ANALYTICS's git-log series need no usage.py at
#: all (only its token-trend/model-matrix rows fall back to their own
#: "usage.py not found" line when it's missing), and WORKFLOW is always
#: static.
PANES = ("BACKLOG", "DOCTOR", "FLEET", "NEXT", "ANALYTICS", "WORKFLOW", "USAGE")
#: "in_progress" renders separately, first -- see backlog_rows -- so its
#: position here only governs the remaining buckets' order.
BUCKET_ORDER = ("in_progress", "ready", "needs_routing", "blocked", "held")


def active_panes(usage_mod: ModuleType | None) -> tuple[str, ...]:
    """PANES, minus USAGE when usage.py couldn't be loaded -- the fallback
    that keeps cockpit.py standalone without usage.py present."""
    return PANES if usage_mod is not None else PANES[:-1]


def next_pane(current: int, key: int, panes: tuple[str, ...]) -> int:
    """Resolve a keypress against the tab bar: Tab/Right cycles forward,
    Left cycles backward, a digit 1..len(panes) jumps directly -- relative
    to `panes`, not a hard-coded pane count, so a pane list that grows or
    shrinks (see active_panes) never leaves a tab unreachable. Any other key
    returns `current` unchanged."""
    import curses

    if key in (ord("\t"), curses.KEY_RIGHT):
        return (current + 1) % len(panes)
    if key == curses.KEY_LEFT:
        return (current - 1) % len(panes)
    if ord("1") <= key < ord("1") + len(panes):
        return key - ord("1")
    return current


def _wrap(text: str, width: int) -> list[str]:
    out: list[str] = []
    for para in text.splitlines() or [""]:
        line = ""
        for word in para.split():
            if line and len(line) + 1 + len(word) > width:
                out.append("  " + line)
                line = word
            else:
                line = f"{line} {word}".strip()
        out.append("  " + line)
    return out


def file_tree(paths: list[str]) -> list[str]:
    """Render repo-relative paths as an indented tree, directory levels
    collapsed the way `tree` draws them -- the shape asked for when a PR
    touches enough files that a flat list stops being readable."""
    root: dict[str, Any] = {}
    for p in paths:
        node = root
        for part in p.split("/"):
            node = node.setdefault(part, {})

    lines: list[str] = []

    def walk(node: dict[str, Any], prefix: str) -> None:
        items = sorted(node.items())
        for i, (name, child) in enumerate(items):
            last = i == len(items) - 1
            lines.append(prefix + ("└── " if last else "├── ") + name)
            walk(child, prefix + ("    " if last else "│   "))

    walk(root, "")
    return lines


class DependencyNode(NamedTuple):
    task_id: str
    section: str
    depth: int
    prefix: str
    label: str
    has_children: bool
    is_expanded: bool
    parent_id: str | None
    node_key: tuple[str, str, int]


def build_inverse_depends_map(by_id: dict[str, Any]) -> dict[str, list[str]]:
    """Inverse depends map: task_id -> list of task_ids that name it in their depends."""
    inv: dict[str, list[str]] = {tid: [] for tid in by_id}
    for tid, tdict in by_id.items():
        if isinstance(tdict, dict):
            for dep in tdict.get("depends", []):
                if dep in inv:
                    inv[dep].append(tid)
                else:
                    inv[dep] = [tid]
    for k in inv:
        inv[k].sort()
    return inv


def build_ancestor_tree(task_id: str, by_id: dict[str, Any],
                        path: tuple[str, ...] = ()) -> dict[str, Any]:
    """Recursively walk depends upward. Asserts no cycles exist."""
    assert task_id not in path, f"Cycle detected in depends involving {task_id}"
    task = by_id.get(task_id, {})
    deps = sorted(task.get("depends", [])) if isinstance(task, dict) else []
    tree: dict[str, Any] = {}
    new_path = path + (task_id,)
    for dep_id in deps:
        if dep_id in by_id:
            tree[dep_id] = build_ancestor_tree(dep_id, by_id, new_path)
        else:
            tree[dep_id] = {}
    return tree


def build_descendant_tree(task_id: str, inv_map: dict[str, list[str]],
                          path: tuple[str, ...] = ()) -> dict[str, Any]:
    """Recursively walk inverse depends downward. Asserts no cycles exist."""
    assert task_id not in path, f"Cycle detected in inverse depends involving {task_id}"
    children = inv_map.get(task_id, [])
    tree: dict[str, Any] = {}
    new_path = path + (task_id,)
    for child_id in children:
        tree[child_id] = build_descendant_tree(child_id, inv_map, new_path)
    return tree


def dependency_tree(
        task_id: str, by_id: dict[str, Any],
        collapsed_nodes: set[tuple[str, str, int]] | None = None,
) -> tuple[list[DependencyNode], list[str], list[str]]:
    """Build ancestor (upward depends) and descendant (downward inverse depends)
    trees for a task in box-drawing format using the same walk style as file_tree.
    Returns (visible_nodes, ancestor_lines, descendant_lines)."""
    anc_tree = build_ancestor_tree(task_id, by_id)
    inv_map = build_inverse_depends_map(by_id)
    desc_tree = build_descendant_tree(task_id, inv_map)

    visible_nodes: list[DependencyNode] = []
    anc_lines: list[str] = []
    desc_lines: list[str] = []

    def walk(section: str, tree: dict[str, Any], prefix: str, depth: int,
             parent_id: str | None, lines_out: list[str]) -> None:
        items = sorted(tree.items())
        for i, (tid, child_dict) in enumerate(items):
            last = i == len(items) - 1
            has_children = bool(child_dict)
            node_key = (section, tid, depth)
            is_collapsed = collapsed_nodes is not None and node_key in collapsed_nodes
            is_expanded = has_children and not is_collapsed

            node_prefix = prefix + ("└── " if last else "├── ")
            label = node_prefix + tid

            node = DependencyNode(
                task_id=tid,
                section=section,
                depth=depth,
                prefix=node_prefix,
                label=label,
                has_children=has_children,
                is_expanded=is_expanded,
                parent_id=parent_id,
                node_key=node_key
            )
            visible_nodes.append(node)
            lines_out.append(label)

            if is_expanded:
                child_prefix = prefix + ("    " if last else "│   ")
                walk(section, child_dict, child_prefix, depth + 1, tid, lines_out)

    walk("ancestors", anc_tree, "", 0, None, anc_lines)
    walk("descendants", desc_tree, "", 0, None, desc_lines)

    return visible_nodes, anc_lines, desc_lines



def read_agent_log_tail(agent: Agent | None = None, repo: Path | None = None,
                        max_lines: int = 5) -> list[str]:
    """Read recent tail stream lines from a live agent's transcript or dispatch log."""
    if agent and agent.kind == "agy":
        for item in list(_TRACKED_ANTIGRAVITY_DISPATCHES):
            log_path = Path(item["log_path"]) if item.get("log_path") else None
            matched = log_path is not None and (
                log_path.name == agent.session_id or item.get("pid") == agent.pid
            )
            if matched and log_path.is_file():
                try:
                    text = log_path.read_text(encoding="utf-8", errors="replace")
                    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                    if lines:
                        return lines[-max_lines:]
                except OSError:
                    pass
    if agent and agent.cwd and agent.session_id:
        path = transcript(agent.cwd, agent.session_id)
        if path and path.is_file():
            try:
                st_size = path.stat().st_size
                cache_key = (str(path.resolve()), st_size, max_lines)
                if cache_key in _LOG_TAIL_CACHE:
                    return _LOG_TAIL_CACHE[cache_key]

                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                tail_msgs: list[str] = []
                for raw in reversed(lines):
                    if not raw.strip():
                        continue
                    try:
                        record = json.loads(raw)
                        message = record.get("message")
                        if isinstance(message, dict):
                            content = message.get("content")
                            if isinstance(content, str) and content.strip():
                                tail_msgs.append(content.strip().splitlines()[-1])
                            elif isinstance(content, list):
                                for item in content:
                                    if isinstance(item, dict) and item.get("text"):
                                        tail_msgs.append(item["text"].strip().splitlines()[-1])
                        if len(tail_msgs) >= max_lines:
                            break
                    except (ValueError, UnicodeDecodeError):
                        continue
                if tail_msgs:
                    res = list(reversed(tail_msgs))
                    _LOG_TAIL_CACHE[cache_key] = res
                    return res
            except OSError:
                pass
    if repo:
        logs_dir = repo / ".cockpit" / "logs"
        if logs_dir.is_dir():
            logs = sorted(logs_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
            if logs:
                try:
                    log_file = logs[0]
                    st_size = log_file.stat().st_size
                    cache_key = (str(log_file.resolve()), st_size, max_lines)
                    if cache_key in _LOG_TAIL_CACHE:
                        return _LOG_TAIL_CACHE[cache_key]

                    text = log_file.read_text(encoding="utf-8", errors="replace")
                    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                    if lines:
                        res = lines[-max_lines:]
                        _LOG_TAIL_CACHE[cache_key] = res
                        return res
                except OSError:
                    pass
    return ["(no active log stream)"]


def task_detail_lines(task: Any, agent: Agent | None = None,
                      worktree: Path | str | None = None,
                      pr: dict[str, Any] | None = None,
                      repo: Path | None = None,
                      by_id: dict[str, Any] | None = None,
                      tree_cursor: int | None = None,
                      collapsed_nodes: set[tuple[str, str, int]] | None = None
                      ) -> list[_PanelLine]:
    """Everything the Backlog pane's detail view shows for one task. Shared
    by the "Show task detail" modal and the cursor-driven right-hand panel,
    so the two never drift apart.

    Returns `list[_PanelLine]`: plain strings for prose, `(text, 'id')` for
    the header line carrying the task id, and `(label, 'head')` for every
    field-label line -- the same tone vocabulary the row list already uses.
    """
    retries = task.get("retries", task.get("retry_count", 0))
    rationale = task.get("rationale")
    notes = task.get("notes")
    snippets = task.get("block_snippets", task.get("snippets"))
    token_burn = task.get("token_burn")
    if token_burn is None and agent and agent.usage:
        token_burn = f"{agent.usage.context:,} ctx (${agent.usage.cost:.4f})"
    elif token_burn is None:
        token_burn = "0 ctx ($0.00)"

    wt_str = str(worktree) if worktree else str(task.get("worktree") or "none")
    log_tail = task.get("log_tail") or read_agent_log_tail(agent, repo)
    history = task.get("session_history") or task.get("history")

    header = (f"{task['id']}  p{task['phase']}  {task['routing']}  "
              f"{task['status']}")
    lines: list[_PanelLine] = [
        (header, "id"), "", task["title"], "",
        *_wrap(task["description"], 76)]

    if rationale:
        lines += ["", ("Rationale:", "head"), *_wrap(str(rationale), 76)]
    if notes:
        lines += ["", ("Notes:", "head"), *_wrap(str(notes), 76)]

    lines += ["", ("Exit criteria:", "head"),
              *[f"  - {c}" for c in task.get("exit_criteria", [])], "",
              ("Touches:", "head"),
              *[f"  {p}" for p in task.get("touches", [])]]

    task_id = task.get("id", "")
    if by_id is not None and task_id:
        visible_nodes, anc_lines, desc_lines = dependency_tree(
            task_id, by_id, collapsed_nodes=collapsed_nodes)

        lines += ["", ("Ancestors:", "head")]
        if anc_lines:
            anc_nodes = [n for n in visible_nodes if n.section == "ancestors"]
            for node in anc_nodes:
                vis_idx = visible_nodes.index(node)
                mark = "> " if (tree_cursor is not None and tree_cursor == vis_idx) else "  "
                lines.append(f"{mark}{node.label}")
        else:
            lines.append("  (none)")

        lines += ["", ("Descendants:", "head")]
        if desc_lines:
            desc_nodes = [n for n in visible_nodes if n.section == "descendants"]
            for node in desc_nodes:
                vis_idx = visible_nodes.index(node)
                mark = "> " if (tree_cursor is not None and tree_cursor == vis_idx) else "  "
                lines.append(f"{mark}{node.label}")
        else:
            lines.append("  (none)")
    else:
        lines += ["", "Depends: " + (", ".join(task.get("depends", [])) or "none")]

    if snippets:
        lines += ["", "Block Snippets:"]
        if isinstance(snippets, list):
            lines += [f"  {s}" for s in snippets]
        else:
            lines += _wrap(str(snippets), 74)

    lines += ["",
              f"Worktree: {wt_str}",
              f"Retries: {retries}",
              f"Token burn: {token_burn}",
              "",
              ("Live Log Tail:", "head"),
              *[f"  {ln}" for ln in (log_tail if isinstance(log_tail, list) else [str(log_tail)])],
              "",
              ("Session History:", "head")]
    if history:
        if isinstance(history, list):
            lines += [f"  {h}" for h in history]
        else:
            lines += [f"  {history}"]
    elif agent:
        lines.append(f"  Session {agent.session_id} (pid {agent.pid}, {agent.status})")
    else:
        lines.append("  (no session history)")

    return lines


def finding_detail_lines(finding: Any, plan: Plan, repo: Path | None = None) -> list[str]:
    """"Explain this finding": what is true, the recommended fix, and its
    repair tier. Shared by the Doctor pane's menu and its right-hand
    panel."""
    subject = str(getattr(finding, "subject", ""))
    wt = str(repo) if repo else (subject if subject else "none")
    retries = getattr(finding, "retries", getattr(finding, "retry_count", 0))
    log_tail = read_agent_log_tail(None, repo)

    lines = [f"subject: {finding.subject}", f"level:   {finding.level}", "",
           "What is true:", "  " + finding.message, "",
           "Recommended resolution:", "  " + finding.hint, "",
           f"Repair tier: {plan.tier}",
           "  auto    - a safe command or status edit",
           "  guided  - possible, but it is a judgement call",
           "  explain - no mechanical fix; decide and act yourself",
           "",
           f"Worktree: {wt}",
           f"Retries: {retries}",
           "Token burn: 0 ctx ($0.00)",
           "",
           "Live Log Tail:",
           *[f"  {ln}" for ln in log_tail],
           "",
           "Session History:",
           "  (no session history)"]
    return lines


def agent_detail_lines(agent: Agent, repo: Path | None = None) -> list[str]:
    """Everything the Fleet pane's detail view shows for one agent. Shared
    by its modal and the right-hand panel."""
    use = agent.usage
    lines = [f"session {agent.session_id}", f"pid     {agent.pid}",
            f"kind    {agent.kind}", f"status  {agent.status} {agent.state}",
            f"age     {human_age(agent.age)}", f"cwd     {agent.cwd}", ""]
    if use:
        lines += [f"model         {use.model}",
                 f"context       {use.context:,} / {use.window:,} "
                 f"({round(100 * use.context / use.window)}%)",
                 f"last turn in  {use.input:,}",
                 f"last turn out {use.output:,}",
                 f"cache read    {use.cache_read:,}",
                 f"cache write   {use.cache_write:,}",
                 f"est. cost     ${use.cost:.4f} (last turn, advisory)"]
    else:
        lines.append("no transcript found for this session")
    lines += ["", f"Attach with:  claude --resume {agent.session_id}"]

    retries = getattr(agent, "retries", 0)
    token_burn = f"{use.context:,} ctx (${use.cost:.4f})" if use else "0 ctx ($0.00)"
    log_tail = read_agent_log_tail(agent, repo)

    lines += ["",
              f"Worktree: {agent.cwd}",
              f"Retries: {retries}",
              f"Token burn: {token_burn}",
              "",
              "Live Log Tail:",
              *[f"  {ln}" for ln in log_tail],
              "",
              "Session History:",
              f"  Session {agent.session_id} (pid {agent.pid}, {agent.status})"]
    return lines


def lane_detail_lines(lane: Any, repo: Path | None = None) -> list[str]:
    """Everything the Fleet pane's detail view shows for one lane plan
    entry. Shared by its modal and the right-hand panel."""
    lines = ["tasks:", *[f"  {t}" for t in lane.tasks], "",
            "holds paths:", *[f"  {p}" for p in lane.touches], "",
            f"Worktree: {repo or 'none'}",
            "Retries: 0",
            "Token burn: 0 ctx ($0.00)",
            "",
            "Live Log Tail:",
            "  (no active log stream)",
            "",
            "Session History:",
            "  (no session history)"]
    return lines


def next_detail_lines(action: Any, repo: Path | None = None) -> list[str]:
    """Everything the Next pane's detail view shows: the full command and
    prompt when one is queued, otherwise the plain-language reason."""
    if not action.command:
        lines = _wrap(action.reason, 76)
    else:
        lines = ["Command:", *_wrap(shlex.join(action.command[:-1]), 74), "",
                 "Prompt:"]
        lines += [f"  {ln}" for ln in action.command[-1].splitlines()]
    lines += ["",
              f"Worktree: {repo or 'none'}",
              "Retries: 0",
              "Token burn: 0 ctx ($0.00)",
              "",
              "Live Log Tail:",
              "  (no active log stream)",
              "",
              "Session History:",
              "  (no session history)"]
    return lines


def review_detail_lines(task: Any, pr: dict[str, Any] | None,
                        branch: str | None, agent: Agent | None = None,
                        repo: Path | None = None,
                        by_id: dict[str, Any] | None = None,
                        tree_cursor: int | None = None,
                        collapsed_nodes: set[tuple[str, str, int]] | None = None) -> list[str]:
    """Everything the Review pane's detail view shows for one task: its
    description and exit criteria, then -- when a PR was found -- its
    summary, diff stats, and a tree of changed files. Falls back to plain
    guidance when `gh` or the branch itself is unavailable rather than
    failing (same contract as the doctor findings), since "a link to the
    closed PR is sufficient" when nothing richer can be fetched.
    """
    lines = [f"{task['id']}  p{task['phase']}  {task['routing']}  review",
             "", task["title"], "", *_wrap(task["description"], 76), "",
             "Exit criteria:",
             *[f"  - {c}" for c in task["exit_criteria"]]]

    task_id = task.get("id", "")
    if by_id is not None and task_id:
        visible_nodes, anc_lines, desc_lines = dependency_tree(
            task_id, by_id, collapsed_nodes=collapsed_nodes)

        lines += ["", "Ancestors:"]
        if anc_lines:
            anc_nodes = [n for n in visible_nodes if n.section == "ancestors"]
            for node in anc_nodes:
                vis_idx = visible_nodes.index(node)
                mark = "> " if (tree_cursor is not None and tree_cursor == vis_idx) else "  "
                lines.append(f"{mark}{node.label}")
        else:
            lines.append("  (none)")

        lines += ["", "Descendants:"]
        if desc_lines:
            desc_nodes = [n for n in visible_nodes if n.section == "descendants"]
            for node in desc_nodes:
                vis_idx = visible_nodes.index(node)
                mark = "> " if (tree_cursor is not None and tree_cursor == vis_idx) else "  "
                lines.append(f"{mark}{node.label}")
        else:
            lines.append("  (none)")
    if branch is None:
        lines += ["", "No local branch found for this task; check "
                  f"`gh pr list --search {task['id']}`."]
    else:
        lines += ["", f"Branch: {branch}"]
        if pr is None:
            lines += ["", f"No PR found for {branch}. Check "
                      f"`gh pr list --head {branch}`."]
        else:
            lines += ["", f"PR #{pr.get('number')}  {pr.get('state', '?')}  "
                      f"{pr.get('url', '')}",
                      f"+{pr.get('additions', 0)} -{pr.get('deletions', 0)}  "
                      f"{pr.get('changedFiles', 0)} file(s)"]
            body = str(pr.get("body") or "").strip()
            if body:
                lines += ["", "Summary:", *_wrap(body, 76)]
            files = [f.get("path", "") for f in pr.get("files") or []
                    if isinstance(f, dict) and f.get("path")]
            if files:
                lines += ["", "Files changed:", *[f"  {ln}" for ln in file_tree(files)]]

    wt_str = str(agent.cwd) if agent and agent.cwd else (
        str(repo / ".claude" / "worktrees" / branch.replace("task/", ""))
        if branch and repo else "none"
    )
    retries = task.get("retries", task.get("retry_count", 0))
    token_burn = task.get("token_burn")
    if token_burn is None and agent and agent.usage:
        token_burn = f"{agent.usage.context:,} ctx (${agent.usage.cost:.4f})"
    elif token_burn is None:
        token_burn = "0 ctx ($0.00)"

    log_tail = task.get("log_tail") or read_agent_log_tail(agent, repo)
    history = task.get("session_history") or task.get("history")

    lines += ["",
              f"Worktree: {wt_str}",
              f"Retries: {retries}",
              f"Token burn: {token_burn}",
              "",
              "Live Log Tail:",
              *[f"  {ln}" for ln in (log_tail if isinstance(log_tail, list) else [str(log_tail)])],
              "",
              "Session History:"]
    if history:
        if isinstance(history, list):
            lines += [f"  {h}" for h in history]
        else:
            lines += [f"  {history}"]
    elif agent:
        lines.append(f"  Session {agent.session_id} (pid {agent.pid}, {agent.status})")
    else:
        lines.append("  (no session history)")

    return lines


#: Same glyph vocabulary backlog.py renders with, so the two tools read as one
#: system. Duplicated rather than imported off `bl` because these are drawing
#: primitives, not classification rules -- and backlog_rows()/doctor_rows()/
#: fleet_rows() keep their existing (snap[, agents, daily]) signatures, which
#: the test suite calls positionally.
TL, TR, BL, BR, H, V = "╭", "╮", "╰", "╯", "─", "│"
FULL, EMPTY = "█", "░"
DOT, ARROW = "●", "→"

#: Black-Red Tactical Dark Theme 256-color definitions
COLOR_CRIMSON_RED = 196  # #FF3333
COLOR_EMBER_AMBER = 208  # #FF8800
COLOR_CYAN_ACCENT = 45   # #00D7FF
COLOR_EMERALD_GREEN = 46 # #00FF5F

#: Semantic tone mapping to color numbers (256-color index vs 8-color fallback)
THEME_PALETTE: dict[str, tuple[int, int]] = {
    "good": (COLOR_EMERALD_GREEN, 2),    # emerald green / COLOR_GREEN
    "warn": (COLOR_EMBER_AMBER, 3),      # ember amber / COLOR_YELLOW
    "bad": (COLOR_CRIMSON_RED, 1),       # crimson red / COLOR_RED
    "info": (COLOR_CYAN_ACCENT, 4),      # cyan accent / COLOR_BLUE
    "busy": (COLOR_CYAN_ACCENT, 6),      # cyan accent / COLOR_CYAN
    "head": (7, 7),                      # COLOR_WHITE
    "id": (COLOR_CRIMSON_RED, 5),        # crimson red / COLOR_MAGENTA
    "orange": (COLOR_EMBER_AMBER, 3),    # ember amber / COLOR_YELLOW
    "crimson": (COLOR_CRIMSON_RED, 1),   # crimson red / COLOR_RED
    "amber": (COLOR_EMBER_AMBER, 3),     # ember amber / COLOR_YELLOW
    "emerald": (COLOR_EMERALD_GREEN, 2), # emerald green / COLOR_GREEN
    "cyan": (COLOR_CYAN_ACCENT, 6),      # cyan accent / COLOR_CYAN
}

#: Tab -> tone: each pane gets the colour of the question it answers, so the
#: chrome itself hints at what is inside before a single row is read.
PANE_TONE = {"BACKLOG": "good", "DOCTOR": "warn", "FLEET": "busy", "NEXT": "info",
            "ANALYTICS": "cyan", "WORKFLOW": "id", "USAGE": "orange"}

#: Bucket -> tone, identical to backlog.py's BUCKET_COLOUR (green/cyan/blue/
#: yellow/red) so a bucket means the same colour in both tools.
BUCKET_TONE = {"ready": "good", "in_progress": "busy", "needs_routing": "info",
               "blocked": "warn", "held": "bad"}

#: Bucket -> display label, only where it differs from the bucket key
#: uppercased: "needs_routing" is backlog.py's classify() vocabulary
#: (SPEC-level, shared with the API and nsctl), but the Backlog tab reads
#: better calling it what it is for a human -- HUMAN GATED.
BUCKET_LABEL = {"needs_routing": "HUMAN GATED"}

#: id-prefix -> one-line "what this type of task is for" blurb, shown once
#: per section in the BACKLOG pane's type-grouped view (backlog_rows_by_type).
#: Cockpit-local presentation data, not a manifest field -- ADR 0002 bans
#: forking the tasks.toml schema, and a human-readable gloss on an id prefix
#: is exactly the kind of thing that stays out of it. A prefix missing here
#: (a new type, or a typo) just renders its section with no blurb line.
TASK_TYPE_GOALS: dict[str, str] = {
    "AG": "Scheduler and backend dispatch: pick a model/backend, run agents unattended",
    "AP": "REST API and SSE: the daemon's HTTP surface for nsctl and the dashboard",
    "BL": "Backlog ordering: NightShift-local task sequencing persisted in SQLite",
    "CK": "Cockpit TUI: the operator's live terminal view into backlog, fleet and usage",
    "CL": "nsctl CLI: status, queue, block, control and logs from the terminal",
    "CO": "Core models and manifest I/O: domain types, tasks.toml reader/writer, SQLite",
    "DG": "Digest and notification delivery: built summaries that actually get sent",
    "DX": "Landing: commit, push and open the PR once a task's work is done",
    "E2": "End-to-end dry runs: FakeClaude, local bare origin, no network",
    "EF": "Model/effort tiering: route tasks to the right size class",
    "EN": "Task decomposition with a human approval gate",
    "EX": "Execution policy and prompt templates for the agent subprocess",
    "MT": "Metrics aggregation and Prometheus exposition",
    "NT": "Notifier fan-out: ntfy, webhook and SMTP channels",
    "OP": "Daemon entrypoint: process wiring, uvicorn, logging, signals",
    "PX": "ADR 0021: bounded parallel task execution",
    "RS": "ADR 0023: attempt ceiling and orphan reclamation",
    "RV": "ADR 0022: independent review gate before the status write",
    "SB": "Sandbox: git worktree lifecycle for agent runs",
    "UI": "Web dashboard SPA",
    "UM": "Usage metrics: turns, sessions and cost from transcripts",
    "VC": "VCS providers: commit, push, PR/MR creation and pipeline status",
    "VF": "VerifyRunner: deterministic gate execution and output capping",
}


class Row(NamedTuple):
    text: str            # plain concatenation -- what tests search and what
                          # renders when `segments` is absent
    tone: str             # colour key, used as-is when segments is None
    payload: Any = None  # the object this row selects, or None for headings
    segments: tuple[tuple[str, str], ...] | None = None  # (text, tone) parts;
                          # when set, the TUI paints each part in its own
                          # colour instead of the single `tone` above


def seg(*parts: tuple[str, str]) -> tuple[str, tuple[tuple[str, str], ...]]:
    """Build (plain_text, segments) from (text, tone) parts.

    One accent per column, not one accent per row: an identifier is always
    the 'id' tone, a bucket marker is always its bucket tone, a hint is
    always dim -- regardless of which pane or bucket it appears in.
    """
    return "".join(text for text, _ in parts), parts


def bar_segments(counts: list[tuple[int, str]], width: int = 28) -> list[tuple[str, str]]:
    """A proportional bar as (block, tone) parts -- the curses analogue of
    backlog.py's bar(). Every non-empty segment keeps at least one cell."""
    total = sum(n for n, _ in counts)
    if not total:
        return [(EMPTY * width, "dim")]
    cells: list[tuple[str, str]] = []
    used = 0
    for i, (count, tone) in enumerate(counts):
        if not count:
            continue
        size = width - used if i == len(counts) - 1 else max(
            1, round(count / total * width))
        size = min(size, max(0, width - used))
        cells.append((FULL * size, tone))
        used += size
    if used < width:
        cells.append((EMPTY * (width - used), "dim"))
    return cells


def pct_tone(pct: int) -> str:
    """Threshold colour for a percentage -- context usage, budget burn, etc.
    Informative, not decorative: the colour is the reading, not a label."""
    if pct >= 90:
        return "bad"
    if pct >= 70:
        return "warn"
    return "good"


def status_tone(status: str) -> str:
    """Colour key for a status or result string (e.g. 'done', 'active', 'busy', 'failed')."""
    low = status.lower()
    if any(k in low for k in ("fail", "error", "blocked", "bad")):
        return "bad"
    if low.startswith("ok") or low.endswith("ok") or any(
            k in low for k in ("launched", "active", "done", "good", "ready", "passed")):
        return "good"
    if any(k in low for k in
           ("cancel", "nothing", "wip", "busy", "warn", "in_progress", "review", "holding")):
        return "warn"
    return "head"


def status_badge(status: str, tone: str | None = None) -> tuple[str, tuple[tuple[str, str], ...]]:
    """Render a status badge primitive, e.g. `● ACTIVE`."""
    t = tone or status_tone(status)
    label = status.upper()
    return seg((f"{DOT} ", t), (label, t))


def progress_bar(
        pct: int, width: int = 10,
        tone: str | None = None) -> tuple[str, tuple[tuple[str, str], ...]]:
    """Render a segmented progress meter bar primitive, e.g. `██████░░░░ 60%`."""
    pct_clamped = max(0, min(100, pct))
    filled_count = round(pct_clamped / 100 * width)
    empty_count = width - filled_count
    t = tone or pct_tone(pct_clamped)
    filled_str = FULL * filled_count
    empty_str = EMPTY * empty_count
    return seg((filled_str, t), (empty_str, "dim"), (f" {pct_clamped}%", "head"))


def render_sparkline(values: Sequence[int | float], width: int | None = None) -> str:
    """Render a token trend sparkline primitive from block glyphs."""
    if not values:
        return EMPTY * (width or 0)
    vals = list(values)
    if width is not None and width > 0:
        if len(vals) > width:
            vals = vals[-width:]
        elif len(vals) < width:
            vals = [0] * (width - len(vals)) + vals
    peak = max(vals) if vals else 1
    if peak <= 0:
        peak = 1
    max_idx = len(SPARKLINE_GLYPHS) - 1
    return "".join(
        SPARKLINE_GLYPHS[min(max_idx, max(0, round(v / peak * max_idx)))]
        for v in vals
    )


def box_enclosure(
        title: str, lines: Sequence[str], footer: str = "",
        width: int | None = None) -> list[str]:
    """Render a box enclosure primitive with rounded corners (╭──╮)."""
    text_lines = list(lines)
    max_line_w = max((len(ln) for ln in text_lines), default=0)
    title_w = len(title) + 4 if title else 0
    footer_w = len(footer) + 4 if footer else 0
    content_w = max(max_line_w, title_w, footer_w, 0)
    w = max(width or (content_w + 4), 4)
    inner_w = w - 2

    res: list[str] = []
    if title:
        t_str = f" {title} "
        rem = max(0, inner_w - len(t_str))
        res.append(f"{TL}{t_str}{H * rem}{TR}")
    else:
        res.append(f"{TL}{H * inner_w}{TR}")

    for line in text_lines:
        res.append(f"{V}{line:<{inner_w}}{V}")

    if footer:
        f_str = f" {footer} "
        rem = max(0, inner_w - len(f_str))
        res.append(f"{BL}{f_str}{H * rem}{BR}")
    else:
        res.append(f"{BL}{H * inner_w}{BR}")

    return res


def human_tokens(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.0f}k"
    return str(count)


def human_age(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


class DispatchRecommendation(NamedTuple):
    backend: str
    model: str
    effort: str | None


def recommend_dispatch(bl: ModuleType, task: Any,
                       families: dict[str, str]) -> DispatchRecommendation:
    """A starting-point Backend/Model/Effort for `task`, spread across the
    three quotas AG-14 gave this cockpit (Claude's own Pro window,
    Antigravity's Gemini quota, Antigravity's bundled 'other' quota) by task
    size rather than always defaulting to Claude. Used both to seed the
    dispatch picker (`dispatch_task()`, L4) and to group the READY bucket by
    backend below -- defined here, in L3, so this L3 presentation code can
    call it without an upward reference into L4.

    Size is derived from `task`'s own shape -- touches + exit_criteria --
    the same crude S/M/L signal EF-01's size_class() uses, but computed here
    independently: this file has no `src.nightshift` import (module
    docstring) and this mapping is its own reviewer-tunable heuristic (task
    CK-06 notes), not a share of policy.py's. S -> antigravity (Gemini
    family, no effort field); M -> antigravity-other (the bundled
    allotment, no effort field either); L -> claude, opus, high effort.

    A task's own `model` key, if present, still wins for `.model` -- same
    precedence EF-01 gives a nightshift.toml model override -- but never
    changes the recommended `.backend`/`.effort`, which stay purely
    size-derived.

    `families` is accepted, unused, to keep this call's signature the same
    shape as `_dispatch_fields`/`_agy_backend_labels` -- the fixed S/M/L
    mapping above doesn't need it, but a future family-aware revision
    (CK-06 notes: "adjust the constants here" if the heuristic proves wrong)
    would.
    """
    weight = len(task["touches"]) + len(task["exit_criteria"])
    size = "S" if weight <= 3 else "M" if weight <= 7 else "L"
    override = task.get("model")

    if size == "L":
        return DispatchRecommendation("claude", override or "opus", "high")
    if size == "M":
        default_model = getattr(bl, "AGY_OTHER_MODEL", "claude-sonnet-4-6")
        return DispatchRecommendation("antigravity-other", override or default_model, None)
    return DispatchRecommendation("antigravity", override or bl.AGY_MODEL, None)


#: READY-bucket sub-heading per recommend_dispatch() backend, in display
#: order -- CK-06. Only non-empty groups render (mirrors how every other
#: bucket here is skipped when empty).
_READY_BACKEND_LABELS = (("claude", "CLAUDE"), ("antigravity", "ANTIGRAVITY"),
                         ("antigravity-other", "ANTIGRAVITY-OTHER"))


def _group_ready_by_backend(bl: ModuleType, tasks: list[Any],
                            families: dict[str, str]) -> list[tuple[str, list[Any]]]:
    grouped: dict[str, list[Any]] = {backend: [] for backend, _ in _READY_BACKEND_LABELS}
    for task in tasks:
        grouped[recommend_dispatch(bl, task, families).backend].append(task)
    return [(label, grouped[backend]) for backend, label in _READY_BACKEND_LABELS
           if grouped[backend]]


def _current_buckets(
        snap: Snapshot, agents: Sequence[Agent]) -> tuple[dict[str, list[Any]], dict[str, Agent]]:
    """Bucket assignment reflecting live agent claims, not just tasks.toml's
    status field: a task an agent has already picked up shows as
    in_progress even if the manifest hasn't caught up yet. Shared by
    backlog_rows() and its type-grouped counterpart so "current bucket"
    means the same thing in both BACKLOG views."""
    buckets = {bucket: list(tasks) for bucket, tasks in snap.buckets.items()}
    running: dict[str, Agent] = {}
    for bucket, tasks in list(buckets.items()):
        if bucket == "in_progress":
            continue
        kept = []
        for task in tasks:
            agent = agent_for_task(task["id"], agents)
            if agent:
                running[task["id"]] = agent
                buckets["in_progress"].append(task)
            else:
                kept.append(task)
        buckets[bucket] = kept
    buckets["in_progress"].sort(key=lambda t: (t["phase"], t["id"]))
    return buckets, running


def _bucket_note(bucket: str, task: Any, snap: Snapshot,
                 running: dict[str, Agent], pending: frozenset[str]) -> str:
    """The per-task explanation shown next to a bucketed row -- why it's
    stuck, who's running it, or that it's parked. One source of truth so
    backlog_rows()'s section() and backlog_rows_by_type() read the same
    bucket the same way."""
    if bucket == "blocked":
        waiting = sorted(
            d for d in task["depends"]
            if d in snap.by_id
            and snap.by_id[d]["status"] not in ("done", "review"))
        return "waits on " + ", ".join(waiting)
    if bucket == "needs_routing":
        return f"{task['routing']} - human gated"
    if bucket == "in_progress":
        agent = running.get(task["id"])
        if agent:
            flag = f"{agent.status}/{agent.state}" if agent.state else agent.status
            return f"agent {flag} -- {human_age(agent.age)}"
        return task["routing"]
    if bucket == "held":
        return "held"
    if bucket == "review":
        queued = task["id"] in pending
        return f"queued -- pending {LANDING_BRANCH}" if queued else "awaiting human review"
    return ""


def backlog_rows(snap: Snapshot, pending: frozenset[str] = frozenset(),
                 agents: Sequence[Agent] = (), bl: ModuleType | None = None,
                 families: dict[str, str] | None = None) -> list[Row]:
    """`pending` is the pending_landed_task_ids() result: tasks already
    queued in LANDING_BRANCH's PR, so a row can say so instead of reading as
    still awaiting a decision nobody has made yet.

    `agents` is the Fleet pane's live agent list. A task claimed by one of
    them (matched via agent_for_task) is dispatched right now, regardless of
    what tasks.toml's status field still says -- it is shown under IN
    PROGRESS, not its manifest bucket, so a task never reads as Ready while
    an agent is already working it.

    `bl`/`families`, when given, split the READY section into CLAUDE/
    ANTIGRAVITY/ANTIGRAVITY-OTHER sub-headings by recommend_dispatch()
    (CK-06), so an operator can see at a glance which quota each ready task
    would spend. Left as `None` (the default), READY renders as one section,
    same as before CK-06 -- `bl` is the only piece of this call that isn't
    already reachable from `snap` alone, so it gates the new behaviour."""
    rows: list[Row] = []
    if snap.errors:
        rows.append(Row("tasks.toml INVALID", "bad"))
        rows += [Row(f"  {e}", "bad") for e in snap.errors]
        return rows
    buckets, running = _current_buckets(snap, agents)
    done = sum(1 for t in snap.by_id.values() if t["status"] == "done")
    review = sum(1 for t in snap.by_id.values() if t["status"] == "review")
    total = len(snap.by_id) or 1
    counts = {k: len(v) for k, v in buckets.items()}
    pct = round(100 * done / total)
    bar = bar_segments([(done, "good"), (review, "orange"),
                        (counts["ready"], "info"), (counts["in_progress"], "warn"),
                        (counts["blocked"] + counts["held"], "bad")])
    text, parts = seg(*bar, (f"  {pct}% done", "head"))
    rows.append(Row(text, "head", None, parts))
    text, parts = seg((f"{done} done", "good"), ("   ", "dim"),
                      (f"{review} review", "orange"), ("   ", "dim"),
                      (f"{counts['ready']} ready", "info"), ("   ", "dim"),
                      (f"{counts['in_progress']} wip", "warn"), ("   ", "dim"),
                      (f"{counts['blocked'] + counts['held']} stuck", "bad"))
    rows.append(Row(text, "dim", None, parts))

    def section(bucket: str, tasks: list[Any], tone: str, label_text: str) -> None:
        rows.append(Row("", "dim"))
        label = f" {label_text} ({len(tasks)}) "
        rows.append(Row(f"{H}{H}{label}{H * 3}", tone))
        for task in tasks:
            note = _bucket_note(bucket, task, snap, running, pending)
            text, parts = seg((f"{DOT} ", tone), (f"p{task['phase']} ", "dim"),
                              (f"{task['id']:<10} ", "id"),
                              (f"{task['title'][:44]:<44} ", "text"),
                              (note, "dim"))
            rows.append(Row(text, tone, task, parts))

    # IN PROGRESS renders first and always, even at zero -- it's the one
    # section an operator checks to confirm nothing is silently running
    # unattended.
    section("in_progress", buckets["in_progress"], BUCKET_TONE["in_progress"],
            "IN PROGRESS")

    review_tasks = sorted((t for t in snap.by_id.values() if t["status"] == "review"),
                          key=lambda t: (t["phase"], t["id"]))
    if review_tasks:
        rows.append(Row("", "dim"))
        label = f" REVIEW ({len(review_tasks)}) "
        rows.append(Row(f"{H}{H}{label}{H * 3}", "orange"))
        for task in review_tasks:
            queued = task["id"] in pending
            tone = "good" if queued else "orange"
            note = _bucket_note("review", task, snap, running, pending)
            text, parts = seg((f"{DOT} ", tone), (f"p{task['phase']} ", "dim"),
                              (f"{task['id']:<10} ", "id"),
                              (f"{task['title'][:44]:<44} ", "text"),
                              (note, "dim"))
            rows.append(Row(text, tone, task, parts))

    # READY, HUMAN GATED and BLOCKED nest under one TASKS heading -- they're
    # the three buckets a todo task can be in, so they read as one decision
    # queue rather than three independent sections. IN PROGRESS (above) and
    # REVIEW (above) stay top-level: the first thing an operator checks and
    # the human-review queue both read better standing alone. HELD stays
    # top-level too, below -- it's an explicit park, not part of that queue.
    task_buckets = ("ready", "needs_routing", "blocked")
    if any(buckets[bucket] for bucket in task_buckets):
        rows.append(Row("", "dim"))
        rows.append(Row(f"{H}{H} TASKS {H * 3}", "head"))
        for bucket in task_buckets:
            tasks = buckets[bucket]
            if not tasks:
                continue
            if bucket == "ready" and bl is not None:
                for label_text, group in _group_ready_by_backend(bl, tasks, families or {}):
                    section(bucket, group, BUCKET_TONE[bucket], label_text)
                continue
            label_text = BUCKET_LABEL.get(bucket, bucket.replace("_", " ").upper())
            section(bucket, tasks, BUCKET_TONE[bucket], label_text)

    for bucket in BUCKET_ORDER:
        if bucket in ("in_progress", *task_buckets):
            continue
        tasks = buckets[bucket]
        if not tasks:
            continue
        label_text = BUCKET_LABEL.get(bucket, bucket.replace("_", " ").upper())
        section(bucket, tasks, BUCKET_TONE[bucket], label_text)
    return rows


def backlog_rows_by_type(snap: Snapshot, pending: frozenset[str] = frozenset(),
                         agents: Sequence[Agent] = ()) -> list[Row]:
    """The BACKLOG pane's second rendering mode (toggled with 't'): one
    section per task-id prefix instead of per scheduling bucket, so "every
    outstanding CK task and what's blocking it" is one section instead of
    five read and mentally filtered by id. Reads the same Snapshot.by_id
    data backlog_rows() does -- classify()'s buckets stay backlog.py's L0
    contract; this only adds a second view over them, never a new bucket."""
    rows: list[Row] = []
    if snap.errors:
        rows.append(Row("tasks.toml INVALID", "bad"))
        rows += [Row(f"  {e}", "bad") for e in snap.errors]
        return rows
    buckets, running = _current_buckets(snap, agents)
    bucket_of: dict[str, str] = {
        task["id"]: bucket for bucket, tasks in buckets.items() for task in tasks}
    for task in snap.by_id.values():
        if task["status"] == "review":
            bucket_of[task["id"]] = "review"

    by_type: dict[str, list[Any]] = {}
    for task in snap.by_id.values():
        if task["status"] == "done":
            continue
        by_type.setdefault(task["id"].split("-", 1)[0], []).append(task)

    for prefix in sorted(by_type):
        tasks = sorted(by_type[prefix], key=lambda t: (t["phase"], t["id"]))
        rows.append(Row("", "dim"))
        label = f" {prefix} ({len(tasks)}) "
        rows.append(Row(f"{H}{H}{label}{H * 3}", "head"))
        goal = TASK_TYPE_GOALS.get(prefix)
        if goal:
            rows.append(Row(f"  {goal}", "dim"))
        for task in tasks:
            bucket = bucket_of[task["id"]]
            if bucket == "review":
                tone = "good" if task["id"] in pending else "orange"
            else:
                tone = BUCKET_TONE[bucket]
            label_text = BUCKET_LABEL.get(bucket, bucket.replace("_", " ").upper())
            note = _bucket_note(bucket, task, snap, running, pending)
            text, parts = seg((f"{DOT} ", tone), (f"p{task['phase']} ", "dim"),
                              (f"{task['id']:<10} ", "id"),
                              (f"{task['title'][:36]:<36} ", "text"),
                              (f"{label_text:<12} ", "dim"),
                              (note, "dim"))
            rows.append(Row(text, tone, task, parts))
    return rows


def doctor_rows(snap: Snapshot) -> list[Row]:
    if not snap.findings:
        return [Row(f"{DOT} No findings. Repo looks sound.", "good")]
    rows: list[Row] = []
    counts: dict[str, int] = {}
    for finding in snap.findings:
        counts[finding.code] = counts.get(finding.code, 0) + 1
    for finding in snap.findings:
        mark = "!" if finding.level == "warn" else "i"
        tone = "warn" if finding.level == "warn" else "info"
        batch = ""
        if finding.code in BATCHABLE and counts[finding.code] > 1:
            batch = f"  (x{counts[finding.code]})"
        text, parts = seg((f"{mark} ", tone), (f"{finding.code:<20} ", "id"),
                          (finding.message, "text"), (batch, "dim"))
        rows.append(Row(text, tone, finding, parts))
        rows.append(Row(f"    {finding.hint}", "dim"))
    return rows


def fleet_rows(snap: Snapshot, agents: list[Agent],
               daily: tuple[int, float] | None) -> list[Row]:
    rows: list[Row] = []
    if snap.lanes:
        rows.append(Row(f"{H}{H} LANE PLAN ({len(snap.lanes)}) {H * 3}", "good"))
        for lane in snap.lanes:
            text, parts = seg((f"  {DOT} ", "good"), (f"lane {lane.index}  ", "id"),
                              (" ".join(lane.tasks), "text"))
            rows.append(Row(text, "good", lane, parts))
        for item in snap.deferred:
            why = ("collides with" if item.reason == "collision"
                   else "queued behind")
            rows.append(Row(f"    {item.task} {why} lane {item.lane}", "dim"))
        rows.append(Row("", "dim"))

    rows.append(Row(f"{H}{H} RUNNING AGENTS ({len(agents)}) {H * 3}", "busy"))
    if not agents:
        rows.append(Row("  none under this repo", "dim"))
    total_cost, total_tokens = 0.0, 0
    for agent in agents:
        if agent.usage:
            use = agent.usage
            pct = round(100 * use.context / use.window)
            detail_tone = pct_tone(pct)
            detail = f"{human_tokens(use.context)} ctx {pct:>3}%  ${use.cost:.2f}"
            total_cost += use.cost
            total_tokens += use.context
        else:
            detail, detail_tone = "no transcript", "dim"
        flag = f"{agent.status}/{agent.state}" if agent.state else agent.status
        flag_tone = "busy" if agent.status == "busy" else (
            "warn" if agent.state else "dim")
        text, parts = seg((f"  {DOT} ", flag_tone), (f"{agent.name[:26]:<26} ", "id"),
                          (f"{agent.kind[:6]:<6} ", "dim"),
                          (f"{flag[:12]:<12} ", flag_tone),
                          (f"{human_age(agent.age):>6}  ", "dim"),
                          (detail, detail_tone))
        rows.append(Row(text, flag_tone, agent, parts))
    if agents:
        text, parts = seg((f"  {'fleet total':<26} {'':<6} {'':<12} {'':>6}  ", "dim"),
                          (f"{human_tokens(total_tokens)} ctx       "
                           f"${total_cost:.2f}", "head"))
        rows.append(Row(text, "head", None, parts))
    if daily:
        tokens, cost = daily
        rows.append(Row("", "dim"))
        text, parts = seg(("Today (all projects): ", "dim"),
                          (f"{human_tokens(tokens)} tokens, ${cost:.2f}", "info"),
                          (" recorded", "dim"))
        rows.append(Row(text, "dim", None, parts))
    rows.append(Row("Subscription 5h/weekly limits are not exposed on disk; "
                    "run /usage in a session.", "dim"))
    return rows


def task_lattice_grid_rows(snap: Snapshot, pending: frozenset[str] = frozenset(),
                           agents: Sequence[Agent] = ()) -> list[Row]:
    """Render the 6-column Task Lattice grid (Backlog, In Progress, Review, Ready,
    Human Gated, Blocked) in high-density format."""
    rows: list[Row] = []
    if snap.errors:
        rows.append(Row("tasks.toml INVALID", "bad"))
        rows += [Row(f"  {e}", "bad") for e in snap.errors]
        return rows

    buckets = {bucket: list(tasks) for bucket, tasks in snap.buckets.items()}
    running: dict[str, Agent] = {}
    for bucket, tasks in list(buckets.items()):
        if bucket == "in_progress":
            continue
        kept = []
        for task in tasks:
            agent = agent_for_task(task["id"], agents)
            if agent:
                running[task["id"]] = agent
                buckets.setdefault("in_progress", []).append(task)
            else:
                kept.append(task)
        buckets[bucket] = kept

    def _sort_key(t: dict[str, Any]) -> tuple[int, str]:
        return (t.get("phase", 0), t.get("id", ""))

    in_prog = sorted(buckets.get("in_progress", []), key=_sort_key)
    review = sorted(
        [t for t in snap.by_id.values() if t.get("status") == "review"], key=_sort_key)
    ready = sorted(buckets.get("ready", []), key=_sort_key)
    gated = sorted(buckets.get("needs_routing", []), key=_sort_key)
    blocked = sorted(buckets.get("blocked", []), key=_sort_key)

    categorized_ids = {t["id"] for t in in_prog + review + ready + gated + blocked}
    backlog = sorted(
        [t for t in snap.by_id.values() if t["id"] not in categorized_ids], key=_sort_key)

    columns_data = [
        ("Backlog", backlog, "dim"),
        ("In Progress", in_prog, BUCKET_TONE["in_progress"]),
        ("Review", review, "orange"),
        ("Ready", ready, BUCKET_TONE["ready"]),
        ("Human Gated", gated, BUCKET_TONE["needs_routing"]),
        ("Blocked", blocked, BUCKET_TONE["blocked"]),
    ]

    header_title = f"╭─ TASK LATTICE GRID (6 Columns) {H * 40}╮"
    header_parts = (
        ("╭─ ", "dim"), ("TASK LATTICE GRID (6 Columns)", "head"), (f" {H * 40}╮", "dim"))
    rows.append(Row(header_title, "head", None, header_parts))

    col_headers = " │ ".join(f"{label} ({len(ts)})" for label, ts, _ in columns_data)
    grid_header = f"│ {col_headers:<76} │"

    parts_hdr: list[tuple[str, str]] = [("│ ", "dim")]
    for idx, (label, ts, tone) in enumerate(columns_data):
        if idx > 0:
            parts_hdr.append((" │ ", "dim"))
        parts_hdr.append((f"{label} ({len(ts)})", tone))
    parts_hdr.append((" │", "dim"))
    rows.append(Row(grid_header, "head", None, tuple(parts_hdr)))

    sub_bar = f"├{H * 78}┤"
    rows.append(Row(sub_bar, "dim", None, ((sub_bar, "dim"),)))

    max_depth = max(len(ts) for _, ts, _ in columns_data)
    if max_depth == 0:
        e = "(empty)"
        empty_grid = f"│ {e:<11} │ {e:<13} │ {e:<8} │ {e:<7} │ {e:<13} │ {e:<9} │"
        rows.append(Row(empty_grid, "dim"))
    else:
        for r in range(max_depth):
            cell_strs = []
            cell_parts: list[tuple[str, str]] = [("│ ", "dim")]
            row_payload = None
            for idx, (label, ts, tone) in enumerate(columns_data):
                if idx > 0:
                    cell_parts.append((" │ ", "dim"))
                if r < len(ts):
                    t = ts[r]
                    if row_payload is None:
                        row_payload = t
                    cell_text = t["id"]
                    t_tone = tone
                    if label == "Review" and t["id"] in pending:
                        t_tone = "good"
                    cell_parts.append((f"{cell_text:<10}", t_tone))
                    cell_strs.append(f"{cell_text:<10}")
                else:
                    cell_parts.append((f"{'·':<10}", "dim"))
                    cell_strs.append(f"{'·':<10}")
            cell_parts.append((" │", "dim"))
            grid_line = "│ " + " │ ".join(cell_strs) + " │"
            rows.append(Row(grid_line, "head", row_payload, tuple(cell_parts)))

    grid_foot = f"╰{H * 78}╯"
    rows.append(Row(grid_foot, "dim", None, ((grid_foot, "dim"),)))

    for label, ts, tone in columns_data:
        if not ts:
            continue
        rows.append(Row("", "dim"))
        sec_head = f"{H}{H} {label.upper()} ({len(ts)}) {H * 3}"
        rows.append(Row(sec_head, tone))
        for task in ts:
            note = ""
            if label == "Blocked":
                waiting = sorted(
                    d for d in task.get("depends", [])
                    if d in snap.by_id and snap.by_id[d]["status"] not in ("done", "review"))
                note = "waits on " + ", ".join(waiting)
            elif label == "Human Gated":
                note = f"{task.get('routing', '')} - human gated"
            elif label == "In Progress":
                agent = running.get(task["id"])
                if agent:
                    flag = f"{agent.status}/{agent.state}" if agent.state else agent.status
                    note = f"agent {flag} -- {human_age(agent.age)}"
                else:
                    note = task.get("routing", "")
            elif label == "Review":
                queued = task["id"] in pending
                tone = "good" if queued else "orange"
                note = f"queued -- pending {LANDING_BRANCH}" if queued else "awaiting human review"

            text, parts = seg((f"{DOT} ", tone), (f"p{task.get('phase', 1)} ", "dim"),
                              (f"{task['id']:<10} ", "id"),
                              (f"{task.get('title', '')[:44]:<44} ", "text"),
                              (note, "dim"))
            rows.append(Row(text, tone, task, parts))

    return rows


def stat_callout_boxes(spend: float, tokens: int, exec_seconds: float) -> list[Row]:
    """Render big stat callout boxes ($ Spend, Tokens Total, Exec Time)."""
    spend_str = f"${spend:.2f}"
    tokens_str = human_tokens(tokens)
    exec_str = human_age(exec_seconds)

    b1_head = f"╭─ $ SPEND {H * 6}╮"
    b1_body = f"│ {spend_str:<14} │"
    b1_foot = f"╰{H * 16}╯"

    b2_head = f"╭─ TOKENS TOTAL {H * 1}╮"
    b2_body = f"│ {tokens_str:<14} │"
    b2_foot = f"╰{H * 16}╯"

    b3_head = f"╭─ EXEC TIME {H * 4}╮"
    b3_body = f"│ {exec_str:<14} │"
    b3_foot = f"╰{H * 16}╯"

    line1 = f"{b1_head}  {b2_head}  {b3_head}"
    line2 = f"{b1_body}  {b2_body}  {b3_body}"
    line3 = f"{b1_foot}  {b2_foot}  {b3_foot}"

    parts1 = (
        ("╭─ ", "dim"), ("$ SPEND", "head"), (f" {H * 6}╮  ", "dim"),
        ("╭─ ", "dim"), ("TOKENS TOTAL", "head"), (f" {H * 1}╮  ", "dim"),
        ("╭─ ", "dim"), ("EXEC TIME", "head"), (f" {H * 4}╮", "dim"),
    )
    parts2 = (
        ("│ ", "dim"), (f"{spend_str:<14}", "good"), (" │  ", "dim"),
        ("│ ", "dim"), (f"{tokens_str:<14}", "info"), (" │  ", "dim"),
        ("│ ", "dim"), (f"{exec_str:<14}", "warn"), (" │", "dim"),
    )
    parts3 = (
        (f"╰{H * 16}╯  ", "dim"),
        (f"╰{H * 16}╯  ", "dim"),
        (f"╰{H * 16}╯", "dim"),
    )

    return [
        Row(line1, "dim", None, parts1),
        Row(line2, "head", None, parts2),
        Row(line3, "dim", None, parts3),
    ]


def tok_min_sparkline_box(tok_min_history: Sequence[int | float] = ()) -> list[Row]:
    """Render peak/now TOK/MIN sparkline box enclosure."""
    vals = list(tok_min_history) or [120, 340, 560, 890, 1450, 1100, 2300, 1800, 2100, 1950]
    peak = max(vals) if vals else 0
    now = vals[-1] if vals else 0
    spark = render_sparkline(vals, width=20)

    peak_str = f"Peak: {human_tokens(int(peak))}/m"
    now_str = f"Now: {human_tokens(int(now))}/m"

    line1 = f"╭─ TOK/MIN SPARKLINE {H * 37}╮"
    line2 = f"│ {peak_str:<15} {now_str:<14} {spark} │"
    line3 = f"╰{H * 57}╯"

    parts1 = (("╭─ ", "dim"), ("TOK/MIN SPARKLINE", "head"), (f" {H * 37}╮", "dim"))
    parts2 = (
        ("│ ", "dim"),
        (f"{peak_str:<15}", "warn"),
        (" ", "dim"),
        (f"{now_str:<14}", "good"),
        (" ", "dim"),
        (spark, "info"),
        (" │", "dim"),
    )
    parts3 = ((f"╰{H * 57}╯", "dim"),)

    return [
        Row(line1, "dim", None, parts1),
        Row(line2, "head", None, parts2),
        Row(line3, "dim", None, parts3),
    ]


def fleet_monitor_table(agents: Sequence[Agent], snap: Snapshot | None = None) -> list[Row]:
    """Render Fleet Monitor table with worker CPU/context meters."""
    rows: list[Row] = []
    head_text = f"╭─ FLEET MONITOR ({len(agents)} workers) {H * 35}╮"
    parts_head = (
        ("╭─ ", "dim"), (f"FLEET MONITOR ({len(agents)} workers)", "head"), (f" {H * 35}╮", "dim"))
    rows.append(Row(head_text, "head", None, parts_head))

    col_header = (
        f"│ {'WORKER':<14} {'TASK':<10} {'STATUS':<10} {'CPU METER':<16} "
        f"{'CONTEXT METER':<16} │")
    parts_col = (
        ("│ ", "dim"),
        (f"{'WORKER':<14}", "head"), (" ", "dim"),
        (f"{'TASK':<10}", "head"), (" ", "dim"),
        (f"{'STATUS':<10}", "head"), (" ", "dim"),
        (f"{'CPU METER':<16}", "head"), (" ", "dim"),
        (f"{'CONTEXT METER':<16}", "head"),
        (" │", "dim"),
    )
    rows.append(Row(col_header, "head", None, parts_col))

    if not agents:
        empty_line = f"│ {'(no active fleet workers)':<68} │"
        parts_empty = (("│ ", "dim"), (f"{'(no active fleet workers)':<68}", "dim"), (" │", "dim"))
        rows.append(Row(empty_line, "dim", None, parts_empty))
    else:
        for agent in agents:
            w_name = agent.name[:14]
            matched_task_id = "N/A"
            if snap and snap.by_id:
                for tid in snap.by_id:
                    if agent_for_task(tid, [agent]):
                        matched_task_id = tid
                        break
            if matched_task_id == "N/A" and agent.cwd:
                matched_task_id = Path(agent.cwd).name.split("-")[0]

            stat_txt, stat_parts = status_badge(agent.status)
            cpu_pct = getattr(agent, "cpu_pct", 80 if agent.status == "busy" else 5)
            cpu_bar_txt, cpu_bar_parts = progress_bar(cpu_pct, width=8)

            if agent.usage and agent.usage.window:
                ctx_pct = round(100 * agent.usage.context / agent.usage.window)
            else:
                ctx_pct = getattr(agent, "context_pct", 0)
            ctx_bar_txt, ctx_bar_parts = progress_bar(ctx_pct, width=8)

            line = (
                f"│ {w_name:<14} {matched_task_id:<10} {stat_txt:<10} {cpu_bar_txt:<16} "
                f"{ctx_bar_txt:<16} │")
            flag_tone = (
                "busy" if agent.status == "busy"
                else ("good" if agent.status == "idle" else "dim"))
            parts_worker = (
                ("│ ", "dim"),
                (f"{w_name:<14} ", "id"),
                (f"{matched_task_id:<10} ", "text"),
                *stat_parts,
                (" " * max(0, 10 - len(stat_txt)) + " ", "dim"),
                *cpu_bar_parts,
                (" " * max(0, 16 - len(cpu_bar_txt)) + " ", "dim"),
                *ctx_bar_parts,
                (" │", "dim"),
            )
            rows.append(Row(line, flag_tone, agent, parts_worker))

    foot_line = f"╰{H * 70}╯"
    rows.append(Row(foot_line, "dim", None, ((foot_line, "dim"),)))
    return rows


def fleet_monitor_and_usage_boxes(
    snap: Snapshot,
    agents: Sequence[Agent] = (),
    daily: tuple[int, float] | None = None,
    usage_mod: ModuleType | None = None,
    tok_min_history: Sequence[int | float] = ()
) -> list[Row]:
    """Render high-density dashboard panes: big stat callout boxes ($ Spend,
    Tokens Total, Exec Time), peak/now TOK/MIN sparklines, and Fleet Monitor
    table with worker CPU/context meters."""
    rows: list[Row] = []

    total_spend = sum((a.usage.cost for a in agents if a.usage), 0.0)
    total_tokens = sum((a.usage.context for a in agents if a.usage), 0)
    max_exec_time = max((a.age for a in agents), default=0.0)

    if daily:
        tokens, cost = daily
        total_spend = max(total_spend, cost)
        total_tokens = max(total_tokens, tokens)

    rows.extend(stat_callout_boxes(total_spend, total_tokens, max_exec_time))
    rows.append(Row("", "dim"))
    rows.extend(tok_min_sparkline_box(tok_min_history))
    rows.append(Row("", "dim"))
    rows.extend(fleet_monitor_table(agents, snap))

    return rows


def next_rows(snap: Snapshot) -> list[Row]:
    action = snap.action
    rows = [Row(f"{DOT} {action.kind.upper()}", "head"),
            Row(action.reason, "info"), Row("", "dim")]
    if action.skill:
        text, parts = seg((f"{ARROW} skill: ", "dim"), (action.skill, "good"))
        rows.append(Row(text, "good", None, parts))
    if action.command:
        branch = next((a for a in action.command if a.startswith("Implement")), "")
        for line in branch.splitlines():
            if line.strip().startswith("- Work on branch"):
                text, parts = seg((f"{ARROW} ", "dim"),
                                  (line.split("branch ")[1].split(";")[0], "id"))
                rows.append(Row(text, "id", None, parts))
        rows.append(Row("", "dim"))
        rows.append(Row("Command:", "head"))
        wrapped = shlex.join(action.command[:-1])
        rows.append(Row("  " + wrapped, "dim", action))
        rows.append(Row("", "dim"))
        rows.append(Row("Prompt:", "head"))
        for line in action.command[-1].splitlines():
            rows.append(Row("  " + line, "dim", action))
    return rows


# =====================================================================
# WORKFLOW pane -- this repo's own dev loop (task selection through a
# merged PR) as an ordered pipeline, and the roster of subagents a
# dispatched session can delegate to. Both sections are static and
# read-only: no write path, and every fact is either read live
# (.claude/settings.json's hook commands, .claude/agents/*.md frontmatter)
# or cites where it is true today (a file path, an ADR number, a CLAUDE.md
# section) rather than a paraphrase that can drift.
# =====================================================================

def _wrap_command(command: str, width: int = 84) -> list[str]:
    return textwrap.wrap(command, width=width, break_long_words=False,
                         break_on_hyphens=False) or [command]


def hook_commands(repo: Path) -> dict[str, str]:
    """The literal `command` string of the commit- and push-time PreToolUse
    hooks in .claude/settings.json, keyed "commit"/"push" -- read at render
    time so the pipeline's hooks step can quote what the hook actually runs
    instead of a copy that can drift. {} if the file is absent or
    unparseable; this pane must never crash over it."""
    try:
        data = json.loads((repo / ".claude" / "settings.json")
                          .read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    commands: dict[str, str] = {}
    for entry in data.get("hooks", {}).get("PreToolUse", []):
        for hook in entry.get("hooks", []):
            condition, command = hook.get("if", ""), hook.get("command", "")
            if not command:
                continue
            if "git commit" in condition:
                commands["commit"] = command
            elif "git push" in condition:
                commands["push"] = command
    return commands


class WorkflowStep(NamedTuple):
    name: str
    summary: str
    detail: tuple[str, ...]  # always ends with a "Source:" citation line


def workflow_pipeline_steps(repo: Path) -> list[WorkflowStep]:
    """This repo's own dev loop, select-task to merged-PR, as the ordered
    sequence CLAUDE.md, docs/adr/0012, docs/adr/0015 and
    .claude/settings.json otherwise only document by hand-assembly."""
    commands = hook_commands(repo)
    hook_detail = ["PreToolUse hooks in .claude/settings.json gate the",
                   "dispatched session's own git commands before they run:", ""]
    if "commit" in commands:
        hook_detail += ["on git commit (ruff):",
                        *[f"  {ln}" for ln in _wrap_command(commands["commit"])], ""]
    if "push" in commands:
        hook_detail += ["on git push (validate_tasks.py + layering + "
                        "mypy --strict):",
                        *[f"  {ln}" for ln in _wrap_command(commands["push"])], ""]
    if not commands:
        hook_detail.append("(.claude/settings.json not found or its hooks "
                           "not parseable)")
    hook_detail.append("Source: .claude/settings.json (PreToolUse hooks).")

    return [
        WorkflowStep(
            "1. Select task",
            "scripts/backlog.py --next picks the ready task",
            ("scripts/backlog.py --next resolves the next schedulable task "
             "from tasks.toml (ready, dependencies satisfied, routing=impl) "
             "and prints its dispatch command.", "",
             "Source: scripts/backlog.py (--next mode); mirrored live in "
             "this cockpit's own NEXT pane (next_rows()).")),
        WorkflowStep(
            "2. Plan (optional)",
            "the built-in Plan agent, run by the operator before dispatch",
            ("An optional step: the operator may run Claude Code's "
             "built-in Plan agent against the task before dispatching it, "
             "to work out an approach first.", "",
             "Not house-defined -- there is no .claude/agents/plan.md. "
             "ADR 0012 draws the built-in-vs-house-defined line for the "
             "implement step's Explore; Plan follows the same split "
             "without being named there itself.", "",
             "Source: docs/adr/0012-subagent-delegation-in-emitted-"
             "prompts.md (built-in vs. house-defined agents).")),
        WorkflowStep(
            "3. Implement",
            "dispatched `claude -p` (bare mode); may delegate to Explore "
            "or the six house agents",
            ("backlog.py's build_command() dispatches `claude -p --model "
             "... --permission-mode acceptEdits ...` -- Claude Code's "
             "non-interactive bare mode, one shot per task.", "",
             "Per ADR 0012, the emitted prompt's Delegation block may hand "
             "bulky, low-answer-density work to the built-in Explore agent "
             "or to the six house agents (gate-runner, criteria-auditor, "
             "failure-analyst, test-author, house-reviewer, adr-scribe); "
             "the implementation itself always stays in the dispatched "
             "session.", "",
             "Source: scripts/backlog.py (build_command(), delegation()); "
             "docs/adr/0012-subagent-delegation-in-emitted-prompts.md.")),
        WorkflowStep(
            "4. Claude Code hooks",
            "ruff on commit; validate_tasks.py + layering + mypy --strict "
            "on push",
            tuple(hook_detail)),
        WorkflowStep(
            "5. Land",
            "ADR 0015's triggered script commits, pushes, opens the PR -- "
            "not yet built (DX-11, status: todo)",
            ("ADR 0015 decided the dev loop lands through a script the "
             "agent triggers, not a git sequence it composes by hand: the "
             "agent supplies only a commit subject and a review-request "
             "body, everything else is computed or asserted by the "
             "script.", "",
             "That script (scripts/land.py) is owned by task DX-11, still "
             "status=todo in tasks.toml. Until it exists, CLAUDE.md's "
             "Working rules govern directly: the dispatched session runs "
             "the commit, push and `gh pr create` itself.", "",
             "Source: docs/adr/0015-dev-loop-lands-through-a-triggered-"
             "script.md; CLAUDE.md \"Working rules\"; tasks.toml (DX-11).")),
        WorkflowStep(
            "6. CI/CD",
            "CI is the sole arbiter of completion",
            ("\"CI is the sole arbiter of completion. A task is done when "
             "its named exit-criteria tests are green in CI -- never by "
             "inspection.\" -- CLAUDE.md, Precedence & arbiter.", "",
             "Pipeline config: .gitlab-ci.yml.", "",
             "Source: CLAUDE.md \"Precedence & arbiter\"; .gitlab-ci.yml.")),
        WorkflowStep(
            "7. Human review / merge",
            "PR opened ready for review (never draft); squash-merged",
            ("\"Open PRs ready for review, never as drafts.\" \"One task "
             "per branch, squash-merge, main always green.\" -- CLAUDE.md, "
             "Working rules.", "",
             "Source: CLAUDE.md \"Working rules\".")),
    ]


def workflow_step_detail_lines(step: WorkflowStep,
                               repo: Path | None = None) -> list[str]:
    return [step.name, "", *step.detail]


class AgentDef(NamedTuple):
    name: str
    description: str
    model: str    # "" for a built-in, or a file that does not declare one
    tools: str    # "" for a built-in, or a file that does not declare one
    effort: str   # "" whenever the frontmatter has no effort: field
    source: str   # relative file path, or "built-in (no .claude/agents file)"


#: Claude Code built-ins this dev loop names (ADR 0012) with no local
#: .claude/agents file to parse -- listed plainly, never given a fabricated
#: model/tools value neither one declares.
BUILTIN_WORKFLOW_AGENTS: tuple[AgentDef, ...] = (
    AgentDef("Explore",
            "Fast read-only search agent for locating code by pattern, "
            "symbol, or keyword.",
            "", "", "", "built-in (no .claude/agents file)"),
    AgentDef("Plan",
            "Software architect agent for designing an implementation "
            "plan before dispatch.",
            "", "", "", "built-in (no .claude/agents file)"),
)


def parse_agent_frontmatter(path: Path) -> dict[str, str]:
    """key: value pairs from the leading ---fenced block of a subagent
    definition -- the same line-based read as scripts/backlog.py's own
    _agent_name(), extended to every field instead of just `name`. Never
    raises; a file it cannot make sense of yields an empty dict."""
    fields: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return fields
    if not lines or lines[0].strip() != "---":
        return fields
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if sep and key.strip():
            fields[key.strip()] = value.strip()
    return fields


def agent_roster(repo: Path) -> list[AgentDef]:
    """Every callable subagent: the Claude Code built-ins ADR 0012 names
    plus whatever .claude/agents/*.md actually defines, parsed at render
    time so an added, removed or retuned house agent shows up here without
    touching this pane. A missing .claude/agents/ degrades to just the
    built-ins -- the sibling-module fallback in UM-04's usage_rows() is the
    precedent for "missing input degrades gracefully", not a crash."""
    roster = list(BUILTIN_WORKFLOW_AGENTS)
    agents_dir = repo / ".claude" / "agents"
    if agents_dir.is_dir():
        for path in sorted(agents_dir.glob("*.md")):
            fields = parse_agent_frontmatter(path)
            roster.append(AgentDef(
                fields.get("name", path.stem),
                fields.get("description", ""),
                fields.get("model", ""),
                fields.get("tools", ""),
                fields.get("effort", ""),
                str(path.relative_to(repo))))
    return roster


def agent_roster_detail_lines(agent: AgentDef,
                              repo: Path | None = None) -> list[str]:
    return [agent.name, "", agent.description or "(no description)", "",
            f"model  {agent.model or '(not declared)'}",
            f"tools  {agent.tools or '(not declared)'}",
            f"effort {agent.effort or '(not declared)'}", "",
            f"Source: {agent.source}"]


def _workflow_signature(repo: Path) -> tuple[float, tuple[tuple[str, float], ...]]:
    """(.claude/settings.json mtime, sorted (filename, mtime) pairs for
    .claude/agents/*.md) -- the on-disk state workflow_rows() actually
    depends on. Cheap to stat every poll tick, unlike re-reading and
    re-parsing every file's contents just to throw the result away a
    second later when nothing changed."""
    settings_path = repo / ".claude" / "settings.json"
    try:
        settings_mtime = settings_path.stat().st_mtime
    except OSError:
        settings_mtime = 0.0
    agent_mtimes: list[tuple[str, float]] = []
    agents_dir = repo / ".claude" / "agents"
    if agents_dir.is_dir():
        for path in sorted(agents_dir.glob("*.md")):
            try:
                agent_mtimes.append((path.name, path.stat().st_mtime))
            except OSError:
                continue
    return settings_mtime, tuple(agent_mtimes)


def workflow_rows(repo: Path, bypass_cache: bool = False) -> list[Row]:
    """The WORKFLOW pane: the dev-loop pipeline (section 1) then the
    callable-agent roster (section 2). See workflow_pipeline_steps() /
    agent_roster() for what backs each row.

    Cached by _workflow_signature() the same way snapshot() caches by
    tasks.toml's mtime: this pane's underlying facts (hook commands, house
    agent frontmatter) only change when someone edits those files, so
    redoing the read-and-parse every ~1s poll tick while the pane is open
    is pure waste."""
    repo_key = str(repo.resolve())
    signature = _workflow_signature(repo)
    if not bypass_cache and repo_key in _WORKFLOW_CACHE:
        cached_signature, cached_rows = _WORKFLOW_CACHE[repo_key]
        if cached_signature == signature:
            return cached_rows

    rows: list[Row] = [Row(f"{H}{H} DEV-LOOP PIPELINE {H * 3}", "good")]
    for step in workflow_pipeline_steps(repo):
        text, parts = seg((f"  {step.name:<24} ", "id"), (step.summary, "text"))
        rows.append(Row(text, "text", step, parts))

    roster = agent_roster(repo)
    rows.append(Row("", "dim"))
    rows.append(Row(f"{H}{H} CALLABLE AGENTS ({len(roster)}) {H * 3}", "busy"))
    for agent in roster:
        text, parts = seg(
            (f"  {agent.name[:18]:<18} ", "id"),
            (f"{(agent.model or '-'):<8} ", "dim"),
            (f"{(agent.effort or '-'):<8} ", "dim"),
            ((agent.tools or agent.source)[:40], "text"))
        rows.append(Row(text, "text", agent, parts))

    _WORKFLOW_CACHE[repo_key] = (signature, rows)
    return rows


#: How many trailing days the USAGE pane's token sparkline covers.
USAGE_SPARKLINE_DAYS = 14
#: history.jsonl scope the USAGE pane reads. Nothing in this task writes a
#: snapshot with this key -- a future task owns capture -- so an empty
#: history is the common case and must render as a plain "no history yet".
USAGE_HISTORY_SCOPE = "repo"
#: Block glyphs, low to high, for the token sparkline.
SPARKLINE_GLYPHS = " ▁▂▃▄▅▆▇█"
#: Tones cycled across a model split, most-tokens first.
MODEL_SPLIT_TONES = ("busy", "info", "good", "warn", "orange", "bad")


class _UsageData(NamedTuple):
    """Everything the USAGE pane shows, computed once and shared by
    usage_rows() (curses) and usage_summary_lines() (plain text) so the two
    never drift apart."""
    session_bd: Any
    today_bd: Any
    week_bd: Any
    model_tokens: dict[str, int]
    history: list[Any]
    suggestions: list[Any]
    all_turns: list[Any]


def _collect_usage_data(usage_mod: ModuleType, repo: Path) -> _UsageData:
    key = (str(CLAUDE_HOME), str(repo.resolve()))
    cached = _USAGE_CACHE.get(key)
    if cached is not None:
        return cached

    turns = usage_mod.collect_turns(CLAUDE_HOME)
    sessions = usage_mod.fold_sessions(turns)
    latest = max(sessions.values(), key=lambda s: s.ended, default=None)
    session_turns = ([t for t in turns if t.session_id == latest.session_id]
                     if latest else [])

    today = time.strftime("%Y-%m-%d")
    week_start = time.strftime("%Y-%m-%d", time.localtime(time.time() - 6 * 86400))
    today_turns = [t for t in turns if t.timestamp[:10] == today]
    week_turns = [t for t in turns if t.timestamp[:10] >= week_start]

    model_tokens: dict[str, int] = {}
    for t in week_turns:
        model_tokens[t.model] = model_tokens.get(t.model, 0) + (
            t.input_tokens + t.output_tokens + t.cache_read_tokens
            + t.cache_creation_tokens)

    week_bd = usage_mod.compute_breakdown(week_turns)
    history = [s for s in usage_mod.load_history(repo)
              if s.scope == USAGE_HISTORY_SCOPE][-USAGE_SPARKLINE_DAYS:]

    data = _UsageData(
        session_bd=usage_mod.compute_breakdown(session_turns),
        today_bd=usage_mod.compute_breakdown(today_turns),
        week_bd=week_bd,
        model_tokens=model_tokens,
        history=history,
        suggestions=usage_mod.generate_usage_suggestions(week_bd),
        all_turns=turns)
    _USAGE_CACHE.set(key, data)
    return data


#: Trailing days probe_landed_commits and the ANALYTICS pane's git-derived
#: series (task completions, code churn) cover.
ANALYTICS_WINDOW_DAYS = 30
#: How many trailing days the commit-calendar heatmap renders as columns.
ANALYTICS_CALENDAR_DAYS = 28
#: Glyph levels for the commit-calendar heatmap, low to high -- a shorter
#: ramp than SPARKLINE_GLYPHS since a calendar cell is one character, not a
#: multi-level bar.
_CALENDAR_GLYPHS = " ░▒▓█"


def _trailing_dates(n: int) -> list[str]:
    """The last `n` calendar dates (YYYY-MM-DD), oldest first, ending
    today -- local time, the same clock USAGE's today/week windows use."""
    now = time.time()
    return [time.strftime("%Y-%m-%d", time.localtime(now - i * 86400))
           for i in range(n - 1, -1, -1)]


def _calendar_glyph(count: int, peak: int) -> str:
    if count <= 0 or peak <= 0:
        return _CALENDAR_GLYPHS[0]
    idx = min(len(_CALENDAR_GLYPHS) - 1,
             max(1, round(count / peak * (len(_CALENDAR_GLYPHS) - 1))))
    return _CALENDAR_GLYPHS[idx]


def commit_calendar_rows(landed: dict[str, int], days: Sequence[str]) -> list[Row]:
    """A GitHub-style commit-calendar heatmap: one column per week, one row
    per weekday (Mon..Sun), each cell a glyph scaled by that day's landed
    task count. `days` must be consecutive calendar dates, oldest first."""
    if not days:
        return [Row("  no data", "dim")]
    peak = max((landed.get(d, 0) for d in days), default=0)
    first_weekday = time.strptime(days[0], "%Y-%m-%d").tm_wday
    padded: list[str | None] = [None] * first_weekday + list(days)
    weeks = [padded[i:i + 7] for i in range(0, len(padded), 7)]

    rows: list[Row] = []
    for wd, label in enumerate(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")):
        cells = " ".join(
            _calendar_glyph(landed.get(week[wd], 0), peak)
            if wd < len(week) and week[wd] is not None else " "
            for week in weeks)
        text, parts = seg((f"  {label} ", "dim"), (cells, "good"))
        rows.append(Row(text, "text", None, parts))
    rows.append(Row(f"  peak {peak}/day over {len(days)}d", "dim"))
    return rows


def model_day_matrix_rows(usage_mod: ModuleType, repo: Path,
                          days: Sequence[str]) -> list[Row]:
    """Per-model token totals for the trailing `days` (oldest first) as a
    small table: one column per day, one row per model, ranked by total
    tokens over the window and capped at the top 6. Live from transcripts --
    see fold_days_by_model's docstring for why this isn't drawn from the
    persisted Snapshot history."""
    data = _collect_usage_data(usage_mod, repo)
    matrix = usage_mod.fold_days_by_model(data.all_turns)
    totals: dict[str, int] = {}
    for (date, model), tokens in matrix.items():
        if date in days:
            totals[model] = totals.get(model, 0) + tokens
    models = sorted(totals, key=lambda m: -totals[m])[:6]
    if not models:
        return [Row("  no data in this window", "dim")]

    header = "  ".join(d[5:] for d in days)
    rows = [Row(f"  {'model':<22} {header}", "dim")]
    for i, model in enumerate(models):
        tone = MODEL_SPLIT_TONES[i % len(MODEL_SPLIT_TONES)]
        cells = "  ".join(
            f"{human_tokens(matrix.get((d, model), 0)):>5}" for d in days)
        text, parts = seg((f"  {model:<22} ", "id"), (cells, tone))
        rows.append(Row(text, "text", None, parts))
    return rows


def analytics_rows(usage_mod: ModuleType | None, repo: Path) -> list[Row]:
    """The ANALYTICS pane: token usage, task completions and code churn as
    day-granular series, a GitHub-style commit-calendar heatmap, and a
    model x day matrix. Built entirely from usage.py's transcript scan
    (token trend, model matrix) and a `main` git-log walk (completions,
    churn) -- no new persisted store (see probe_landed_commits' docstring
    for why). No row carries a payload -- like USAGE, this pane is a
    dashboard, nothing on it is selectable."""
    rows: list[Row] = []
    commits = probe_landed_commits(repo, days=ANALYTICS_WINDOW_DAYS)
    landed = landed_by_day(commits)
    churn = churn_by_day(commits)
    window_days = _trailing_dates(ANALYTICS_WINDOW_DAYS)
    calendar_days = window_days[-ANALYTICS_CALENDAR_DAYS:]

    rows.append(Row(f"{H}{H} TOKEN USAGE ({USAGE_SPARKLINE_DAYS}d) {H * 3}", "head"))
    if usage_mod is None:
        rows.append(Row("  usage.py not found; token trend unavailable.", "dim"))
    else:
        history = _collect_usage_data(usage_mod, repo).history
        if history:
            peak = max(s.total_tokens for s in history) or 1
            glyphs = render_sparkline([s.total_tokens for s in history])
            rows.append(Row(f"  {glyphs}  ({len(history)}d, "
                            f"peak {human_tokens(peak)})", "info"))
        else:
            rows.append(Row("  no history yet", "dim"))

    rows.append(Row("", "dim"))
    rows.append(Row(f"{H}{H} TASKS LANDED / DAY ({ANALYTICS_WINDOW_DAYS}d) {H * 3}", "head"))
    if landed:
        counts = [landed.get(d, 0) for d in window_days]
        peak_c = max(counts) or 1
        glyphs = render_sparkline(counts)
        rows.append(Row(f"  {glyphs}  (peak {peak_c}/day, "
                        f"{sum(counts)} tasks total)", "good"))
    else:
        rows.append(Row(
            f"  no landed commits in the last {ANALYTICS_WINDOW_DAYS}d "
            "(or `main` unavailable)", "dim"))

    rows.append(Row("", "dim"))
    rows.append(Row(f"{H}{H} CODE CHURN ({ANALYTICS_WINDOW_DAYS}d) {H * 3}", "head"))
    if churn:
        ins_series = [churn.get(d, (0, 0))[0] for d in window_days]
        del_series = [churn.get(d, (0, 0))[1] for d in window_days]
        rows.append(Row(f"  + {render_sparkline(ins_series)}  "
                        f"{sum(ins_series):>6} lines added", "good"))
        rows.append(Row(f"  - {render_sparkline(del_series)}  "
                        f"{sum(del_series):>6} lines removed", "bad"))
    else:
        rows.append(Row("  no churn data", "dim"))

    rows.append(Row("", "dim"))
    rows.append(Row(f"{H}{H} COMMIT CALENDAR ({len(calendar_days)}d) {H * 3}", "head"))
    rows.extend(commit_calendar_rows(landed, calendar_days))

    rows.append(Row("", "dim"))
    rows.append(Row(f"{H}{H} MODEL x DAY (tokens, 7d) {H * 3}", "head"))
    if usage_mod is None:
        rows.append(Row("  usage.py not found; model matrix unavailable.", "dim"))
    else:
        rows.extend(model_day_matrix_rows(usage_mod, repo, window_days[-7:]))

    return rows


#: Width of one quota gauge bar in the USAGE pane's QUOTA REMAINING block.
QUOTA_BAR_WIDTH = 20

#: Backends --capture-usage captures. Both, so the one opt-in flag covers
#: whichever backend is actually in use rather than needing a flag each.
QUOTA_CAPTURE_BACKENDS = ("claude", "antigravity")


def expected_agy_families(bl_mod: ModuleType, repo: Path) -> tuple[str, ...]:
    """The agy model families the dispatch picker would offer for this repo.
    The usage pane expects a gauge for each, so a family agy didn't report
    this run gets its own fallback line instead of silently vanishing from
    the pane."""
    models = bl_mod.BACKEND_MODELS.get("antigravity", ())
    return tuple(sorted(agy_family_groups(models, read_agy_families(repo))))


def capture_quota_gauges(usage_mod: ModuleType, bl_mod: ModuleType, repo: Path,
                         backends: tuple[str, ...] = QUOTA_CAPTURE_BACKENDS,
                         claude_bin: str | None = None
                         ) -> tuple[Any, dict[str, Any] | None]:
    """The USAGE pane's opt-in quota capture (`--capture-usage`), for
    quota_rows() to render.

    Called once, at startup, and deliberately never from refresh() or any
    rows() path: every capture spawns a real interactive session under a
    pty, so a capture on the pane's refresh timer would spawn one per
    redraw. Each backend fails on its own -- a `claude` capture that times
    out still leaves agy's groups renderable, and vice versa.

    agy's result is widened to every expected family so an unreported group
    is present-but-None rather than absent, which is what lets quota_rows()
    fall back for that group alone."""
    claude_gauge: Any = None
    agy_gauges: dict[str, Any] | None = None
    if "claude" in backends:
        claude_gauge = usage_mod.capture_usage_gauge(claude_bin=claude_bin)
    if "antigravity" in backends:
        captured = usage_mod.capture_agy_usage_gauges()
        families = set(expected_agy_families(bl_mod, repo)) | set(captured)
        agy_gauges = {family: captured.get(family)
                      for family in sorted(families)}
    return claude_gauge, agy_gauges


def quota_bar_row(label: str, remaining_pct: int) -> Row:
    """One quota gauge, drawn with the pane's existing bar_segments()
    vocabulary. `remaining_pct` is the share LEFT, while pct_tone() reads a
    share USED (>=90 is 'bad'), so the tone comes from the complement --
    otherwise a nearly-exhausted quota would paint green."""
    left = max(0, min(100, remaining_pct))
    tone = pct_tone(100 - left)
    text, parts = seg(
        (f"      {label:<7}", "dim"),
        *bar_segments([(left, tone), (100 - left, "dim")], QUOTA_BAR_WIDTH),
        (f" {left:>3}%", "head"))
    return Row(text, "text", None, parts)


def quota_rows(usage_mod: ModuleType, claude_gauge: Any,
               agy_gauges: dict[str, Any] | None) -> list[Row]:
    """The USAGE pane's QUOTA REMAINING block: a 5h and a weekly gauge for
    every backend that was captured.

    THE SIGN, stated once for both backends: every percentage rendered here
    is what is REMAINING. claude's LimitGauge holds UM-05's percentages
    *used*, so it is inverted on the way in; agy's GroupGauge already holds
    remaining -- its own screen captions the bar "49% remaining" -- and is
    shown exactly as captured.

    agy's groups are keyed and headed by agy_family_groups()'s family
    vocabulary, the same one the dispatch picker shows, rather than a second
    label set that could drift from it. A backend or group with no captured
    data -- capture off, capture failed, or a group agy didn't report this
    run -- falls back to NOT_ON_DISK_LINE for that block alone; one
    failure never withholds another's gauge."""
    rows = [Row(f"{H}{H} QUOTA REMAINING {H * 3}", "head")]

    rows.append(Row("  claude", "id"))
    if claude_gauge is None:
        rows.append(Row(f"    {usage_mod.NOT_ON_DISK_LINE}", "dim"))
    else:
        rows.append(quota_bar_row("5h", 100 - claude_gauge.session_pct))
        rows.append(quota_bar_row("weekly", 100 - claude_gauge.week_pct))

    rows.append(Row("  antigravity", "id"))
    if not agy_gauges:
        rows.append(Row(f"    {usage_mod.NOT_ON_DISK_LINE}", "dim"))
    for family in sorted(agy_gauges or {}):
        group = agy_gauges[family] if agy_gauges else None
        rows.append(Row(f"    {DOT} {family}", "dim"))
        if group is None:
            rows.append(Row(f"      {usage_mod.NOT_ON_DISK_LINE}", "dim"))
        else:
            rows.append(quota_bar_row("5h", group.five_hour_remaining_pct))
            rows.append(quota_bar_row("weekly", group.weekly_remaining_pct))
    return rows


def usage_rows(usage_mod: ModuleType | None, repo: Path,
               daily: tuple[int, float] | None,
               claude_gauge: Any = None,
               agy_gauges: dict[str, Any] | None = None) -> list[Row]:
    """The USAGE pane: this-session/today/this-week totals, the quota
    gauges, the model split as a proportional bar, a 14-day token
    sparkline, UM-02's breakdowns and suggestions, and the history path. No
    row carries a payload -- this pane is a dashboard, nothing on it is
    selectable.

    `claude_gauge`/`agy_gauges` are the opt-in captures usage.py's L5
    produces (see quota_rows() for the sign). They are passed in, never
    captured here: refreshing this pane must never spawn a session."""
    if usage_mod is None:
        return [Row("usage.py not found; USAGE pane unavailable "
                    "(falling back to the original four panes).", "dim")]

    data = _collect_usage_data(usage_mod, repo)
    rows: list[Row] = []

    rows.append(Row(f"{H}{H} THIS SESSION / TODAY / THIS WEEK {H * 3}", "head"))
    for label, bd in (("session", data.session_bd), ("today", data.today_bd),
                      ("this week", data.week_bd)):
        text, parts = seg((f"  {label:<10}", "dim"),
                          (f"{bd.turn_count:>4} turns  ", "dim"),
                          (f"{human_tokens(bd.total_tokens):>6} tok  ", "text"),
                          (f"${bd.total_cost:.2f}", "info"))
        rows.append(Row(text, "text", None, parts))

    rows.append(Row("", "dim"))
    rows.extend(quota_rows(usage_mod, claude_gauge, agy_gauges))

    rows.append(Row("", "dim"))
    rows.append(Row(f"{H}{H} MODEL SPLIT (this week) {H * 3}", "head"))
    total = sum(data.model_tokens.values())
    if not data.model_tokens:
        rows.append(Row("  no data this week", "dim"))
    else:
        ranked = sorted(data.model_tokens.items(), key=lambda kv: -kv[1])
        counts = [(count, MODEL_SPLIT_TONES[i % len(MODEL_SPLIT_TONES)])
                 for i, (_, count) in enumerate(ranked)]
        text, parts = seg(*bar_segments(counts))
        rows.append(Row(text, "text", None, parts))
        for i, (model, count) in enumerate(ranked):
            tone = MODEL_SPLIT_TONES[i % len(MODEL_SPLIT_TONES)]
            pct = round(100 * count / total) if total else 0
            text, parts = seg((f"  {DOT} ", tone), (f"{model:<24} ", "id"),
                              (f"{pct:>3}%  {human_tokens(count)}", "text"))
            rows.append(Row(text, "text", None, parts))

    rows.append(Row("", "dim"))
    rows.append(Row(f"{H}{H} TOKEN TREND ({USAGE_SPARKLINE_DAYS}d) {H * 3}", "head"))
    if data.history:
        peak = max(s.total_tokens for s in data.history) or 1
        glyphs = render_sparkline([s.total_tokens for s in data.history])
        rows.append(Row(f"  {glyphs}  ({len(data.history)}d, "
                        f"peak {human_tokens(peak)})", "info"))
    else:
        rows.append(Row("  no history yet", "dim"))

    rows.append(Row("", "dim"))
    rows.append(Row(f"{H}{H} BREAKDOWN (this week) {H * 3}", "head"))
    cache_tone = ("good" if data.week_bd.cache_hit_rate
                 >= usage_mod.LOW_CACHE_HIT_RATE_THRESHOLD else "warn")
    rows.append(Row(f"  cache hit rate         "
                    f"{data.week_bd.cache_hit_rate:>5.1f}%", cache_tone))
    ctx_pct = round(data.week_bd.high_context_token_share)
    rows.append(Row(f"  high-context share     {ctx_pct:>3}%", pct_tone(ctx_pct)))
    side_pct = round(data.week_bd.sidechain_token_share)
    rows.append(Row(f"  sidechain token share   {side_pct:>3}%", pct_tone(side_pct)))
    rows.append(Row(f"  web search / fetch      {data.week_bd.web_search_requests} "
                    f"/ {data.week_bd.web_fetch_requests}", "dim"))

    rows.append(Row("", "dim"))
    rows.append(Row(f"{H}{H} SUGGESTIONS ({len(data.suggestions)}) {H * 3}",
                    "warn" if data.suggestions else "good"))
    if not data.suggestions:
        rows.append(Row("  none -- nothing crossed a threshold", "good"))
    for suggestion in data.suggestions:
        rows.append(Row(f"  {DOT} {suggestion.text}", "warn"))

    rows.append(Row("", "dim"))
    text, parts = seg(("history: ", "dim"),
                      (str(usage_mod.history_path(repo)), "id"))
    rows.append(Row(text, "dim", None, parts))
    rows.append(Row("Cost is advisory -- a local estimate, not a billing "
                    "record.", "dim"))
    return rows


def usage_summary_lines(usage_mod: ModuleType | None, repo: Path) -> list[str]:
    """The same content usage_rows() holds, as plain text -- shared by
    render_summary() (--no-tui) and the --usage one-shot dashboard."""
    if usage_mod is None:
        return ["usage.py not found; USAGE pane unavailable."]

    data = _collect_usage_data(usage_mod, repo)
    lines = ["usage:"]
    for label, bd in (("session", data.session_bd), ("today", data.today_bd),
                      ("this week", data.week_bd)):
        lines.append(f"  {label:<10} {bd.turn_count:>4} turns  "
                     f"{human_tokens(bd.total_tokens):>6} tok  "
                     f"${bd.total_cost:.2f}")

    lines.append("  model split (this week):")
    total = sum(data.model_tokens.values())
    if not data.model_tokens:
        lines.append("    no data this week")
    for model, count in sorted(data.model_tokens.items(), key=lambda kv: -kv[1]):
        pct = round(100 * count / total) if total else 0
        lines.append(f"    {model:<24} {pct:>3}%  {human_tokens(count)}")

    if data.history:
        peak = max(s.total_tokens for s in data.history) or 1
        glyphs = render_sparkline([s.total_tokens for s in data.history])
        lines.append(f"  token trend ({USAGE_SPARKLINE_DAYS}d): {glyphs}  "
                     f"peak {human_tokens(peak)}")
    else:
        lines.append(f"  token trend ({USAGE_SPARKLINE_DAYS}d): no history yet")

    lines.append(f"  cache hit rate:         {data.week_bd.cache_hit_rate:.1f}%")
    lines.append(f"  high-context share:     "
                 f"{data.week_bd.high_context_token_share:.1f}%")
    lines.append(f"  sidechain token share:  "
                 f"{data.week_bd.sidechain_token_share:.1f}%")

    lines.append(f"  suggestions: {len(data.suggestions)}")
    for suggestion in data.suggestions:
        lines.append(f"    - {suggestion.text}")

    lines.append(f"  history: {usage_mod.history_path(repo)}")
    lines.append("  Cost is advisory -- a local estimate, not a billing record.")
    return lines


# =====================================================================
# L4  TUI  --  curses. Adapts to the terminal: unlike backlog.py's
#     fixed-80-column renderer, nothing here is diffed or fixtured, so
#     the determinism constraint that pins that file does not apply.
# =====================================================================

HELP = [
    "  tab / 1-5      switch pane            j k / arrows   move cursor",
    "  enter          act on the selection   r              refresh",
    "  a              apply all auto-fixable doctor findings",
    "  t              BACKLOG: toggle group-by-type vs group-by-bucket",
    "  D              BACKLOG: mark all REVIEW tasks done",
    "  d              BACKLOG: focus dependency tree in inspector",
    "  q              quit                   ?              this help",
    "",
    "  Every action previews its exact command and waits for confirmation.",
]

#: Below this terminal width the list keeps the full row and the detail
#: panel is dropped rather than squeezed into something unreadable.
MIN_SPLIT_WIDTH = 66


#: How often an idle cockpit polls for changes made by another process (a
#: task moved, an agent finished, a PR landed) instead of waiting for 'r'.
#: A single named constant so the cadence is easy to retune later.
POLL_INTERVAL_MS = 1000


def _is_poll_tick(key: int) -> bool:
    """True when `key` is curses' ERR/timeout sentinel (-1) -- i.e.
    stdscr.getch() woke up because POLL_INTERVAL_MS elapsed with no
    keypress, not because the operator pressed anything. Curses-free and
    driven by plain ints so it's unit-testable without a real terminal."""
    return key == -1


def run_tui(bl: ModuleType, repo: Path, args: argparse.Namespace) -> int:
    import curses

    binary = claude_bin(args.claude_bin)

    def paint(stdscr: Any) -> int:
        curses.curs_set(0)
        tones: dict[str, int] = {}
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            use_256 = curses.COLORS >= 256
            palette = [
                (name, color_256 if use_256 else color_8)
                for name, (color_256, color_8) in THEME_PALETTE.items()
            ]
            for index, (name, colour) in enumerate(palette, start=1):
                curses.init_pair(index, colour, -1)
                tones[name] = curses.color_pair(index)
            tones["head"] |= curses.A_BOLD
            tones["id"] |= curses.A_BOLD
            tones["dim"] = curses.A_DIM
            # "text" is deliberately absent: body text (titles, messages)
            # stays the terminal's default foreground, the one colour every
            # accent above is chosen to stand out against.

        state = _State(bl, repo, args, binary, tones)
        state.refresh()
        while True:
            state.draw(stdscr)
            # Bounded only around this outer-loop getch(): a tick must be
            # able to interrupt an idle wait so refresh() runs on its own,
            # but _modal/_menu/_confirm/_prompt_text/_settings run their own
            # nested getch() loops for confirmations -- those stay blocking
            # (timeout(-1)) below so an automatic refresh can never land
            # mid-prompt and shift rows/indices under it.
            stdscr.timeout(POLL_INTERVAL_MS)
            try:
                key = stdscr.getch()
            except KeyboardInterrupt:
                return 0
            if _is_poll_tick(key):
                state.refresh(force=False)
                continue
            stdscr.timeout(-1)
            if state.handle(stdscr, key) is False:
                return 0

    return int(curses.wrapper(paint))


class _State:
    """TUI state. Holds the snapshot, the cursor, and the last result line."""

    def __init__(self, bl: ModuleType, repo: Path, args: argparse.Namespace,
                 binary: str, tones: dict[str, int]) -> None:
        self.bl, self.repo, self.args = bl, repo, args
        self.binary, self.tones = binary, tones
        self.pool = get_subprocess_pool()
        self.usage_mod = load_usage(repo)
        self.panes = active_panes(self.usage_mod)
        self.pane = 0
        self.cursor = [0] * len(PANES)
        self.scroll = [0] * len(PANES)
        self.status = ("? for help" if self.usage_mod is not None else
                       "? for help (usage.py not found; USAGE pane unavailable)")
        self.snap: Snapshot | None = None
        self.agents: list[Agent] = []
        self.daily: tuple[int, float] | None = None
        # Quota gauges are captured at most once, here -- never in
        # refresh(), which runs on the poll timer (see capture_quota_gauges).
        self.claude_gauge: Any = None
        self.agy_gauges: dict[str, Any] | None = None
        if getattr(args, "capture_usage", False) and self.usage_mod is not None:
            self.claude_gauge, self.agy_gauges = capture_quota_gauges(
                self.usage_mod, bl, repo, claude_bin=args.claude_bin)
        self.pending: frozenset[str] = frozenset()
        self.tree_focus = False
        self.tree_cursor = 0
        self.collapsed_nodes: set[tuple[str, str, int]] = set()
        # BACKLOG pane's rendering mode: bucket-grouped (default) or
        # type-grouped (backlog_rows_by_type), toggled with 't' -- see
        # handle() and HELP.
        self.backlog_by_type = False
        # Per-pane row cache: draw()/handle()/detail_lines() each call
        # rows() once per keypress, and USAGE's rows() rescans every
        # transcript on disk -- without this a single j/k press paid for
        # that scan three times over. Cleared whenever the underlying data
        # (refresh()) actually changes.
        self._rows_cache: dict[int, list[Row]] = {}
        # Background PR fetches for the passive inspector (see
        # detail_lines()), keyed by branch so the render loop never blocks
        # on `gh pr view` just because the cursor sits on a review task.
        self._pr_futures: dict[str, Future[dict[str, Any] | None]] = {}
        # FIFO queue of pending "mark done" landing requests, drained by one
        # background worker so confirming a verdict accepts it without blocking.
        self._land_queue: list[tuple[list[str], str]] = []
        self._land_future: Future[str] | None = None
        self._land_current: tuple[list[str], str] | None = None
        self._land_results: list[str] = []
        # Background probe pipeline: a tick submits it if none is in flight
        # and returns; draw() adopts the result when it completes. The initial
        # (synchronous) refresh() at startup is the only call that blocks.
        self._refresh_future: Future[tuple[
            Snapshot, list[Agent], tuple[int, float] | None, frozenset[str]
        ]] | None = None

    @property
    def _land_task_id(self) -> str | None:
        return self._land_current[1] if self._land_current else None

    # -- data ---------------------------------------------------------
    def _probe_pipeline(self) -> tuple[
        Snapshot, list[Agent], tuple[int, float] | None, frozenset[str]
    ]:
        """Run the full probe pipeline synchronously and return its results.
        Safe to call from any thread: all functions it invokes are either
        thread-safe or stateless."""
        snap = snapshot(self.bl, self.repo, self.args.lanes,
                        not self.args.no_git)
        agents = probe_agents(self.repo, self.binary)
        daily = daily_usage()
        pending = pending_landed_task_ids(self.bl, self.repo)
        return snap, agents, daily, pending

    def _adopt_probe_result(
        self,
        result: tuple[Snapshot, list[Agent], tuple[int, float] | None, frozenset[str]],
    ) -> None:
        """Install the tuple returned by _probe_pipeline, invalidating only
        the pane caches whose source data actually changed."""
        new_snap, new_agents, new_daily, new_pending = result
        snap_changed = new_snap is not self.snap
        agents_changed = new_agents != self.agents
        daily_changed = new_daily != self.daily
        pending_changed = new_pending != self.pending
        self.snap = new_snap
        self.agents = new_agents
        self.daily = new_daily
        self.pending = new_pending
        # Evict only the pane caches that actually consumed the changed data.
        # snap  → BACKLOG(0), DOCTOR(1), FLEET(2), NEXT(3)
        # agents → BACKLOG(0), FLEET(2)
        # daily  → FLEET(2), USAGE(6)
        # pending → BACKLOG(0)
        stale: set[int] = set()
        if snap_changed:
            stale.update((0, 1, 2, 3))
        if agents_changed:
            stale.update((0, 2))
        if daily_changed:
            stale.update((2, 6))
        if pending_changed:
            stale.add(0)
        for pane_idx in stale:
            self._rows_cache.pop(pane_idx, None)

    def refresh(self, force: bool = True) -> None:
        """Refresh state from the probe pipeline.

        ``force=True`` (the default; used by the startup call and the 'r'
        keypress) runs synchronously on the calling thread -- the operator
        asked explicitly, so a brief pause is acceptable.

        ``force=False`` (automatic poll tick) submits the probe pipeline to
        the background pool if none is already in flight, then returns
        immediately so the input thread stays responsive.  The result is
        adopted by ``_poll_refresh()`` on the next ``draw()`` call.
        """
        if force:
            result = self._probe_pipeline()
            self._adopt_probe_result(result)
            self.tree_focus = False
        else:
            # Do not stack a second request on top of one already in flight.
            if self._refresh_future is not None and not self._refresh_future.done():
                return
            self._refresh_future = self.pool.submit(self._probe_pipeline)

    def _poll_refresh(self) -> None:
        """Non-blocking check for a completed background probe (poll-tick
        path).  Called at the top of draw() -- adopts the result and resets
        the future so the next tick can submit a new one."""
        future = self._refresh_future
        if future is None or not future.done():
            return
        self._refresh_future = None
        try:
            result = future.result()
        except Exception:
            return  # probe failed; keep the previous snapshot
        self._adopt_probe_result(result)
        self.tree_focus = False

    def _enqueue_land(self, ids: list[str], desc: str) -> None:
        self._land_queue.append((ids, desc))
        if self._land_future is None or self._land_future.done():
            self._start_next_land()
        else:
            queued_count = len(self._land_queue)
            current_desc = self._land_current[1] if self._land_current else ""
            self.status = f"landing {current_desc}... ({queued_count} queued)"

    def _start_next_land(self) -> None:
        if not self._land_queue:
            self._land_future = None
            self._land_current = None
            return
        ids, desc = self._land_queue.pop(0)
        self._land_current = (ids, desc)
        self._land_future = land_tasks_done_async(self.bl, self.repo, ids)
        if self._land_queue:
            self.status = f"landing {desc}... ({len(self._land_queue)} queued)"
        else:
            self.status = f"landing {desc}..."

    def _poll_land(self) -> None:
        """Non-blocking check for a "mark done" landing kicked off in the
        background by mark_task_done(). Picked up on the next draw (at most
        one poll tick later) instead of freezing the loop until it lands."""
        future = self._land_future
        if future is None or not future.done():
            return
        res = future.result()
        self._land_results.append(res)
        self.status = res
        self._rows_cache.clear()
        self._start_next_land()

    def rows(self) -> list[Row]:
        cached = self._rows_cache.get(self.pane)
        if cached is not None:
            return cached
        snap = self.snap
        assert snap is not None
        if self.pane == 0:
            if self.backlog_by_type:
                computed = backlog_rows_by_type(snap, self.pending, self.agents)
            else:
                families = read_agy_families(self.repo)
                computed = backlog_rows(snap, self.pending, self.agents, self.bl, families)
        elif self.pane == 1:
            computed = doctor_rows(snap)
        elif self.pane == 2:
            computed = fleet_rows(snap, self.agents, self.daily)
        elif self.pane == 3:
            computed = next_rows(snap)
        elif self.pane == 4:
            computed = analytics_rows(self.usage_mod, self.repo)
        elif self.pane == 5:
            computed = workflow_rows(self.repo)
        else:
            computed = usage_rows(self.usage_mod, self.repo, self.daily,
                                  self.claude_gauge, self.agy_gauges)
        self._rows_cache[self.pane] = computed
        return computed

    def selected(self) -> Any:
        rows = self.rows()
        index = self.cursor[self.pane]
        if 0 <= index < len(rows):
            return rows[index].payload
        return None

    def _pr_for_branch(self, branch: str) -> dict[str, Any] | None:
        """Non-blocking PR lookup for the passive inspector: a cache hit
        returns immediately, a miss kicks off a background `gh pr view` and
        returns None for this draw -- review_detail_lines() already reads
        None as "no PR found yet", and the fetch's own probe_pr() populates
        _PR_CACHE for the next draw to pick up without re-fetching."""
        key = (str(self.repo.resolve()), branch)
        cached = _PR_CACHE.get(key)
        if cached is not None:
            return cached
        future = self._pr_futures.get(branch)
        if future is None:
            self._pr_futures[branch] = probe_pr_async(self.repo, branch)
            return None
        if not future.done():
            return None
        del self._pr_futures[branch]
        return future.result()

    def _current_tree_nodes(self) -> tuple[list[DependencyNode], list[str], list[str]]:
        """Compute visible dependency tree nodes and lines for the selected task."""
        snap = self.snap
        if snap is None or not snap.by_id:
            return [], [], []
        target = self.selected()
        if target is None or not isinstance(target, dict):
            return [], [], []
        task_id = target.get("id")
        if not task_id or task_id not in snap.by_id:
            return [], [], []
        return dependency_tree(task_id, snap.by_id, collapsed_nodes=self.collapsed_nodes)

    def detail_lines(self) -> list[Any]:
        """Full detail for whatever the cursor currently highlights,
        mirrored live in the right-hand panel (inspector). Displays task
        rationale/description, block snippets, git worktree path, retry counts,
        token burn, live agent log tail stream, and session history."""
        snap = self.snap
        assert snap is not None
        target = self.selected()
        if target is None:
            return ["INSPECTOR", "", "No item selected."]
        if self.pane == 0 and isinstance(target, dict):
            if target.get("status") == "review":
                branch = (branch_for_task(target["id"], snap.git["branches"])
                          if snap.git else None)
                pr = self._pr_for_branch(branch) if branch else None
                agent = agent_for_task(target["id"], self.agents)
                return review_detail_lines(
                    target, pr, branch, agent=agent, repo=self.repo,
                    by_id=snap.by_id,
                    tree_cursor=self.tree_cursor if self.tree_focus else None,
                    collapsed_nodes=self.collapsed_nodes if self.tree_focus else None
                )
            agent = agent_for_task(target["id"], self.agents)
            branch = (branch_for_task(target["id"], snap.git["branches"])
                      if snap.git else None)
            worktree = agent.cwd if agent else (
                str(self.repo / ".claude" / "worktrees" / branch.replace("task/", ""))
                if branch else None
            )
            return task_detail_lines(
                target, agent=agent, worktree=worktree, repo=self.repo,
                by_id=snap.by_id,
                tree_cursor=self.tree_cursor if self.tree_focus else None,
                collapsed_nodes=self.collapsed_nodes if self.tree_focus else None
            )
        if self.pane == 1:
            return finding_detail_lines(
                target, plan_fix(target, self.repo, snap.by_id), repo=self.repo)
        if self.pane == 2 and isinstance(target, Agent):
            return agent_detail_lines(target, repo=self.repo)
        if self.pane == 2:
            return lane_detail_lines(target, repo=self.repo)
        if self.pane == 3:
            return next_detail_lines(target, repo=self.repo)
        if self.pane == 5 and isinstance(target, WorkflowStep):
            return workflow_step_detail_lines(target, repo=self.repo)
        if self.pane == 5 and isinstance(target, AgentDef):
            return agent_roster_detail_lines(target, repo=self.repo)
        return ["INSPECTOR - USAGE PANE", "", "Usage stats dashboard."]

    # -- drawing ------------------------------------------------------
    def draw(self, stdscr: Any) -> None:
        import curses

        self._poll_refresh()
        self._poll_land()
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        snap = self.snap
        assert snap is not None

        title = f"{snap.cfg['project'] or snap.repo.name}"
        head_attr = self.tones.get("head", 0) | curses.A_BOLD
        _put(stdscr, 0, 0, f" {DOT} {title}", head_attr)
        _put(stdscr, 0, len(f" {DOT} {title}") + 1,
             f"{ARROW} {snap.repo}", curses.A_DIM)

        offset = 0
        for index, name in enumerate(self.panes):
            label = f" {index + 1}:{name} "
            tone = self.tones.get(PANE_TONE.get(name, "head"), 0)
            attr = (tone | curses.A_REVERSE | curses.A_BOLD if index == self.pane
                   else tone | curses.A_DIM)
            _put(stdscr, 1, offset, label, attr)
            offset += len(label) + 1
        _put(stdscr, 2, 0, H * (width - 1), curses.A_DIM)

        top, bottom = 3, height - 2
        view = max(1, bottom - top)
        rows = self.rows()
        cursor = min(self.cursor[self.pane], max(0, len(rows) - 1))
        self.cursor[self.pane] = cursor
        scroll = self.scroll[self.pane]
        scroll = min(scroll, cursor)
        scroll = max(scroll, cursor - view + 1)
        scroll = max(0, min(scroll, max(0, len(rows) - view)))
        self.scroll[self.pane] = scroll

        detail = self.detail_lines()
        list_w, detail_col = width - 1, None
        if width >= MIN_SPLIT_WIDTH:
            list_w = max(24, (width - 3) // 2)
            detail_col = list_w + 3
            for y in range(top, bottom):
                _put(stdscr, y, list_w + 1, V, curses.A_DIM)

        for line, row in enumerate(rows[scroll:scroll + view]):
            y = top + line
            selected = scroll + line == cursor and row.payload is not None
            if row.segments:
                col = 0
                for part_text, part_tone in row.segments:
                    if col >= list_w:
                        break
                    attr = self.tones.get(part_tone, 0)
                    if selected:
                        attr |= curses.A_REVERSE
                    _put(stdscr, y, col, part_text[:list_w - col], attr)
                    col += len(part_text)
            else:
                attr = self.tones.get(row.tone, 0)
                if selected:
                    attr |= curses.A_REVERSE
                _put(stdscr, y, 0, row.text[:list_w], attr)

        if detail_col is not None and detail:
            for line, entry in enumerate(detail[:view]):
                if isinstance(entry, tuple):
                    dtext: str = entry[0]
                    dtone: str = entry[1]
                    _put(stdscr, top + line, detail_col, dtext,
                         self.tones.get(dtone, 0) if dtone else 0)
                else:
                    _put(stdscr, top + line, detail_col, entry)

        _put(stdscr, height - 2, 0, H * (width - 1), curses.A_DIM)
        status_attr = self.tones.get(_status_tone(self.status), 0) | curses.A_REVERSE
        _put(stdscr, height - 1, 0, f" {self.status} "[:width - 1], status_attr)
        stdscr.refresh()

    # -- input --------------------------------------------------------
    def handle(self, stdscr: Any, key: int) -> bool:
        import curses

        if key in (ord("q"), 27):
            if self.tree_focus and key == 27:
                self.tree_focus = False
                return True
            return False

        if self.pane == 0 and key == ord("d"):
            nodes, _, _ = self._current_tree_nodes()
            if nodes:
                self.tree_focus = not self.tree_focus
                if self.tree_focus:
                    self.tree_cursor = 0
                self.status = "dependency tree focused" if self.tree_focus else "BACKLOG"
            else:
                self.status = "no dependencies or descendants for this task"
            return True

        if self.pane == 0 and self.tree_focus:
            nodes, _, _ = self._current_tree_nodes()
            if not nodes:
                self.tree_focus = False
            else:
                if key in (ord("j"), curses.KEY_DOWN):
                    self.tree_cursor = min(len(nodes) - 1, self.tree_cursor + 1)
                    return True
                elif key in (ord("k"), curses.KEY_UP):
                    self.tree_cursor = max(0, self.tree_cursor - 1)
                    return True
                elif key in (ord("l"), curses.KEY_RIGHT):
                    node = nodes[self.tree_cursor]
                    if node.has_children:
                        if not node.is_expanded:
                            self.collapsed_nodes.discard(node.node_key)
                        else:
                            if self.tree_cursor + 1 < len(nodes):
                                self.tree_cursor += 1
                    return True
                elif key in (ord("h"), curses.KEY_LEFT):
                    node = nodes[self.tree_cursor]
                    if node.has_children and node.is_expanded:
                        self.collapsed_nodes.add(node.node_key)
                    elif node.parent_id is not None:
                        parent_idx = next(
                            (i for i in range(self.tree_cursor - 1, -1, -1)
                             if nodes[i].section == node.section
                             and nodes[i].depth == node.depth - 1
                             and nodes[i].task_id == node.parent_id),
                            None
                        )
                        if parent_idx is not None:
                            self.tree_cursor = parent_idx
                    else:
                        self.tree_focus = False
                    return True
                elif key in (10, 13, curses.KEY_ENTER):
                    node = nodes[self.tree_cursor]
                    target_id = node.task_id
                    rows = self.rows()
                    idx = next((i for i, r in enumerate(rows)
                                if isinstance(r.payload, dict)
                                and r.payload.get("id") == target_id), None)
                    if idx is not None:
                        self.cursor[0] = idx
                    self.tree_focus = False
                    return True

        if key in (ord("\t"), curses.KEY_RIGHT, curses.KEY_LEFT) or \
                ord("1") <= key < ord("1") + len(self.panes):
            self.tree_focus = False
            self.pane = next_pane(self.pane, key, self.panes)
        elif key in (ord("j"), curses.KEY_DOWN):
            self.tree_focus = False
            self.cursor[self.pane] = _step(self.rows(), self.cursor[self.pane], 1)
        elif key in (ord("k"), curses.KEY_UP):
            self.tree_focus = False
            self.cursor[self.pane] = _step(self.rows(), self.cursor[self.pane], -1)
        elif key == ord("r"):
            self.status = "refreshing..."
            self.draw(stdscr)
            self.refresh()
            self.status = "refreshed"
        elif key == ord("?"):
            _modal(stdscr, "KEYS", HELP, self.tones)
        elif key == ord("a"):
            self.batch_all(stdscr)
        elif key == ord("t") and self.pane == 0:
            self.backlog_by_type = not self.backlog_by_type
            self.cursor[0] = 0
            self.tree_focus = False
            self._rows_cache.pop(0, None)
            self.status = ("BACKLOG: grouped by type" if self.backlog_by_type
                          else "BACKLOG: grouped by bucket")
        elif key == ord("D") and self.pane == 0:
            self.mark_all_reviewed_done(stdscr)
        elif key in (10, 13, curses.KEY_ENTER):
            self.activate(stdscr)
        return True

    def activate(self, stdscr: Any) -> None:
        target = self.selected()
        if target is None:
            return
        if self.pane == 1:
            self.act_finding(stdscr, target)
        elif self.pane == 0:
            self.act_task(stdscr, target)
        elif self.pane == 2 and isinstance(target, Agent):
            self.act_agent(stdscr, target)
        elif self.pane == 2:
            self.act_lane(stdscr, target)
        else:
            self.act_next(stdscr)

    # -- per-pane actions ---------------------------------------------
    def act_finding(self, stdscr: Any, finding: Any) -> None:
        snap = self.snap
        assert snap is not None
        plan = plan_fix(finding, self.repo, snap.by_id)
        same = [f for f in snap.findings if f.code == finding.code]
        options = []
        if plan.steps:
            options.append(("Fix this one", "one"))
        if plan.steps and finding.code in BATCHABLE and len(same) > 1:
            options.append((f"Fix all {len(same)} {finding.code} findings", "all"))
        options.append(("Explain this finding", "why"))
        choice = _menu(stdscr, finding.code, [label for label, _ in options],
                       self.tones)
        if choice is None:
            return
        kind = options[choice][1]
        if kind == "why":
            _modal(stdscr, finding.code, finding_detail_lines(finding, plan),
                   self.tones)
            return
        targets = same if kind == "all" else [finding]
        steps: list[Step] = []
        for item in targets:
            steps += plan_fix(item, self.repo, snap.by_id).steps
        self.run(stdscr, plan.title if kind == "one"
                 else f"Fix {len(targets)} x {finding.code}", steps, plan.caution)

    def act_task(self, stdscr: Any, task: Any) -> None:
        if task["status"] == "review":
            self.act_review_task(stdscr, task)
            return
        snap = self.snap
        assert snap is not None
        human_gated = any(t["id"] == task["id"]
                          for t in snap.buckets["needs_routing"])
        statuses = ["todo", "in_progress", "review", "blocked"]
        options: list[tuple[str, str]] = [
            ("Dispatch this task in the background", "dispatch")]
        if human_gated:
            options.append(("Approve", "approve"))
        options.append(("Talk to Claude about this task", "talk"))
        options += [(f"Set status -> {s}", s) for s in statuses]
        choice = _menu(stdscr, task["id"], [label for label, _ in options],
                       self.tones)
        if choice is None:
            return
        kind = options[choice][1]
        if kind == "dispatch":
            self.dispatch_task(stdscr, task)
        elif kind == "approve":
            self.approve_task(stdscr, task)
        elif kind == "talk":
            self.act_talk_to_claude(stdscr, task)
        elif kind == "done":
            return
        else:
            status = kind
            self.run(stdscr, f"{task['id']} -> {status}",
                     [Step(f"tasks.toml: {task['id']} status "
                           f"{task['status']} -> {status}",
                           edit=(task["id"], status))],
                     caution="NightShift and this cockpit never write 'done' "
                             "(ADR 0009); CI and review land a task."
                     if status == "review" else "")

    def approve_task(self, stdscr: Any, task: Any) -> None:
        """The human sign-off the HUMAN GATED bucket exists to gate on:
        classify() only ever puts a task in the autonomous 'ready' bucket
        when its routing is impl/tester (backlog.py's AUTONOMOUS set), so
        approving a task whose routing is architect/reviewer means
        rewriting `routing` itself -- the one field every other edit in
        this file leaves alone."""
        routing = autonomous_routing_for(task)
        self.run(stdscr, f"Approve {task['id']}",
                 [Step(f"tasks.toml: {task['id']} routing "
                       f"{task['routing']} -> {routing} (approved for "
                       "autonomous dispatch)",
                       routing_edit=(task["id"], routing))])

    def act_talk_to_claude(self, stdscr: Any, task: Any) -> None:
        """Hand the operator the real terminal for an interactive `claude`
        session about `task`. The only foreground/attached subprocess this
        file runs -- every other dispatch path is intentionally detached or
        captured because the TUI owns the terminal, so this suspends curses
        first and restores it exactly before returning control."""
        import curses

        curses.def_prog_mode()
        curses.endwin()
        try:
            self.status = talk_to_claude(self.repo, self.binary, task)
        finally:
            curses.reset_prog_mode()
        self.refresh()
        self.draw(stdscr)

    def act_review_task(self, stdscr: Any, task: Any) -> None:
        """The Review pane's per-task menu: read what was built, then land
        it or bounce it back. Landing with 'done' is the human review step
        ADR 0009 reserves for a person, not NightShift itself -- this is
        that step, always confirmed before it writes."""
        snap = self.snap
        assert snap is not None
        branch = (branch_for_task(task["id"], snap.git["branches"])
                  if snap.git else None)
        labels = ["Review implementation", "Mark task done",
                  "Send back to todo (failed review)"]
        choice = _menu(stdscr, f"{task['id']}  review", labels, self.tones)
        if choice is None:
            return
        if choice == 0:
            self.status = "fetching PR..."
            self.draw(stdscr)
            pr = probe_pr(self.repo, branch) if branch else None
            _modal(stdscr, task["id"], review_detail_lines(task, pr, branch),
                  self.tones)
            self.status = "? for help"
        elif choice == 1:
            self.mark_task_done(stdscr, task)
        else:
            self.review_failed(stdscr, task)

    def mark_task_done(self, stdscr: Any, task: Any) -> None:
        """The human-review verdict ADR 0009 reserves for a person: lands in
        the shared LANDING_BRANCH worktree, committed and pushed there, with
        that branch's PR opened or refreshed -- never this checkout's own
        tasks.toml, which reflects only what main has actually merged. A run
        of these in one session reuses the same worktree/branch/PR rather
        than paying setup cost per task.

        The git fetch/commit/push/gh-pr round trip runs in the background
        (land_task_done_async): the confirm screen is the only blocking
        step, so the operator can keep navigating immediately after
        confirming instead of the TUI freezing for the several seconds that
        round trip takes. The result lands in the status line once ready --
        see _poll_land(), called every draw()."""
        lines = ["Will run:", "",
                 f"  land {task['id']} -> done  (branch {LANDING_BRANCH})"]
        caution = ("Marks this the landed, human-reviewed version of the "
                   "task (ADR 0009: only a human review does this, never "
                   "NightShift itself). Commits and pushes tasks.toml in "
                   f"the shared {LANDING_BRANCH} worktree, opening or "
                   "refreshing its PR -- this checkout's own tasks.toml is "
                   "untouched until that PR merges.")
        lines += ["", *_wrap("CAUTION: " + caution, 74)]
        if not _confirm(stdscr, f"{task['id']} -> done", lines, self.tones,
                        "bad"):
            self.status = "cancelled"
            return
        self._enqueue_land([task["id"]], task["id"])

    def mark_all_reviewed_done(self, stdscr: Any) -> None:
        """Bulk form of mark_task_done(): lands every task currently in the
        REVIEW bucket into one LANDING_BRANCH batch, so clearing a full
        review queue is one confirmation instead of one per task. Same ADR
        0009 contract as the single-task path -- still a human confirming
        the verdict, just for all of them at once."""
        snap = self.snap
        assert snap is not None
        review_tasks = sorted(
            (t for t in snap.by_id.values() if t["status"] == "review"),
            key=lambda t: (t["phase"], t["id"]))
        if not review_tasks:
            self.status = "no tasks in review"
            return
        ids = [t["id"] for t in review_tasks]
        lines = ["Will run:", ""]
        lines += [f"  land {task_id} -> done  (branch {LANDING_BRANCH})"
                 for task_id in ids]
        caution = ("Marks all of the above the landed, human-reviewed "
                   "version of each task (ADR 0009: only a human review "
                   "does this, never NightShift itself). Commits and "
                   "pushes tasks.toml in the shared "
                   f"{LANDING_BRANCH} worktree, opening or refreshing its "
                   "PR -- this checkout's own tasks.toml is untouched "
                   "until that PR merges.")
        lines += ["", *_wrap("CAUTION: " + caution, 74)]
        if not _confirm(stdscr, f"Mark {len(ids)} task(s) done", lines,
                        self.tones, "bad"):
            self.status = "cancelled"
            return
        desc = f"{len(ids)} task(s)" if len(ids) > 1 else ids[0]
        self._enqueue_land(ids, desc)

    def review_failed(self, stdscr: Any, task: Any) -> None:
        """Collect why the review failed and send the task back to `todo`
        with that feedback in `notes`, so the next dispatch prompt carries
        it (backlog.py's build_prompt surfaces `notes` verbatim)."""
        reason = _prompt_text(stdscr, f"{task['id']}  review failed",
                              "Why did it fail review?", self.tones)
        if reason is None or not reason.strip():
            self.status = "cancelled"
            return
        changes = _prompt_text(stdscr, f"{task['id']}  review failed",
                               "What should change?", self.tones)
        if changes is None or not changes.strip():
            self.status = "cancelled"
            return
        choice = _menu(stdscr, "Interview the operator first?",
                      ["Yes -- ask clarifying questions before changing "
                       "anything",
                       "No -- the answers above are enough"], self.tones)
        if choice is None:
            self.status = "cancelled"
            return
        notes = review_feedback_notes(reason, changes, interview=(choice == 0))
        self.run(stdscr, f"{task['id']} -> todo (failed review)",
                 [Step(f"tasks.toml: {task['id']} status review -> todo",
                       edit=(task["id"], "todo"), notes=notes)],
                 caution="Rewrites this task's `notes` with the feedback "
                         "above, so the next dispatch prompt carries it.")

    def dispatch_task(self, stdscr: Any, task: Any) -> None:
        """Pick backend (folding in antigravity's model family, AG-10
        revision), subagent delegation, model, permission mode, and effort
        before dispatching one task.

        The picker opens seeded from `recommend_dispatch()` (CK-06): Backend/
        Model/Effort default to whichever of the three quotas this task's
        size points at, not always 'claude'. Model's and Permission's legal
        options depend on Backend; Effort is claude-only and absent from
        the field list otherwise. `_settings()` is handed `_dispatch_fields`
        itself as a `rebuild` callback, so highlighting a different Backend
        updates Model/Permission/Effort immediately rather than only once
        confirmed -- one continuous widget, not a round-trip per gating
        change. `size_variants` covers every Backend choice's field shape up
        front so the popup does not resize as the operator moves through it.
        """
        snap = self.snap
        assert snap is not None
        families = read_agy_families(self.repo)
        rec = recommend_dispatch(self.bl, task, families)
        seed = {"Backend": rec.backend, "Model": rec.model}
        if rec.effort is not None:
            seed["Effort"] = rec.effort
        fields, defaults = _dispatch_fields(self.bl, task, families, seed)
        size_variants = [_dispatch_fields(self.bl, task, families, {"Backend": b})[0]
                         for b in fields[0].options]
        picks = _settings(stdscr, f"Dispatch {task['id']}", fields, defaults,
                          self.tones,
                          rebuild=lambda p: _dispatch_fields(self.bl, task, families, p),
                          size_variants=size_variants)
        if picks is None:
            self.status = "cancelled"
            return

        backend = real_backend(picks["Backend"])
        argv = self.bl.build_command(task, snap.cfg, snap.layout, "fleet",
                                     subagents=picks["Subagents"],
                                     model=picks["Model"],
                                     effort=picks.get("Effort", "default"),
                                     backend=backend)
        argv = (_apply_agy_permission_mode(argv, picks["Permission"])
               if backend == "antigravity" else
               _apply_permission_mode(argv, picks["Permission"]))
        self.confirm_dispatch(stdscr, f"Dispatch {task['id']}", argv)

    def act_lane(self, stdscr: Any, lane: Any) -> None:
        choice = _menu(stdscr, f"lane {lane.index}",
                       [f"Dispatch lane {lane.index} ({lane.tasks[0]})",
                        "Show lane detail"], self.tones)
        if choice is None:
            return
        if choice == 0:
            self.confirm_dispatch(stdscr, f"Dispatch lane {lane.index}",
                                  list(lane.command))
        else:
            _modal(stdscr, f"lane {lane.index}", lane_detail_lines(lane),
                  self.tones)

    def act_agent(self, stdscr: Any, agent: Agent) -> None:
        _modal(stdscr, agent.name, agent_detail_lines(agent), self.tones)

    def act_next(self, stdscr: Any) -> None:
        snap = self.snap
        assert snap is not None
        if not snap.action.command:
            _modal(stdscr, snap.action.kind, _wrap(snap.action.reason, 76),
                   self.tones)
            return
        self.confirm_dispatch(stdscr, "Dispatch next task",
                              list(snap.action.command))

    def batch_all(self, stdscr: Any) -> None:
        snap = self.snap
        assert snap is not None
        steps: list[Step] = []
        seen: list[str] = []
        for finding in snap.findings:
            plan = plan_fix(finding, self.repo, snap.by_id)
            if plan.tier == AUTO and plan.steps:
                steps += plan.steps
                seen.append(finding.code)
        if not steps:
            self.status = "nothing auto-fixable"
            return
        self.run(stdscr, f"Apply {len(steps)} auto-fixable finding(s)", steps,
                 caution="Codes: " + ", ".join(sorted(set(seen))))

    # -- confirm + apply ----------------------------------------------
    def confirm_dispatch(self, stdscr: Any, title: str, argv: list[str]) -> None:
        shown = list(argv)
        prompt = shown.pop() if shown and "\n" in shown[-1] else ""
        lines = ["This launches a real agent session in:", f"  {self.repo}", "",
                 "Command:", *_wrap(shlex.join(shown), 74), ""]
        if prompt:
            lines += ["Prompt:", *[f"  {ln}" for ln in prompt.splitlines()], ""]
        lines.append("Output is detached to .cockpit/logs/.")
        if not _confirm(stdscr, title, lines, self.tones, "warn"):
            self.status = "cancelled"
            return
        binary = agy_bin() if argv and argv[0] == "agy" else self.binary
        res = dispatch(self.repo, argv, binary)
        self.status = res
        if res.startswith("FAIL"):
            _modal(stdscr, "DISPATCH FAILED", [res], self.tones, "bad")
        self.refresh()

    def run(self, stdscr: Any, title: str, steps: list[Step],
            caution: str = "") -> None:
        if not steps:
            self.status = "nothing to do"
            return
        lines: list[str] = ["Will run, in order:", ""]
        for step in steps:
            lines.append(f"  {step.describe}")
            if step.argv:
                lines.append(f"      $ {shlex.join(step.argv)}")
            if step.notes is not None:
                lines.append("      notes ->")
                lines += [f"    {ln}" for ln in _wrap(step.notes, 66)]
        if caution:
            lines += ["", *_wrap("CAUTION: " + caution, 74)]
        if not _confirm(stdscr, title, lines, self.tones,
                        "bad" if caution else "warn"):
            self.status = "cancelled"
            return
        results = apply_steps(self.bl, self.repo, steps, self.binary)
        failures = sum(1 for line in results if line.startswith("FAIL"))
        _modal(stdscr, "RESULT", results, self.tones,
              "bad" if failures else "good")
        self.refresh()
        self.status = (f"{len(results)} step(s), {failures} failed" if failures
                       else f"{len(results)} step(s) ok")


def _step(rows: list[Row], index: int, delta: int) -> int:
    """Move to the next/previous selectable row (payload is not None) in
    the direction of travel, skipping hint lines, headers, and separators
    that a single keypress should not land on -- e.g. a doctor finding's
    hint line, between it and the next finding. Clamps at the list ends
    when no further selectable row exists."""
    n = len(rows)
    if n == 0:
        return 0
    cursor = max(0, min(index, n - 1))
    i = cursor
    while 0 <= i + delta < n:
        i += delta
        cursor = i
        if rows[i].payload is not None:
            return i
    return cursor


def _put(stdscr: Any, row: int, col: int, text: str, attr: int = 0) -> None:
    """addstr that never raises. Writing the last cell of the last line is an
    error in curses, and a cockpit that dies on a narrow terminal is useless."""
    height, width = stdscr.getmaxyx()
    if row < 0 or row >= height or col >= width:
        return
    with contextlib.suppress(Exception):
        stdscr.addnstr(row, col, text, max(0, width - col - 1), attr)


def _status_tone(status: str) -> str:
    """Colour the bottom status bar by what it is reporting..."""
    return status_tone(status)


#: Popup border colour by purpose: a plain look-and-close view reads calmer
#: than a dialog that is about to write something.
_PanelLine = str | tuple[str, str]


def _panel(stdscr: Any, title: str, lines: Sequence[_PanelLine], footer: str,
          tones: dict[str, int] | None = None, accent: str = "info",
          right: Sequence[_PanelLine] | None = None,
          min_content_w: int = 0, min_content_h: int = 0) -> None:
    """Centred bordered popup. `right`, if given, renders as a second column
    separated by a vertical rule -- used by the settings picker to show a
    help tooltip for whatever option is currently highlighted.

    `min_content_w`/`min_content_h`, when a caller redraws the same popup
    repeatedly with content that changes size frame to frame (e.g. a
    tooltip whose text varies by highlighted option), pin the box to at
    least that size so it does not visibly resize as the user navigates --
    the caller is expected to pass the size of the largest content it will
    ever show, computed once up front.
    """
    import curses

    tones = tones or {}
    parts = [(ln, "") if isinstance(ln, str) else ln for ln in lines]
    text_lines = [text for text, _ in parts]
    right_parts = ([(ln, "") if isinstance(ln, str) else ln for ln in right]
                   if right else [])
    right_lines = [text for text, _ in right_parts]
    height, width = stdscr.getmaxyx()
    left_w = max((len(ln) for ln in text_lines), default=0)
    content_w = left_w
    if right_parts:
        content_w += 3 + max((len(ln) for ln in right_lines), default=0)
    content_w = max(content_w, min_content_w)
    box_w = min(width - 2, max(len(title) + 8, content_w or 1, len(footer)) + 4)
    box_h = min(height - 2, max(len(parts), len(right_parts), min_content_h) + 4)
    top = max(0, (height - box_h) // 2)
    left = max(0, (width - box_w) // 2)
    win = stdscr.derwin(box_h, box_w, top, left)
    win.erase()
    border = tones.get(accent, 0)
    divider = min(left_w + 3, box_w - 2) if right_parts else None
    _put(win, 0, 0, TL + H * (box_w - 2) + TR, border)
    for row in range(1, box_h - 1):
        _put(win, row, 0, V, border)
        _put(win, row, box_w - 1, V, border)
        if divider is not None:
            _put(win, row, divider, V, border)
    _put(win, box_h - 1, 0, BL + H * (box_w - 2) + BR, border)
    _put(win, 0, 2, f" {title} ", tones.get("head", 0) | curses.A_BOLD)
    for index, (line, tone) in enumerate(parts[:box_h - 4]):
        _put(win, 1 + index, 2, line, tones.get(tone, 0) if tone else 0)
    if divider is not None:
        for index, (line, tone) in enumerate(right_parts[:box_h - 4]):
            _put(win, 1 + index, divider + 2, line, tones.get(tone, 0) if tone else 0)
    _put(win, box_h - 2, 2, footer, curses.A_DIM)
    win.refresh()


def _modal(stdscr: Any, title: str, lines: Sequence[str],
          tones: dict[str, int] | None = None, accent: str = "info") -> None:
    #: apply_steps() result lines are "ok ..." / "FAIL ..." -- colour them by
    #: outcome so a scan of the popup shows what broke without reading prose.
    styled: list[_PanelLine] = [
        (line, "bad") if line.startswith("FAIL") else
        (line, "good") if line.startswith("ok") else line
        for line in lines]
    _panel(stdscr, title, styled, "any key to close", tones, accent)
    stdscr.getch()


def _confirm(stdscr: Any, title: str, lines: Sequence[str],
            tones: dict[str, int] | None = None, accent: str = "warn") -> bool:
    _panel(stdscr, title, lines, "[y] apply    [n] cancel", tones, accent)
    while True:
        key = stdscr.getch()
        if key in (ord("y"), ord("Y")):
            return True
        if key in (ord("n"), ord("N"), 27, 10, 13):
            return False


def _menu(stdscr: Any, title: str, labels: list[str],
         tones: dict[str, int] | None = None) -> int | None:
    import curses

    index = 0
    while True:
        lines: list[_PanelLine] = [
            (f"{ARROW} {label}", "busy") if i == index else f"  {label}"
            for i, label in enumerate(labels)]
        _panel(stdscr, title, lines, "enter select    esc cancel", tones, "busy")
        key = stdscr.getch()
        if key in (ord("j"), curses.KEY_DOWN):
            index = min(index + 1, len(labels) - 1)
        elif key in (ord("k"), curses.KEY_UP):
            index = max(0, index - 1)
        elif key in (10, 13, curses.KEY_ENTER):
            return index
        elif key in (27, ord("q")):
            return None


def _prompt_text(stdscr: Any, title: str, label: str,
                 tones: dict[str, int] | None = None) -> str | None:
    """Single-line free-text input: printable characters append, backspace
    deletes from the end, enter submits, esc cancels. No cursor movement --
    a short-answer form field (review feedback), not a text editor."""
    import curses

    text = ""
    while True:
        lines: list[_PanelLine] = [label, "", text + "_"]
        _panel(stdscr, title, lines, "enter submit    esc cancel", tones, "busy")
        key = stdscr.getch()
        if key in (10, 13, curses.KEY_ENTER):
            return text
        if key == 27:
            return None
        if key in (curses.KEY_BACKSPACE, 127, 8):
            text = text[:-1]
        elif 0 <= key < 256 and chr(key).isprintable():
            text += chr(key)


#: `claude --permission-mode` choices, offered by the dispatch picker.
#: Cockpit-only: backlog.py's build_command() only knows the acceptEdits /
#: --dangerously-skip-permissions binary (its own `dangerous` flag), so the
#: picked value is spliced into the argv it returns -- see
#: _apply_permission_mode() -- rather than threaded through that function.
PERMISSION_MODES = ("auto", "acceptEdits", "plan", "dontAsk",
                    "bypassPermissions", "manual")
#: One-line rationale per PERMISSION_MODES value, surfaced as a tooltip by
#: the dispatch picker, same convention as backlog.py's MODEL_HELP etc.
PERMISSION_MODE_HELP: dict[str, str] = {
    "auto": "Claude decides per tool call whether to ask. The default -- "
            "right for most dispatches.",
    "acceptEdits": "File edits are pre-approved; other tools still prompt "
                   "as usual.",
    "plan": "Research and plan only -- no edits or commands are run.",
    "dontAsk": "Skip prompts for allowed tools; anything not allowed still "
              "blocks.",
    "bypassPermissions": "Skip every permission check. Reserve for "
                         "sandboxes with no network access.",
    "manual": "Prompt for every tool call, no exceptions.",
}
#: Model picker order for the dispatch UI: cheapest/fastest to most capable
#: -- independent of backlog.py's own MODELS tuple, which lists its
#: 'sonnet' default first.
MODEL_ORDER = ("haiku", "sonnet", "opus")

#: Backend row help, cockpit-only -- backlog.py's build_command() already
#: knows both CLIs (AG-09); this is purely the picker's own tooltip.
BACKEND_HELP: dict[str, str] = {
    "claude": "Claude Code. The default -- untouched, every dispatch is "
             "exactly what it was before this row existed.",
    "antigravity": "Google's Antigravity CLI (agy), ADR 0017/0018 -- a "
                   "separate quota from Claude's own Pro window. Gemini "
                   "models (its primary family).",
    "antigravity-other": "Google's Antigravity CLI (agy), routed to its "
                         "bundled non-Gemini allotment (Claude/GPT models, "
                         "ADR 0018) -- a separate quota from both Claude's "
                         "own Pro window and Antigravity's Gemini models.",
}


def _agy_backend_labels(groups: dict[str, tuple[str, ...]]) -> dict[str, str]:
    """Menu-level Backend label -> family name, folding the old separate
    Family row into Backend itself: one fewer row to navigate, and Model
    can key off Backend alone. 'antigravity' is always the primary family
    ('gemini' when present, else alphabetically first); every other
    configured family gets its own 'antigravity-<name>' label (e.g. the
    default split's bundled-allotment family renders as
    'antigravity-other'). Empty when `groups` is empty (agy_family_groups'
    single-or-no-family case) -- the caller falls back to a bare
    'antigravity' label with the unfiltered model list, same as before this
    row existed."""
    if not groups:
        return {}
    names = sorted(groups)
    primary = "gemini" if "gemini" in groups else names[0]
    labels = {"antigravity": primary}
    labels.update({f"antigravity-{name}": name for name in names if name != primary})
    return labels

#: `agy --mode` / `--dangerously-skip-permissions` choices, offered by the
#: dispatch picker for the antigravity backend -- PERMISSION_MODES' sibling,
#: a separate vocabulary rather than a shared one (docs/antigravity-cli-
#: contract.md's captured `agy --help`: `--mode` only understands
#: accept-edits/plan; --dangerously-skip-permissions is a distinct flag).
#: Listed, and picked, first: the cockpit dispatch commits and opens its
#: own PR (ADR 0015), and this is the only one of the four whose help text
#: documents covering every tool call, commands included -- accept-edits'
#: own scope for a non-edit tool like git/gh is unverified (AG-01), and a
#: detached run that blocks on a permission prompt just hangs against
#: /dev/null forever.
AGY_PERMISSION_MODES = ("dangerously-skip-permissions", "accept-edits",
                        "plan", "default")
AGY_PERMISSION_MODE_HELP: dict[str, str] = {
    "dangerously-skip-permissions": "Auto-approve every tool call without "
        "prompting, including git and gh. The default here -- this run "
        "commits and opens its own PR, so it must not stall on a prompt "
        "nothing is attached to answer.",
    "accept-edits": "File edits are pre-approved. agy's own documentation "
        "does not say whether non-edit tools -- including git/gh -- are "
        "covered; unverified for a detached run (AG-01).",
    "plan": "Research and plan only -- no edits or commands are run.",
    "default": "No --mode flag -- agy's own baseline for this session. "
        "Undocumented outside interactive use; avoid for a detached "
        "dispatch.",
}


def _apply_permission_mode(argv: list[str], mode: str) -> list[str]:
    """Splice the picked --permission-mode value into `argv`.

    build_command() only emits acceptEdits (or, with --dangerous,
    --dangerously-skip-permissions); this rewrites the former with the
    operator's actual choice rather than adding a parameter to that
    function for a cockpit-only picker.
    """
    argv = list(argv)
    if "--permission-mode" in argv:
        argv[argv.index("--permission-mode") + 1] = mode
    return argv


def _apply_agy_permission_mode(argv: list[str], mode: str) -> list[str]:
    """Splice the picked agy permission choice into `argv`, replacing
    whatever build_command() emitted by default (`--mode accept-edits`).
    Mirrors _apply_permission_mode()'s role for the claude backend, against
    agy's own, differently-shaped vocabulary (AGY_PERMISSION_MODES).

    build_command() emits the prompt as `-p`'s own value (agy's `-p` takes
    the prompt directly, unlike claude's bare `-p` mode switch -- see its own
    docstring), so the trailing `["-p", prompt]` pair is popped and
    re-appended together here rather than just the prompt: leaving `-p`
    behind while splicing flags in between would strand it next to whatever
    flag lands last, feeding agy the wrong string as its prompt."""
    argv = list(argv)
    prompt_pair: list[str] = []
    if len(argv) >= 2 and argv[-2] == "-p" and "\n" in argv[-1]:
        prompt_pair = argv[-2:]
        argv = argv[:-2]
    if "--dangerously-skip-permissions" in argv:
        argv.remove("--dangerously-skip-permissions")
    if "--mode" in argv:
        i = argv.index("--mode")
        del argv[i:i + 2]
    if mode == "dangerously-skip-permissions":
        argv.append("--dangerously-skip-permissions")
    elif mode != "default":
        argv += ["--mode", mode]
    argv += prompt_pair
    return argv


class Setting(NamedTuple):
    label: str
    options: tuple[str, ...]
    help: dict[str, str] = {}


def real_backend(menu_backend: str) -> str:
    """The CLI backend a menu-level Backend label resolves to: 'claude'
    stays itself, every 'antigravity*' label (plain or 'antigravity-<family>')
    collapses to 'antigravity' -- the only two build_command() understands."""
    return "claude" if menu_backend == "claude" else "antigravity"


def _dispatch_fields(bl: ModuleType, task: Any, families: dict[str, str],
                     picks: dict[str, str]) -> tuple[list[Setting], list[int]]:
    """The dispatch picker's field list and default indices for whatever
    Backend `picks` has settled on so far.

    Backend's own options fold in Antigravity's model family (AG-10 revision):
    a configured multi-family antigravity shows as 'antigravity' (primary
    family) plus one 'antigravity-<name>' per other family, instead of a
    separate Family row -- one fewer row to navigate, and every other field
    keys off Backend alone. Model's option list depends on Backend;
    Permission's vocabulary depends on Backend; Effort is claude-only and
    simply absent from the returned field list otherwise. Pure and
    curses-free on purpose -- _settings() calls this again as a `rebuild`
    callback on every value change, so the Model/Permission/Effort rows
    update live as the operator moves through Backend rather than only once
    confirmed.
    """
    agy_models = bl.BACKEND_MODELS["antigravity"]
    groups = agy_family_groups(agy_models, families)
    agy_labels = _agy_backend_labels(groups)
    backends = ("claude", *agy_labels) if agy_labels else ("claude", "antigravity")

    backend = picks.get("Backend", "claude")
    if backend not in backends:
        backend = "claude"
    subagents = picks.get("Subagents")
    if subagents not in bl.SUBAGENT_MODES:
        subagents = "auto"

    backend_help = dict(BACKEND_HELP)
    for label, name in agy_labels.items():
        if label not in backend_help:
            backend_help[label] = (f"Antigravity's '{name}' family: "
                                   f"{', '.join(groups[name])}.")
    fields = [Setting("Backend", backends, backend_help),
             Setting("Subagents", bl.SUBAGENT_MODES, bl.SUBAGENT_MODE_HELP)]
    defaults = [backends.index(backend), bl.SUBAGENT_MODES.index(subagents)]

    backend_cli = real_backend(backend)
    family = agy_labels.get(backend) if agy_labels else None

    if backend_cli == "claude":
        models = tuple(m for m in MODEL_ORDER if m in bl.MODELS) or bl.MODELS
        model_help = bl.MODEL_HELP
    elif family is not None:
        models = groups[family]
        model_help = bl.AGY_MODEL_HELP
    else:
        models = bl.BACKEND_MODELS[backend_cli]
        model_help = bl.BACKEND_MODEL_HELP[backend_cli]

    if backend_cli == "claude":
        default_model = task.get("model") or bl.MODEL
    elif family == "other":
        default_model = task.get("model") or getattr(bl, "AGY_OTHER_MODEL", "claude-sonnet-4-6")
    else:
        default_model = task.get("model") or bl.AGY_MODEL
    picked_model = picks.get("Model")
    chosen_model = picked_model if picked_model in models else (
        default_model if default_model in models else models[0])
    fields.append(Setting("Model", models, model_help))
    defaults.append(models.index(chosen_model))

    perm_modes: tuple[str, ...]
    if backend_cli == "claude":
        perm_modes, perm_help = PERMISSION_MODES, PERMISSION_MODE_HELP
    else:
        perm_modes, perm_help = AGY_PERMISSION_MODES, AGY_PERMISSION_MODE_HELP
    picked_perm = picks.get("Permission")
    perm = picked_perm if picked_perm in perm_modes else perm_modes[0]
    fields.append(Setting("Permission", perm_modes, perm_help))
    defaults.append(perm_modes.index(perm))

    if backend_cli == "claude":
        picked_effort = picks.get("Effort")
        effort = picked_effort if picked_effort in bl.EFFORT_LEVELS else "default"
        fields.append(Setting("Effort", bl.EFFORT_LEVELS, bl.EFFORT_HELP))
        defaults.append(bl.EFFORT_LEVELS.index(effort))

    return fields, defaults


#: A field's option row wraps past this many columns rather than running an
#: arbitrarily long line off the edge of the popup -- antigravity's 11-slug
#: Model row is well past any reasonable terminal width joined on one line.
FIELD_OPTS_WRAP_W = 60


def _wrap_field_options(tokens: list[str], width: int) -> list[str]:
    """Wrap a field's already-bracketed option tokens across lines at
    `width` columns. Never splits a token mid-word -- each element of
    `tokens` is one whole option (bracketed or not)."""
    lines: list[str] = []
    line = ""
    for tok in tokens:
        cand = f"{line}  {tok}" if line else tok
        if line and len(cand) > width:
            lines.append(line)
            line = tok
        else:
            line = cand
    lines.append(line)
    return lines


def _field_row_lines(field: Setting, value_index: int, label_w: int,
                     active: bool) -> list[_PanelLine]:
    """Render one field as one or more panel lines: the label and marker on
    the first line, continuation lines indented under the options column
    when the option list is too wide for FIELD_OPTS_WRAP_W to hold on one
    line."""
    tokens = [f"[{opt}]" if j == value_index else opt
             for j, opt in enumerate(field.options)]
    wrapped = _wrap_field_options(tokens, FIELD_OPTS_WRAP_W)
    marker = ARROW if active else " "
    tone = "busy" if active else ""
    indent = " " * (3 + label_w)
    out: list[_PanelLine] = [(f"{marker} {field.label:<{label_w}} {wrapped[0]}", tone)]
    out += [(f"{indent}{cont}", tone) for cont in wrapped[1:]]
    return out


def _settings(stdscr: Any, title: str, fields: list[Setting],
             defaults: list[int], tones: dict[str, int] | None = None,
             rebuild: Any = None,
             size_variants: list[list[Setting]] | None = None
             ) -> dict[str, str] | None:
    """Arrow-key settings picker: up/down (or j/k) moves between fields,
    left/right cycles the selected field's value. The right-hand pane is a
    tooltip explaining whichever option is currently highlighted. Enter
    confirms with the values on screen (returned as a label -> value dict,
    since `rebuild` can change the field list's shape between keystrokes),
    esc cancels.

    `rebuild(picks) -> (fields, defaults)`, when given, is called after
    every value change with the values on screen so far: this is what makes
    a gating field (Backend, in the dispatch picker) update dependent rows
    -- Model, Permission, Effort -- live as the operator moves through it,
    rather than only once confirmed. `size_variants`, when given, is every
    field-list shape the widget can ever show (e.g. one per Backend choice);
    the popup is sized from the widest/tallest of those up front so it does
    not visibly resize as `rebuild` swaps the field list underneath it.
    Without either, this degrades to a static, single-shape widget sized
    from `fields` alone.
    """
    import curses

    variants = size_variants if size_variants is not None else [fields]
    flat = [f for group in variants for f in group]
    label_w = max((len(f.label) for f in flat), default=0)
    row_w, tip_w = 0, 0
    for field in flat:
        # Bracketing the first option never changes the wrapped width by
        # more than the bracket pair itself, so it stands in for "how wide
        # this field's row renders" regardless of current value.
        tokens = [f"[{opt}]" if j == 0 else opt
                 for j, opt in enumerate(field.options)]
        wrapped = _wrap_field_options(tokens, FIELD_OPTS_WRAP_W)
        row_w = max(row_w, len(f"{ARROW} {field.label:<{label_w}} {wrapped[0]}"),
                   *(len(f"{' ' * (3 + label_w)}{cont}") for cont in wrapped[1:]))
        for opt in field.options:
            wrapped_help = _wrap(field.help.get(opt, "(no description)"), 34)
            tip_w = max([tip_w, len(f"{field.label}: {opt}")] +
                       [len(ln) for ln in wrapped_help])
    min_h = max([len(fields)] + [
        sum(len(_wrap_field_options(
            [f"[{opt}]" if j == 0 else opt for j, opt in enumerate(f.options)],
            FIELD_OPTS_WRAP_W)) for f in group)
        for group in variants])
    min_w = row_w + 3 + tip_w

    values = list(defaults)
    row = 0
    while True:
        lines: list[_PanelLine] = []
        for i, field in enumerate(fields):
            lines += _field_row_lines(field, values[i], label_w, i == row)
        active = fields[row]
        selected = active.options[values[row]]
        tooltip: list[_PanelLine] = [(f"{active.label}: {selected}", "busy"), ""]
        tooltip += _wrap(active.help.get(selected, "(no description)"), 34)
        _panel(stdscr, title, lines,
               "left/right value    up/down field    enter dispatch    "
               "esc cancel", tones, "busy", right=tooltip,
               min_content_w=min_w, min_content_h=min_h)
        key = stdscr.getch()
        if key in (ord("j"), curses.KEY_DOWN):
            row = min(row + 1, len(fields) - 1)
        elif key in (ord("k"), curses.KEY_UP):
            row = max(0, row - 1)
        elif key in (curses.KEY_LEFT, curses.KEY_RIGHT):
            step = -1 if key == curses.KEY_LEFT else 1
            values[row] = (values[row] + step) % len(fields[row].options)
            if rebuild is not None:
                picks = {f.label: f.options[v]
                        for f, v in zip(fields, values, strict=True)}
                fields, defaults = rebuild(picks)
                values = list(defaults)
                row = min(row, len(fields) - 1)
        elif key in (10, 13, curses.KEY_ENTER):
            return {f.label: f.options[v]
                   for f, v in zip(fields, values, strict=True)}
        elif key in (27, ord("q")):
            return None


# =====================================================================
# L5  CLI
# =====================================================================


def render_summary(snap: Snapshot, agents: list[Agent],
                   daily: tuple[int, float] | None,
                   usage_mod: ModuleType | None = None,
                   repo: Path | None = None) -> str:
    """The --no-tui view: everything the panes hold, as plain text. The
    USAGE section is opt-in -- pass usage_mod/repo (main() does) to include
    it; omitted, this is identical to the pre-USAGE-pane output."""
    lines = [f"repo:    {snap.repo}",
             f"project: {snap.cfg['project'] or '(unnamed)'}"]
    if snap.errors:
        lines.append("tasks.toml INVALID:")
        lines += [f"  - {e}" for e in snap.errors]
        return "\n".join(lines)

    counts = {k: len(v) for k, v in snap.buckets.items()}
    done = sum(1 for t in snap.by_id.values() if t["status"] == "done")
    lines += [
        f"tasks:   {len(snap.by_id)}  ({done} done, {counts['ready']} ready, "
        f"{counts['in_progress']} wip, "
        f"{counts['blocked'] + counts['held']} stuck)",
        f"next:    {snap.action.kind} - {snap.action.reason}",
        "",
        f"findings: {len(snap.findings)}"]
    for finding in snap.findings:
        plan = plan_fix(finding, snap.repo, snap.by_id)
        lines.append(f"  [{plan.tier:<7}] {finding.code:<20} {finding.message}")
    lines += ["", f"agents:  {len(agents)}"]
    for agent in agents:
        detail = ""
        if agent.usage:
            use = agent.usage
            detail = (f"  {human_tokens(use.context)} ctx "
                      f"({round(100 * use.context / use.window)}%)  "
                      f"${use.cost:.2f}")
        lines.append(f"  {agent.name[:30]:<30} {agent.kind:<12} "
                     f"{agent.status:<6} {human_age(agent.age):>6}{detail}")
    if daily:
        tokens, cost = daily
        lines.append(f"today:   {human_tokens(tokens)} tokens, ${cost:.2f} "
                     "(local stats cache; 5h/weekly limits not on disk)")
    if usage_mod is not None and repo is not None:
        lines += [""] + usage_summary_lines(usage_mod, repo)
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cockpit.py",
        description="Interactive house backlog cockpit: monitor, diagnose, "
                    "repair, dispatch.",
        epilog="Reads the manifest through scripts/backlog.py and never forks "
               "it. Every write is confirmed interactively.")
    parser.add_argument("--repo", default=None, metavar="PATH",
                        help="project root (default: search upward from cwd)")
    parser.add_argument("--backlog", default=None, metavar="PATH",
                        help="path to backlog.py (default: the repo's own copy)")
    parser.add_argument("--lanes", type=int, default=None, metavar="N")
    parser.add_argument("--no-git", action="store_true", dest="no_git")
    parser.add_argument("--no-tui", action="store_true", dest="no_tui",
                        help="print a one-shot summary instead of the TUI")
    parser.add_argument("--usage", action="store_true", dest="usage",
                        help="print the usage dashboard and exit (cron/pipes)")
    parser.add_argument("--claude-bin", default=None, metavar="PATH",
                        help="claude executable (or set COCKPIT_CLAUDE_BIN)")
    parser.add_argument("--capture-usage", action="store_true",
                        dest="capture_usage",
                        help="capture each backend's 5h/weekly quota gauges "
                             "once at startup, by driving its own /usage "
                             "screen (opt-in: spawns a real session)")
    return parser


def main(argv: list[str] | None = None, out: Any = None) -> int:
    args = build_parser().parse_args(argv)
    out = out or sys.stdout
    try:
        repo = (Path(args.repo).expanduser().resolve() if args.repo
                else find_repo(Path.cwd()))
        if repo is None:
            raise CockpitError(
                f"no tasks.toml or .git above {Path.cwd()}. Pass --repo PATH.")
        if not repo.is_dir():
            raise CockpitError(f"{repo} is not a directory")
        bl = load_backlog(repo, args.backlog)
    except CockpitError as exc:
        print(f"cockpit: {exc}", file=sys.stderr)
        return 2

    if args.usage:
        print("\n".join(usage_summary_lines(load_usage(repo), repo)), file=out)
        return 0

    if args.no_tui:
        snap = snapshot(bl, repo, args.lanes, not args.no_git)
        agents = probe_agents(repo, claude_bin(args.claude_bin))
        print(render_summary(snap, agents, daily_usage(),
                             load_usage(repo), repo), file=out)
        return 1 if snap.errors else 0

    if not sys.stdout.isatty():
        print("cockpit: stdout is not a terminal; use --no-tui.",
              file=sys.stderr)
        return 2
    try:
        return run_tui(bl, repo, args)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
