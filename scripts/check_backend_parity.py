"""scripts/check_backend_parity.py — AG-12: CI gate for hook/agent sync between backends.

Structural gate analogous to check_adr.py and test_layers.py.  It cannot
judge whether a translation is *correct*, only that one exists or the gap was
honestly named.

Reads two inputs:
  - .claude/agents/*.md filenames  →  agent names (stems)
  - .claude/settings.json PreToolUse block  →  hook statusMessage identifiers

Reads one output:
  - docs/antigravity-backend-parity.md  →  parity table

Fails when:
  1. An agent or hook in .claude/ has no row in the parity table.
  2. A row's translation pointer does not resolve (a stale reference is worse
     than an honest "no equivalent").

A row that says "no antigravity equivalent" with a non-empty reason is always
considered valid — the gate never demands a translation that does not exist.

Wire-in: add ``python3 scripts/check_backend_parity.py`` to scripts/validate
alongside check_adr.py and test_layers.py.  Enforced by the PreToolUse push
hook in .claude/settings.json.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

AGENTS_DIR = ROOT / ".claude" / "agents"
SETTINGS_FILE = ROOT / ".claude" / "settings.json"
PARITY_DOC = ROOT / "docs" / "antigravity-backend-parity.md"

#: Files searched when resolving a translation pointer.  A pointer is
#: resolved if it appears literally in at least one of these files.
TRANSLATION_SOURCES: list[Path] = [
    ROOT / "src" / "nightshift" / "executor" / "agy_runner.py",
    ROOT / "scripts" / "backlog.py",
]

GAP_MARKER = "no antigravity equivalent"


# ---------------------------------------------------------------------------
# Input readers
# ---------------------------------------------------------------------------

def _agent_names() -> list[str]:
    """Stems of .claude/agents/*.md, sorted."""
    return sorted(p.stem for p in AGENTS_DIR.glob("*.md"))


def _hook_identifiers() -> list[str]:
    """statusMessage strings for every PreToolUse hook, in document order."""
    raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    out: list[str] = []
    for block in raw.get("hooks", {}).get("PreToolUse", []):
        for hook in block.get("hooks", []):
            sm = hook.get("statusMessage", "")
            if sm:
                out.append(sm)
    return out


# ---------------------------------------------------------------------------
# Parity-table parser
# ---------------------------------------------------------------------------

def _table_rows(text: str) -> dict[str, dict[str, str]]:
    """Parse every Markdown table row from the parity doc into a dict keyed
    by the first non-empty cell (agent name or hook statusMessage).

    Only rows with exactly 4 pipe-delimited cells are considered (the two
    tables in the parity doc both use 4 columns).  Header and separator rows
    are skipped.
    """
    rows: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) != 4:
            continue
        # Skip header and separator rows (separator cells contain only dashes)
        if all(re.fullmatch(r"-+", c) for c in cells):
            continue
        key_raw = cells[0]
        # Strip backtick-quoted name used in agent table: `gate-runner` → gate-runner
        # and statusMessage format: `ruff check before commit` → ruff check before commit
        key = key_raw.strip("`")
        if not key or key.startswith("Hook") or key.startswith("Agent"):
            continue  # column header cell
        rows[key] = {
            "name": key,
            "purpose": cells[1],
            "translation": cells[2],
            "reason": cells[3],
        }
    return rows


# ---------------------------------------------------------------------------
# Resolution checker
# ---------------------------------------------------------------------------

def _translation_source_text() -> str:
    """Concatenate all TRANSLATION_SOURCES for pointer resolution checks."""
    parts: list[str] = []
    for src in TRANSLATION_SOURCES:
        if src.exists():
            parts.append(src.read_text(encoding="utf-8"))
    return "\n".join(parts)


def _pointer_resolves(translation: str, source: str) -> bool:
    """True if *translation* appears literally in *source*."""
    return bool(translation) and translation in source


# ---------------------------------------------------------------------------
# Gate logic
# ---------------------------------------------------------------------------

def check(
    agent_names: list[str],
    hook_ids: list[str],
    parity_text: str,
) -> list[str]:
    """Return a list of error strings; empty means the gate passes."""
    rows = _table_rows(parity_text)
    source = _translation_source_text()
    errors: list[str] = []

    for name in agent_names:
        if name not in rows:
            errors.append(
                f"agent '{name}': no row in docs/antigravity-backend-parity.md"
            )
        else:
            row = rows[name]
            translation = row["translation"]
            reason = row["reason"]
            if GAP_MARKER.lower() in translation.lower():
                # Named gap: reason must be non-trivial
                if len(reason.strip()) < 20:
                    errors.append(
                        f"agent '{name}': gap row has no real reason"
                        f" (reason: {reason!r})"
                    )
            else:
                # Translation claimed: pointer must resolve
                if not _pointer_resolves(translation, source):
                    errors.append(
                        f"agent '{name}': translation pointer does not resolve:"
                        f" {translation!r}"
                    )

    for hook in hook_ids:
        if hook not in rows:
            errors.append(
                f"hook '{hook}': no row in docs/antigravity-backend-parity.md"
            )
        else:
            row = rows[hook]
            translation = row["translation"]
            reason = row["reason"]
            if GAP_MARKER.lower() in translation.lower():
                if len(reason.strip()) < 20:
                    errors.append(
                        f"hook '{hook}': gap row has no real reason"
                        f" (reason: {reason!r})"
                    )
            else:
                if not _pointer_resolves(translation, source):
                    errors.append(
                        f"hook '{hook}': translation pointer does not resolve:"
                        f" {translation!r}"
                    )

    return errors


def main() -> int:
    errors: list[str] = []

    if not AGENTS_DIR.is_dir():
        errors.append(f".claude/agents/ directory not found at {AGENTS_DIR}")
    if not SETTINGS_FILE.exists():
        errors.append(f".claude/settings.json not found at {SETTINGS_FILE}")
    if not PARITY_DOC.exists():
        errors.append(f"docs/antigravity-backend-parity.md not found at {PARITY_DOC}")

    if errors:
        print("backend-parity gate FAILED (missing inputs):")
        for e in errors:
            print("  -", e)
        return 1

    agent_names = _agent_names()
    hook_ids = _hook_identifiers()
    parity_text = PARITY_DOC.read_text(encoding="utf-8")

    errors = check(agent_names, hook_ids, parity_text)

    if errors:
        print("backend-parity gate FAILED:")
        for e in errors:
            print("  -", e)
        return 1

    n_agents = len(agent_names)
    n_hooks = len(hook_ids)
    print(
        f"OK: {n_agents} agent(s) and {n_hooks} hook(s) all have parity rows,"
        " no stale translation pointers."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
