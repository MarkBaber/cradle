#!/usr/bin/env python3
"""Usage collection: turns, sessions and days from Claude Code transcripts.

cockpit.py's L1 already reads the newest usage record of *one* transcript
(read_usage) and today's totals from ~/.claude/stats-cache.json (daily_usage),
but nothing aggregates across transcripts. This module owns that: it streams
every assistant turn out of every ~/.claude/projects/<slug>/*.jsonl file into
a Turn, and folds Turns into Session and Day rollups.

stats-cache.json is read here too, but as a second, clearly separate source
(read_stats_cache / StatsCacheTotals): it is recomputed on a schedule on the
machine and lags the live transcripts. Its numbers must never be summed into
the transcript-derived Turn/Session/Day totals -- keep the two apart.

This module is also the single owner of the advisory price and
context-window tables that cockpit.py currently keeps its own copies of
(MODEL_PRICES, CACHE_READ_RATE, CACHE_WRITE_RATE, CONTEXT_WINDOWS). A later
task points cockpit.py's copies at these instead of forking them.

Stdlib only. Every function here is failure tolerant in cockpit's L1 sense:
a missing directory, an unreadable file, a truncated final line or a missing
stats cache each yield fewer records, never an exception and never a warning
about themselves.

This module also computes the characteristic breakdowns an operator can act
on (compute_breakdown) and the advisory suggestions derived from them
(generate_usage_suggestions). Suggestions are text only -- nothing here
writes, dispatches or edits anything.

L4 owns the one piece of state this module actually persists: daily
Snapshots appended to .cockpit/usage/history.jsonl (the same .cockpit/
directory cockpit.py already writes dispatch logs to), because
stats-cache.json is recomputed on a schedule and transcripts get pruned --
without a durable snapshot, trend history would have nowhere to come from.
load_history() and the trend functions built on it (moving_average,
week_over_week_delta, flag_anomalies) take history as a plain argument and
read no wall clock, so a fixed history list always produces the same
output.

Layered L0..L5, banner-delimited, one-directional, mirroring cockpit.py's
own convention: a function may reference only names at its own layer or
above.

L5 is the opt-in exception to the "never touches a live session" rule
above: capture_usage_gauge() spawns a real `claude` session under a pty to
screen-scrape the subscription 5h/weekly limit gauges, which have no
on-disk source, and capture_agy_usage_gauges() does the same for
`agy`/Antigravity. Neither is ever called automatically -- no timer path,
no implicit wiring from the layers below -- callers must pass capture=True.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import pty
import re
import select
import shutil
import signal
import statistics
import struct
import subprocess
import tempfile
import termios
import time
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any, NamedTuple

# =====================================================================
# L0  CONTRACT  --  record shapes and the advisory price/context tables.
# =====================================================================

#: tool_use block names that stand for a Skill invocation.
SKILL_TOOL_NAMES = frozenset({"Skill"})

#: tool_use block names that stand for a subagent-launching call.
SUBAGENT_TOOL_NAMES = frozenset({"Task", "Agent"})

#: USD per million tokens, (input, output). Advisory only -- this is a local
#: estimate for operator triage, not a billing record. Cache reads bill at
#: ~0.1x input and 5-minute cache writes at ~1.25x.
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
CACHE_READ_RATE = 0.1
CACHE_WRITE_RATE = 1.25
#: Context window per model. Anything unlisted falls back to DEFAULT_WINDOW.
CONTEXT_WINDOWS: dict[str, int] = {"claude-haiku-4-5": 200_000}
DEFAULT_WINDOW = 1_000_000


class Turn(NamedTuple):
    """One assistant turn parsed out of a transcript .jsonl line."""

    model: str
    timestamp: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cache_creation_1h_tokens: int
    cache_creation_5m_tokens: int
    web_search_requests: int
    web_fetch_requests: int
    duration_ms: int
    session_id: str
    git_branch: str
    cwd: str
    is_sidechain: bool
    tool_names: tuple[str, ...]


class Session(NamedTuple):
    """Turns folded by sessionId."""

    session_id: str
    git_branch: str
    cwd: str
    turn_count: int
    sidechain_turn_count: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    tool_calls: int
    skill_calls: int
    subagent_calls: int
    web_search_requests: int
    web_fetch_requests: int
    started: str
    ended: str


class Day(NamedTuple):
    """Turns folded by the date component of their timestamp."""

    date: str
    turn_count: int
    session_count: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    tool_calls: int
    skill_calls: int
    subagent_calls: int
    web_search_requests: int
    web_fetch_requests: int


class StatsCacheTotals(NamedTuple):
    """~/.claude/stats-cache.json, verbatim-ish -- a second, separate source.

    Recomputed on a schedule on the machine and lags the transcripts. Never
    fold these numbers into Turn/Session/Day: they are not the same
    measurement, just adjacent ones.
    """

    daily_model_tokens: dict[str, dict[str, int]]
    daily_activity: list[Any]
    model_usage: dict[str, dict[str, Any]]
    hour_counts: dict[str, int]


# =====================================================================
# L1  COLLECTION  --  turning files on disk into Turns and StatsCacheTotals.
#     Every reader here is failure tolerant: an unavailable or malformed
#     source yields fewer records, never a traceback and never a warning
#     about itself.
# =====================================================================

def collect_turns(claude_home: Path) -> list[Turn]:
    """Every assistant Turn across every ~/.claude/projects/<slug>/*.jsonl."""
    turns: list[Turn] = []
    try:
        paths = sorted((claude_home / "projects").glob("*/*.jsonl"))
    except OSError:
        return turns
    for path in paths:
        turns.extend(_read_turns(path))
    return turns


def _read_turns(path: Path) -> list[Turn]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    turns: list[Turn] = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except ValueError:
            continue
        turn = _parse_turn(record)
        if turn is not None:
            turns.append(turn)
    return turns


def _parse_turn(record: Any) -> Turn | None:
    if not isinstance(record, dict) or record.get("type") != "assistant":
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    used = message.get("usage")
    if not isinstance(used, dict):
        return None

    cache_creation = used.get("cache_creation")
    if not isinstance(cache_creation, dict):
        cache_creation = {}
    server_tool_use = used.get("server_tool_use")
    if not isinstance(server_tool_use, dict):
        server_tool_use = {}

    content = message.get("content")
    tool_names: tuple[str, ...] = ()
    if isinstance(content, list):
        tool_names = tuple(
            str(block["name"])
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "tool_use"
            and "name" in block)

    return Turn(
        model=str(message.get("model") or "unknown"),
        timestamp=str(record.get("timestamp") or ""),
        input_tokens=int(used.get("input_tokens") or 0),
        output_tokens=int(used.get("output_tokens") or 0),
        cache_read_tokens=int(used.get("cache_read_input_tokens") or 0),
        cache_creation_tokens=int(used.get("cache_creation_input_tokens") or 0),
        cache_creation_1h_tokens=int(
            cache_creation.get("ephemeral_1h_input_tokens") or 0),
        cache_creation_5m_tokens=int(
            cache_creation.get("ephemeral_5m_input_tokens") or 0),
        web_search_requests=int(server_tool_use.get("web_search_requests") or 0),
        web_fetch_requests=int(server_tool_use.get("web_fetch_requests") or 0),
        duration_ms=int(record.get("durationMs") or 0),
        session_id=str(record.get("sessionId") or ""),
        git_branch=str(record.get("gitBranch") or ""),
        cwd=str(record.get("cwd") or ""),
        is_sidechain=bool(record.get("isSidechain") or False),
        tool_names=tool_names)


def read_stats_cache(claude_home: Path) -> StatsCacheTotals | None:
    """~/.claude/stats-cache.json, or None if absent/unreadable/malformed."""
    path = claude_home / "stats-cache.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    daily_model_tokens: dict[str, dict[str, int]] = {}
    for entry in data.get("dailyModelTokens") or []:
        if not isinstance(entry, dict):
            continue
        date = entry.get("date")
        by_model = entry.get("tokensByModel")
        if isinstance(date, str) and isinstance(by_model, dict):
            daily_model_tokens[date] = {
                str(model): int(count or 0) for model, count in by_model.items()}

    model_usage_raw = data.get("modelUsage")
    model_usage = model_usage_raw if isinstance(model_usage_raw, dict) else {}
    daily_activity_raw = data.get("dailyActivity")
    daily_activity = daily_activity_raw if isinstance(daily_activity_raw, list) else []
    hour_counts_raw = data.get("hourCounts")
    hour_counts = hour_counts_raw if isinstance(hour_counts_raw, dict) else {}

    return StatsCacheTotals(
        daily_model_tokens=daily_model_tokens,
        daily_activity=daily_activity,
        model_usage=model_usage,
        hour_counts=hour_counts)


# =====================================================================
# L2  ROLLUPS  --  folding Turns into Session and Day, and reading the
#     tool_use block names that are the only source of tool/Skill/subagent
#     counts.
# =====================================================================

def tool_call_counts(turn: Turn) -> Counter[str]:
    """Every tool_use block name on this turn, counted -- Skill and
    subagent-launching names included."""
    return Counter(turn.tool_names)


def skill_call_count(turn: Turn) -> int:
    return sum(1 for name in turn.tool_names if name in SKILL_TOOL_NAMES)


def subagent_call_count(turn: Turn) -> int:
    return sum(1 for name in turn.tool_names if name in SUBAGENT_TOOL_NAMES)


def fold_sessions(turns: Iterable[Turn]) -> dict[str, Session]:
    """Turns folded by sessionId."""
    by_session: dict[str, list[Turn]] = defaultdict(list)
    for turn in turns:
        by_session[turn.session_id].append(turn)

    sessions: dict[str, Session] = {}
    for session_id, group in by_session.items():
        timestamps = sorted(t.timestamp for t in group if t.timestamp)
        sessions[session_id] = Session(
            session_id=session_id,
            git_branch=group[-1].git_branch,
            cwd=group[-1].cwd,
            turn_count=len(group),
            sidechain_turn_count=sum(1 for t in group if t.is_sidechain),
            input_tokens=sum(t.input_tokens for t in group),
            output_tokens=sum(t.output_tokens for t in group),
            cache_read_tokens=sum(t.cache_read_tokens for t in group),
            cache_creation_tokens=sum(t.cache_creation_tokens for t in group),
            tool_calls=sum(len(t.tool_names) for t in group),
            skill_calls=sum(skill_call_count(t) for t in group),
            subagent_calls=sum(subagent_call_count(t) for t in group),
            web_search_requests=sum(t.web_search_requests for t in group),
            web_fetch_requests=sum(t.web_fetch_requests for t in group),
            started=timestamps[0] if timestamps else "",
            ended=timestamps[-1] if timestamps else "")
    return sessions


def fold_days(turns: Iterable[Turn]) -> dict[str, Day]:
    """Turns folded by the date component (first 10 chars) of their
    timestamp."""
    turns = list(turns)
    by_date: dict[str, list[Turn]] = defaultdict(list)
    sessions_by_date: dict[str, set[str]] = defaultdict(set)
    for turn in turns:
        date = turn.timestamp[:10] if turn.timestamp else "unknown"
        by_date[date].append(turn)
        sessions_by_date[date].add(turn.session_id)

    days: dict[str, Day] = {}
    for date, group in by_date.items():
        days[date] = Day(
            date=date,
            turn_count=len(group),
            session_count=len(sessions_by_date[date]),
            input_tokens=sum(t.input_tokens for t in group),
            output_tokens=sum(t.output_tokens for t in group),
            cache_read_tokens=sum(t.cache_read_tokens for t in group),
            cache_creation_tokens=sum(t.cache_creation_tokens for t in group),
            tool_calls=sum(len(t.tool_names) for t in group),
            skill_calls=sum(skill_call_count(t) for t in group),
            subagent_calls=sum(subagent_call_count(t) for t in group),
            web_search_requests=sum(t.web_search_requests for t in group),
            web_fetch_requests=sum(t.web_fetch_requests for t in group))
    return days


def fold_days_by_model(turns: Iterable[Turn]) -> dict[tuple[str, str], int]:
    """Total tokens folded by (date, model) -- cockpit's ANALYTICS pane
    model x day matrix. Same date-extraction rule as fold_days (first 10
    characters of the turn's timestamp). Computed live off `turns` rather
    than persisted history.jsonl Snapshots: Snapshot only carries a single
    total_tokens figure per (date, scope), with no model dimension, and
    extending it for one caller isn't worth the format churn while nothing
    yet calls write_snapshot in production (see Snapshot's docstring)."""
    totals: dict[tuple[str, str], int] = defaultdict(int)
    for turn in turns:
        date = turn.timestamp[:10] if turn.timestamp else "unknown"
        totals[(date, turn.model)] += (
            turn.input_tokens + turn.output_tokens
            + turn.cache_read_tokens + turn.cache_creation_tokens)
    return dict(totals)


# =====================================================================
# L3  ANALYTICS  --  breakdowns an operator can act on, and the advisory
#     suggestions derived from them. Suggestions are text only: nothing
#     here writes, dispatches or edits anything.
# =====================================================================

#: A turn's context (input + cache_read + cache_creation, the same
#: definition as cockpit.Usage.context) above this many tokens counts as
#: "high-context" for the high_context_token_share breakdown.
HIGH_CONTEXT_TOKENS = 100_000

#: Windows with fewer turns than this produce zero suggestions -- there is
#: not enough signal in a near-empty window to advise on.
MIN_SUGGESTION_TURNS = 5

#: Percent (0-100). cache_hit_rate below this fires a suggestion.
LOW_CACHE_HIT_RATE_THRESHOLD = 50.0
#: Percent. high_context_token_share above this fires a suggestion.
HIGH_CONTEXT_SHARE_THRESHOLD = 25.0
#: Percent. sidechain_token_share above this fires a suggestion.
HIGH_SIDECHAIN_TOKEN_SHARE_THRESHOLD = 50.0
#: Percent. Any single model's cost share above this fires a suggestion.
DOMINANT_MODEL_COST_SHARE_THRESHOLD = 80.0


class Breakdown(NamedTuple):
    """Characteristic breakdowns over a window of Turns."""

    turn_count: int
    total_tokens: int
    total_cost: float
    model_token_share: dict[str, float]
    model_cost_share: dict[str, float]
    cache_hit_rate: float
    high_context_token_share: float
    sidechain_turn_share: float
    sidechain_token_share: float
    tool_call_share: dict[str, float]
    web_search_requests: int
    web_fetch_requests: int


class Suggestion(NamedTuple):
    """One piece of advisory text, plus the measurement and named
    threshold constant that fired it -- never an action."""

    text: str
    measurement: str
    threshold_name: str
    threshold_value: float
    measured_value: float


def _turn_tokens(turn: Turn) -> int:
    return (turn.input_tokens + turn.output_tokens
            + turn.cache_read_tokens + turn.cache_creation_tokens)


def _turn_context(turn: Turn) -> int:
    """Same definition as cockpit.Usage.context: input + cache_read +
    cache_creation. output_tokens is deliberately excluded."""
    return turn.input_tokens + turn.cache_read_tokens + turn.cache_creation_tokens


def _turn_cost(turn: Turn) -> float:
    """Same formula as cockpit.Usage.cost. Advisory only."""
    rate = MODEL_PRICES.get(turn.model)
    if rate is None:
        return 0.0
    inp, out = rate
    return (turn.input_tokens * inp
            + turn.output_tokens * out
            + turn.cache_read_tokens * inp * CACHE_READ_RATE
            + turn.cache_creation_tokens * inp * CACHE_WRITE_RATE) / 1_000_000


def _percent_shares(totals: dict[str, float], grand_total: float) -> dict[str, float]:
    if grand_total <= 0:
        return {}
    return {key: 100 * value / grand_total for key, value in totals.items()}


def compute_breakdown(turns: Iterable[Turn]) -> Breakdown:
    """Token/cost/cache/context/sidechain/tool breakdowns over a window of
    Turns. Never raises on an empty window."""
    turns = list(turns)
    total_tokens = sum(_turn_tokens(t) for t in turns)
    total_cost = sum(_turn_cost(t) for t in turns)

    tokens_by_model: dict[str, float] = defaultdict(float)
    cost_by_model: dict[str, float] = defaultdict(float)
    tool_calls: Counter[str] = Counter()
    total_cache_read = total_cache_creation = total_input = 0
    high_context_tokens = 0
    sidechain_turns = sidechain_tokens = 0

    for turn in turns:
        tokens = _turn_tokens(turn)
        tokens_by_model[turn.model] += tokens
        cost_by_model[turn.model] += _turn_cost(turn)
        tool_calls.update(turn.tool_names)
        total_cache_read += turn.cache_read_tokens
        total_cache_creation += turn.cache_creation_tokens
        total_input += turn.input_tokens
        if _turn_context(turn) > HIGH_CONTEXT_TOKENS:
            high_context_tokens += tokens
        if turn.is_sidechain:
            sidechain_turns += 1
            sidechain_tokens += tokens

    cache_denominator = total_cache_read + total_cache_creation + total_input
    cache_hit_rate = (
        100 * total_cache_read / cache_denominator if cache_denominator else 0.0)

    return Breakdown(
        turn_count=len(turns),
        total_tokens=total_tokens,
        total_cost=total_cost,
        model_token_share=_percent_shares(dict(tokens_by_model), total_tokens),
        model_cost_share=_percent_shares(dict(cost_by_model), total_cost),
        cache_hit_rate=cache_hit_rate,
        high_context_token_share=(
            100 * high_context_tokens / total_tokens if total_tokens else 0.0),
        sidechain_turn_share=(
            100 * sidechain_turns / len(turns) if turns else 0.0),
        sidechain_token_share=(
            100 * sidechain_tokens / total_tokens if total_tokens else 0.0),
        tool_call_share=_percent_shares(
            dict(tool_calls), sum(tool_calls.values())),
        web_search_requests=sum(t.web_search_requests for t in turns),
        web_fetch_requests=sum(t.web_fetch_requests for t in turns))


def generate_usage_suggestions(breakdown: Breakdown) -> list[Suggestion]:
    """Advisory text derived only from a Breakdown's numbers. Never fires
    on an empty or near-empty window, and never writes, dispatches or
    edits anything -- text only."""
    if breakdown.turn_count < MIN_SUGGESTION_TURNS:
        return []

    suggestions: list[Suggestion] = []

    if breakdown.cache_hit_rate < LOW_CACHE_HIT_RATE_THRESHOLD:
        suggestions.append(Suggestion(
            text=(
                f"cache_hit_rate is {breakdown.cache_hit_rate:.1f}%, below "
                f"the LOW_CACHE_HIT_RATE_THRESHOLD of "
                f"{LOW_CACHE_HIT_RATE_THRESHOLD:.0f}% -- consider reusing "
                f"sessions or context so more input is served from cache."),
            measurement="cache_hit_rate",
            threshold_name="LOW_CACHE_HIT_RATE_THRESHOLD",
            threshold_value=LOW_CACHE_HIT_RATE_THRESHOLD,
            measured_value=breakdown.cache_hit_rate))

    if breakdown.high_context_token_share > HIGH_CONTEXT_SHARE_THRESHOLD:
        suggestions.append(Suggestion(
            text=(
                f"high_context_token_share is "
                f"{breakdown.high_context_token_share:.1f}%, above the "
                f"HIGH_CONTEXT_SHARE_THRESHOLD of "
                f"{HIGH_CONTEXT_SHARE_THRESHOLD:.0f}% -- consider trimming "
                f"context or splitting turns that cross "
                f"{HIGH_CONTEXT_TOKENS:,} tokens of context."),
            measurement="high_context_token_share",
            threshold_name="HIGH_CONTEXT_SHARE_THRESHOLD",
            threshold_value=HIGH_CONTEXT_SHARE_THRESHOLD,
            measured_value=breakdown.high_context_token_share))

    if breakdown.sidechain_token_share > HIGH_SIDECHAIN_TOKEN_SHARE_THRESHOLD:
        suggestions.append(Suggestion(
            text=(
                f"sidechain_token_share is "
                f"{breakdown.sidechain_token_share:.1f}%, above the "
                f"HIGH_SIDECHAIN_TOKEN_SHARE_THRESHOLD of "
                f"{HIGH_SIDECHAIN_TOKEN_SHARE_THRESHOLD:.0f}% -- consider "
                f"whether subagent delegation is proportionate to the "
                f"work."),
            measurement="sidechain_token_share",
            threshold_name="HIGH_SIDECHAIN_TOKEN_SHARE_THRESHOLD",
            threshold_value=HIGH_SIDECHAIN_TOKEN_SHARE_THRESHOLD,
            measured_value=breakdown.sidechain_token_share))

    for model, share in sorted(breakdown.model_cost_share.items()):
        if share > DOMINANT_MODEL_COST_SHARE_THRESHOLD:
            suggestions.append(Suggestion(
                text=(
                    f"model_cost_share:{model} is {share:.1f}%, above the "
                    f"DOMINANT_MODEL_COST_SHARE_THRESHOLD of "
                    f"{DOMINANT_MODEL_COST_SHARE_THRESHOLD:.0f}% -- "
                    f"consider whether a cheaper model would work for part "
                    f"of this load."),
                measurement=f"model_cost_share:{model}",
                threshold_name="DOMINANT_MODEL_COST_SHARE_THRESHOLD",
                threshold_value=DOMINANT_MODEL_COST_SHARE_THRESHOLD,
                measured_value=share))

    return suggestions


# =====================================================================
# L4  HISTORY  --  daily Snapshots persisted to .cockpit/usage/history.jsonl,
#     and the trend functions (moving average, week-over-week delta,
#     anomaly flagging) computed over a loaded history. Every trend function
#     here takes `history` as a plain argument and reads no wall clock: a
#     fixed history list always produces the same output.
# =====================================================================

#: How many trailing same-scope snapshots flag_anomalies' median is taken
#: over.
ANOMALY_WINDOW = 7
#: A snapshot counts as anomalous once its total_tokens exceeds this many
#: times the trailing median -- see flag_anomalies' docstring for the rule.
ANOMALY_MEDIAN_MULTIPLE = 3.0


class Snapshot(NamedTuple):
    """One day's usage totals for one scope, as persisted to
    history.jsonl. Keyed by (date, scope): write_snapshot replaces any
    existing entry with the same key rather than duplicating it."""

    date: str
    scope: str
    total_tokens: int
    total_cost: float


class WeekDelta(NamedTuple):
    """this-week-minus-last-week, in the same units as Snapshot."""

    tokens_delta: float
    cost_delta: float


def history_path(repo: Path) -> Path:
    """.cockpit/usage/history.jsonl under `repo` -- the same .cockpit/
    directory cockpit.py already writes dispatch logs to."""
    return repo / ".cockpit" / "usage" / "history.jsonl"


def _parse_snapshot(record: Any) -> Snapshot | None:
    if not isinstance(record, dict):
        return None
    date = record.get("date")
    scope = record.get("scope")
    if not isinstance(date, str) or not isinstance(scope, str):
        return None
    try:
        total_tokens = int(record["total_tokens"])
        total_cost = float(record["total_cost"])
    except (KeyError, TypeError, ValueError):
        return None
    return Snapshot(date=date, scope=scope,
                     total_tokens=total_tokens, total_cost=total_cost)


def _read_history(path: Path) -> list[Snapshot]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    snapshots: list[Snapshot] = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except ValueError:
            continue
        snapshot = _parse_snapshot(record)
        if snapshot is not None:
            snapshots.append(snapshot)
    snapshots.sort(key=lambda s: (s.date, s.scope))
    return snapshots


def load_history(repo: Path) -> list[Snapshot]:
    """Every Snapshot in .cockpit/usage/history.jsonl, oldest-first. A line
    that isn't valid JSON, isn't a JSON object, or is missing/misshapen one
    of the four Snapshot fields is skipped -- it never fails the whole
    read. [] if the file doesn't exist."""
    return _read_history(history_path(repo))


def write_snapshot(repo: Path, snapshot: Snapshot) -> None:
    """Upsert `snapshot` into .cockpit/usage/history.jsonl, keyed by
    (date, scope): an existing line with the same key is replaced, not
    duplicated, so re-running on the same day for the same scope doesn't
    grow the file.

    Write-then-rename: the full (deduplicated) history is written to a
    temp file in the same directory and then os.replace()d over the
    target, so a crash mid-write leaves the previous history.jsonl intact
    rather than a truncated one.
    """
    path = history_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    kept = [s for s in _read_history(path)
            if (s.date, s.scope) != (snapshot.date, snapshot.scope)]
    kept.append(snapshot)
    kept.sort(key=lambda s: (s.date, s.scope))
    body = "".join(json.dumps(s._asdict()) + "\n" for s in kept)

    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=".history-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def moving_average(history: list[Snapshot], scope: str,
                    days: int) -> tuple[float, float]:
    """Mean (total_tokens, total_cost) over the trailing `days` Snapshots
    matching `scope`, where `history` is assumed oldest-first (as
    load_history returns it) and "trailing" means the last `days` matching
    entries -- not the last `days` calendar days. (0.0, 0.0) if nothing
    matches `scope`."""
    window = [s for s in history if s.scope == scope][-days:]
    if not window:
        return (0.0, 0.0)
    return (
        sum(s.total_tokens for s in window) / len(window),
        sum(s.total_cost for s in window) / len(window))


def week_over_week_delta(history: list[Snapshot], scope: str) -> WeekDelta:
    """Sum of the trailing 7 same-scope Snapshots minus the sum of the 7
    same-scope Snapshots before that (both counted from the end of the
    scope-filtered, oldest-first list). WeekDelta(0.0, 0.0) if there are
    fewer than 14 matching snapshots."""
    matching = [s for s in history if s.scope == scope]
    if len(matching) < 14:
        return WeekDelta(0.0, 0.0)
    this_week, last_week = matching[-7:], matching[-14:-7]
    return WeekDelta(
        tokens_delta=(sum(s.total_tokens for s in this_week)
                      - sum(s.total_tokens for s in last_week)),
        cost_delta=(sum(s.total_cost for s in this_week)
                    - sum(s.total_cost for s in last_week)))


def flag_anomalies(history: list[Snapshot], scope: str,
                    window: int = ANOMALY_WINDOW,
                    multiple: float = ANOMALY_MEDIAN_MULTIPLE
                    ) -> list[Snapshot]:
    """Same-scope Snapshots whose total_tokens exceeds `multiple` times the
    median total_tokens of the `window` same-scope snapshots immediately
    before them (chronologically, in the oldest-first, scope-filtered
    list).

    Rule: trailing-median multiple, not stddev -- daily token usage is
    heavy-tailed (one big dispatch day dwarfs the rest), and a median-based
    threshold isn't dragged toward the very spikes it's trying to flag the
    way a mean+stddev rule would be. A snapshot with fewer than `window`
    prior same-scope snapshots is never flagged: there isn't enough
    trailing history yet to judge it against.
    """
    matching = [s for s in history if s.scope == scope]
    flagged: list[Snapshot] = []
    for i, snap in enumerate(matching):
        if i < window:
            continue
        trailing_median = statistics.median(
            s.total_tokens for s in matching[i - window:i])
        if trailing_median > 0 and snap.total_tokens > multiple * trailing_median:
            flagged.append(snap)
    return flagged


# =====================================================================
# L5  CAPTURE  --  opt-in /usage screen-scrape for the subscription 5h and
#     weekly limit gauges, which have no on-disk source (see cockpit.py's
#     usage-pane fallback line, below). Nothing in this module calls
#     capture_usage_gauge() on its own -- there is no timer path -- callers
#     must pass capture=True, and each call spawns a real `claude` session
#     under a pty and consumes real quota.
#
#     Screen-scraping an interactive TUI is expected to be the least
#     durable code in this family: if the layout drifts enough that
#     parsing regularly fails, the intended response is to delete this
#     capture path, not extend the parser -- callers already degrade to
#     NOT_ON_DISK_LINE on any parse failure.
# =====================================================================

#: Mirrors the honest fallback line cockpit.py already renders when the
#: gauge isn't available -- kept here so a failed capture degrades to the
#: exact same wording rather than a second, drifting copy.
NOT_ON_DISK_LINE = (
    "Subscription 5h/weekly limits are not exposed on disk; "
    "run /usage in a session.")

#: Overall wall-clock budget for one capture_usage_gauge() call: waiting
#: for the initial screen plus waiting for the /usage response.
_DEFAULT_CAPTURE_TIMEOUT = 20.0

#: How long the pty must go quiet (no new bytes) before a screen is
#: considered settled.
_SETTLE_QUIET_PERIOD = 0.4

#: agy's screen costs more to settle than claude's: it animates its bars on
#: entry and then repaints the "Refreshes in" countdown in place, so it
#: needs both a longer quiet period and a longer overall budget.
_AGY_QUIET_PERIOD = 1.5
_AGY_CAPTURE_TIMEOUT = 60.0

#: A 0x0 pty makes agy's TUI draw nothing at all, so the capture sets a real
#: window size before spawning it.
_AGY_ROWS, _AGY_COLS = 45, 110

#: A first run in a directory agy has not seen opens a trust prompt that
#: swallows the first /usage; one retry clears it.
_AGY_USAGE_ATTEMPTS = 2

_DECRQM_QUERY_RE = re.compile(rb"\x1b\[\?(\d+)\$p")
_KITTY_QUERY_RE = re.compile(rb"\x1b\[\?u")
_DA1_QUERY_RE = re.compile(rb"\x1b\[(?:0)?c")
_CPR_QUERY_RE = re.compile(rb"\x1b\[6n")

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*(?:\x07|\x1b\\)|\r")
_SESSION_LIMIT_RE = re.compile(
    r"current session\D{0,40}?(\d{1,3})%", re.IGNORECASE)
_WEEK_LIMIT_RE = re.compile(
    r"current week\D{0,40}?(\d{1,3})%", re.IGNORECASE)


class LimitGauge(NamedTuple):
    session_pct: int
    week_pct: int


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences (and bare carriage returns) from a
    captured terminal screen."""
    return _ANSI_RE.sub("", text)


def parse_usage_screen(screen: str) -> LimitGauge | None:
    """Match on labels plus a trailing NN% so minor layout drift degrades
    rather than misreads. Returns None -- never a guess -- if either limit
    line can't be confidently found or a captured percentage is out of
    range."""
    session_match = _SESSION_LIMIT_RE.search(screen)
    week_match = _WEEK_LIMIT_RE.search(screen)
    if session_match is None or week_match is None:
        return None
    session_pct = int(session_match.group(1))
    week_pct = int(week_match.group(1))
    if not (0 <= session_pct <= 100 and 0 <= week_pct <= 100):
        return None
    return LimitGauge(session_pct=session_pct, week_pct=week_pct)


#: agy's /usage screen is N named groups rather than claude's single
#: ungrouped pair, so a flat LimitGauge cannot hold it (ADR 0018: per-family
#: quota is never folded into one Antigravity number).
#:
#: SIGN, stated once for both backends: a GroupGauge holds the percentage
#: REMAINING, because that is agy's own framing -- its screen prints
#: "49.15%" under a bar and captions it "49% remaining". UM-05's LimitGauge
#: holds the opposite (percentages USED, as `claude` frames them) and is
#: left exactly as it was; converting between the two is the renderer's
#: job, not the parser's.
class GroupGauge(NamedTuple):
    weekly_remaining_pct: int
    five_hour_remaining_pct: int


_AGY_GROUP_MARKER = re.compile(r"Models within this group:[ \t]*([^\n]*)")
_AGY_LIMIT_HEADER = re.compile(
    r"^[ \t]*(Weekly|Five Hour) Limit[ \t]*$", re.MULTILINE)
_AGY_REMAINING_RE = re.compile(r"(\d{1,3})%\s*remaining", re.IGNORECASE)


def _agy_group_family(models_line: str) -> str | None:
    """Map one group's member list ("Gemini Flash, Gemini Pro") onto the
    family vocabulary cockpit.agy_family_groups() already uses for the
    dispatch picker -- 'gemini' vs 'other' by default (ADR 0018) -- so the
    usage pane's group headers and the picker's never drift apart. Mirrors
    cockpit._agy_family()'s rule against agy's display names rather than
    importing it: usage.py imports nothing from cockpit.py."""
    names = [name.strip().lower() for name in models_line.split(",")]
    names = [name for name in names if name]
    if not names:
        return None
    if any(name.startswith("gemini") for name in names):
        return "gemini"
    if any(name.startswith(("claude", "gpt")) for name in names):
        return "other"
    return names[0].split()[0]


def _agy_limit_sections(block: str) -> dict[str, str]:
    """Split one group's block into its named limit sections, so a limit
    whose own percentage is unreadable can never silently borrow the
    neighbouring limit's number."""
    headers = list(_AGY_LIMIT_HEADER.finditer(block))
    sections: dict[str, str] = {}
    for i, header in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(block)
        sections[header.group(1).lower()] = block[header.end():end]
    return sections


def _agy_remaining_pct(section: str | None) -> int | None:
    if section is None:
        return None
    match = _AGY_REMAINING_RE.search(section)
    if match is None:
        return None
    pct = int(match.group(1))
    return pct if 0 <= pct <= 100 else None


def parse_agy_usage_screen(screen: str) -> dict[str, GroupGauge]:
    """Parse agy's grouped /usage screen into {family: GroupGauge} of
    REMAINING percentages (see GroupGauge's note on the sign).

    Anchors on the "NN% remaining" caption rather than the bar's own
    float, so the sign is read from the screen instead of assumed. A group
    is included only when both its Weekly and Five Hour limits parse; one
    that doesn't is dropped on its own and never suppresses the groups that
    did -- callers fall back per group. Returns {} for a screen with no
    recognisable groups at all, never a guess.

    A pty capture is a stream of repaint frames, not a screen: agy animates
    its bars and ticks the "Refreshes in" countdown in place. Groups are
    read in order and a later frame's values replace an earlier frame's, so
    the settled numbers win."""
    markers = list(_AGY_GROUP_MARKER.finditer(screen))
    gauges: dict[str, GroupGauge] = {}
    for i, marker in enumerate(markers):
        end = markers[i + 1].start() if i + 1 < len(markers) else len(screen)
        family = _agy_group_family(marker.group(1))
        if family is None:
            continue
        sections = _agy_limit_sections(screen[marker.end():end])
        weekly = _agy_remaining_pct(sections.get("weekly"))
        five_hour = _agy_remaining_pct(sections.get("five hour"))
        if weekly is None or five_hour is None:
            continue
        gauges[family] = GroupGauge(weekly_remaining_pct=weekly,
                                    five_hour_remaining_pct=five_hour)
    return gauges


def _resolve_claude_bin(claude_bin: str | None) -> str:
    return (claude_bin or os.environ.get("COCKPIT_CLAUDE_BIN")
            or shutil.which("claude") or "claude")


def _resolve_agy_bin(agy_bin: str | None) -> str:
    return (agy_bin or os.environ.get("COCKPIT_AGY_BIN")
            or shutil.which("agy") or "agy")


def _drain_until_quiet(fd: int, deadline: float,
                        quiet_period: float = _SETTLE_QUIET_PERIOD) -> str:
    """Read from `fd` until either no new bytes arrive for `quiet_period`
    seconds (the screen has settled) or `deadline` (a time.monotonic()
    timestamp) passes."""
    chunks: list[bytes] = []
    last_read = time.monotonic()
    got_any = False
    while True:
        now = time.monotonic()
        remaining = deadline - now
        if remaining <= 0:
            break
        ready, _, _ = select.select([fd], [], [], min(quiet_period, remaining))
        if ready:
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
            last_read = now
            got_any = True
        elif got_any and (now - last_read) >= quiet_period:
            break
    return b"".join(chunks).decode("utf-8", errors="replace")


def _reap_child(proc: subprocess.Popen[bytes]) -> None:
    """Guarantee the child (and its process group) is terminated and
    waited on, so it never lingers as a zombie or orphan."""
    if proc.poll() is not None:
        proc.wait()
        return
    with contextlib.suppress(ProcessLookupError, OSError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=2.0)


def capture_usage_gauge(claude_bin: str | None = None,
                         timeout: float = _DEFAULT_CAPTURE_TIMEOUT
                         ) -> LimitGauge | None:
    """Spawn `claude` under a pty, send /usage, and parse the limit
    gauges from the settled screen. Returns None on a spawn failure, on
    hitting `timeout` before a parseable screen arrives, or on an
    unparseable screen -- never raises for those cases.

    The child (and its process group) is always terminated and reaped
    before returning or re-raising, including when a KeyboardInterrupt
    arrives mid-capture -- teardown runs via `finally`, then the
    KeyboardInterrupt propagates; it is not swallowed."""
    bin_path = _resolve_claude_bin(claude_bin)
    deadline = time.monotonic() + timeout
    master_fd = slave_fd = -1
    proc: subprocess.Popen[bytes] | None = None
    try:
        master_fd, slave_fd = pty.openpty()
        proc = subprocess.Popen(
            [bin_path], stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            start_new_session=True, close_fds=True)
        os.close(slave_fd)
        slave_fd = -1
        _drain_until_quiet(master_fd, deadline)
        os.write(master_fd, b"/usage\r")
        screen = _drain_until_quiet(master_fd, deadline)
        return parse_usage_screen(strip_ansi(screen))
    except OSError:
        return None
    finally:
        if slave_fd != -1:
            with contextlib.suppress(OSError):
                os.close(slave_fd)
        with contextlib.suppress(OSError):
            os.close(master_fd)
        if proc is not None:
            _reap_child(proc)


def _answer_terminal_queries(fd: int, data: bytes) -> None:
    """Answer the terminal-capability queries agy's TUI blocks on before it
    draws anything: DECRQM mode reports, the kitty keyboard-protocol flags
    query, primary device attributes and a cursor-position report. A pty
    master is not a terminal emulator, so nothing replies to these unless
    we do, and the usage screen never renders."""
    for mode in _DECRQM_QUERY_RE.findall(data):
        with contextlib.suppress(OSError):
            os.write(fd, b"\x1b[?" + mode + b";2$y")
    for pattern, reply in ((_KITTY_QUERY_RE, b"\x1b[?0u"),
                           (_DA1_QUERY_RE, b"\x1b[?1;2c"),
                           (_CPR_QUERY_RE, b"\x1b[1;1R")):
        if pattern.search(data):
            with contextlib.suppress(OSError):
                os.write(fd, reply)


def _drain_answering_queries(fd: int, deadline: float,
                              quiet_period: float = _AGY_QUIET_PERIOD) -> str:
    """_drain_until_quiet()'s variant for agy: identical settle rule, but
    every chunk read is also offered to _answer_terminal_queries()."""
    chunks: list[bytes] = []
    last_read = time.monotonic()
    got_any = False
    while True:
        now = time.monotonic()
        remaining = deadline - now
        if remaining <= 0:
            break
        ready, _, _ = select.select([fd], [], [], min(quiet_period, remaining))
        if ready:
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
            _answer_terminal_queries(fd, data)
            last_read = now
            got_any = True
        elif got_any and (now - last_read) >= quiet_period:
            break
    return b"".join(chunks).decode("utf-8", errors="replace")


def capture_agy_usage_gauges(agy_bin: str | None = None,
                              timeout: float = _AGY_CAPTURE_TIMEOUT
                              ) -> dict[str, GroupGauge]:
    """capture_usage_gauge()'s Antigravity sibling: spawn `agy` under a pty,
    open its /usage screen and parse the grouped weekly/five-hour gauges.
    Returns {} on a spawn failure, on hitting `timeout`, or on an
    unparseable screen -- never raises for those cases, and never
    fabricates a gauge. The claude path above is untouched by this one:
    the two share only the reaping and ANSI helpers.

    AG-01 established that no `agy` subcommand reports quota, so this drives
    the interactive session -- which needs two things a plain pipe does not
    give it: a real window size (a 0x0 pty makes the TUI draw nothing) and
    replies to its capability queries.

    `/usage` is sent up to _AGY_USAGE_ATTEMPTS times because a first run in
    an untrusted directory opens a trust prompt that swallows the first
    attempt; the retry costs nothing and, unlike sending a bare Enter to
    dismiss the prompt, can never submit an empty prompt to the model.

    The child (and its process group) is always terminated and reaped
    before returning or re-raising, including when a KeyboardInterrupt
    arrives mid-capture -- teardown runs via `finally`, then the
    KeyboardInterrupt propagates; it is not swallowed."""
    bin_path = _resolve_agy_bin(agy_bin)
    deadline = time.monotonic() + timeout
    master_fd = slave_fd = -1
    proc: subprocess.Popen[bytes] | None = None
    try:
        master_fd, slave_fd = pty.openpty()
        with contextlib.suppress(OSError):
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", _AGY_ROWS, _AGY_COLS, 0, 0))
        proc = subprocess.Popen(
            [bin_path], stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            start_new_session=True, close_fds=True,
            env={**os.environ, "TERM": "xterm-256color",
                 "COLUMNS": str(_AGY_COLS), "LINES": str(_AGY_ROWS)})
        os.close(slave_fd)
        slave_fd = -1
        screen = _drain_answering_queries(master_fd, deadline)
        for _ in range(_AGY_USAGE_ATTEMPTS):
            os.write(master_fd, b"/usage\r")
            screen = _drain_answering_queries(master_fd, deadline)
            gauges = parse_agy_usage_screen(strip_ansi(screen))
            if gauges:
                return gauges
            if time.monotonic() >= deadline:
                break
        return {}
    except OSError:
        return {}
    finally:
        if slave_fd != -1:
            with contextlib.suppress(OSError):
                os.close(slave_fd)
        with contextlib.suppress(OSError):
            os.close(master_fd)
        if proc is not None:
            _reap_child(proc)


def usage_gauge_lines(*, capture: bool = False,
                       backends: tuple[str, ...] = ("claude",),
                       claude_bin: str | None = None,
                       agy_bin: str | None = None,
                       timeout: float = _DEFAULT_CAPTURE_TIMEOUT) -> list[str]:
    """Off unless `capture=True` -- no capture function is called at all
    otherwise, not even to resolve a binary path. `backends` names which
    ones to capture, so the one opt-in flag covers whichever backends are
    configured rather than growing a second flag per backend.

    Percentages are reported as REMAINING for both backends: claude's
    LimitGauge holds percentages used, so it is inverted here; agy's
    GroupGauge already holds remaining and is passed through. Each backend
    (and each agy group) falls back to NOT_ON_DISK_LINE on its own -- one
    backend's failed capture never withholds another's gauge."""
    #: One backend keeps UM-05's flat, unprefixed shape; naming several is
    #: what introduces the per-backend heading and indent.
    pad = "  " if len(backends) > 1 else ""
    lines: list[str] = []
    for backend in backends:
        if len(backends) > 1:
            lines.append(f"{backend}:")
        if backend == "claude":
            gauge = (capture_usage_gauge(claude_bin=claude_bin, timeout=timeout)
                     if capture else None)
            if gauge is None:
                lines.append(f"{pad}{NOT_ON_DISK_LINE}")
            else:
                lines.append(
                    f"{pad}session (5h): {100 - gauge.session_pct}% remaining")
                lines.append(
                    f"{pad}week:         {100 - gauge.week_pct}% remaining")
        elif backend == "antigravity":
            gauges = (capture_agy_usage_gauges(agy_bin=agy_bin, timeout=timeout)
                      if capture else {})
            if not gauges:
                lines.append(f"{pad}{NOT_ON_DISK_LINE}")
            for family in sorted(gauges):
                group = gauges[family]
                lines.append(f"{pad}{family}:")
                lines.append(f"{pad}  5h:   "
                             f"{group.five_hour_remaining_pct}% remaining")
                lines.append(f"{pad}  week: "
                             f"{group.weekly_remaining_pct}% remaining")
    return lines
