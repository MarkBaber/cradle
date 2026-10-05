#!/usr/bin/env python3
"""ADR gate — structural validation, index regeneration, spec staleness.

Written to the adr-scaffold contract (its assets/ directory is not present in
the skills mount; see PATCHES.md). Stdlib only.

    python3 scripts/check_adr.py --all        # structure + index + staleness
    python3 scripts/check_adr.py --reindex    # regenerate docs/adr/README.md
    python3 scripts/check_adr.py --merge-driver %O %A %B   # git merge driver

Checks:
  numbering    sequential from 0001, no gaps (a gap means a record was
               deleted, which is the failure this log exists to prevent),
               no collisions.
  status       closed vocabulary: Accepted | Proposed | Superseded by NNNN.
  supersession resolves in BOTH directions — every "Superseded by NNNN" points
               at a real ADR, and every "Supersedes: MMMM" is matched by
               MMMM's own status saying so. A one-way pointer is worse than
               none: readers of the survivor believe the old decision is
               retired while it still reads Accepted.
  index        docs/adr/README.md table between the ADR-INDEX markers matches
               the files on disk.
  staleness    the rationale section of each SPEC path must not have a newer
               git commit time than the newest file under docs/adr/ — this
               forces rationale edits through the log instead of quietly
               rewriting the reasoning. Semantic enforcement is NOT attempted
               and is not claimed: no check can read a diff and decide whether
               it violates the intent of ADR 0003.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADR_DIR = ROOT / "docs" / "adr"
SPEC_PATHS = [ROOT / "docs" / "SPEC.md"]
SECTION_PATTERN = re.compile(r"(?i)^#+\s.*(decisions|rejected alternatives|architecture)")

ADR_RE = re.compile(r"^(\d{4})-[a-z0-9-]+\.md$")
STATUS_RE = re.compile(r"^-\s*\*\*Status:\*\*\s*(.+?)\s*$", re.M)
SUPERSEDES_RE = re.compile(r"^-\s*\*\*Supersedes:\*\*\s*(.+?)\s*$", re.M)
TITLE_RE = re.compile(r"^#\s*(\d{4})\.\s*(.+?)\s*$", re.M)
INDEX_START, INDEX_END = "<!-- ADR-INDEX:START -->", "<!-- ADR-INDEX:END -->"


def _adr_files() -> list[Path]:
    return sorted(p for p in ADR_DIR.glob("*.md") if ADR_RE.match(p.name))


def check_structure(errors: list[str]) -> dict[str, dict]:
    records: dict[str, dict] = {}
    for path in _adr_files():
        num = ADR_RE.match(path.name).group(1)
        text = path.read_text(encoding="utf-8")
        if num in records:
            errors.append(f"duplicate ADR number {num}")
        title_m = TITLE_RE.search(text)
        if not title_m:
            errors.append(f"{path.name}: missing '# NNNN. Title' heading")
        elif title_m.group(1) != num:
            errors.append(f"{path.name}: heading number {title_m.group(1)} != filename {num}")
        status_m = STATUS_RE.search(text)
        if not status_m:
            errors.append(f"{path.name}: missing '- **Status:**' line")
            continue
        status = status_m.group(1)
        valid_status = (status in ("Accepted", "Proposed")
                        or re.fullmatch(r"Superseded by \d{4}", status))
        if not valid_status:
            errors.append(f"{path.name}: bad status {status!r} "
                          "(allowed: Accepted | Proposed | Superseded by NNNN)")
        sup_m = SUPERSEDES_RE.search(text)
        supersedes = [n for n in re.findall(r"\d{4}", sup_m.group(1))] if sup_m else []
        if "Rejected alternatives" not in text:
            errors.append(f"{path.name}: no 'Rejected alternatives' section")
        records[num] = {"path": path, "status": status, "supersedes": supersedes,
                        "title": title_m.group(2) if title_m else path.stem}

    nums = sorted(records)
    for i, n in enumerate(nums, start=1):
        if int(n) != i:
            errors.append(f"ADR numbering gap or misorder at {n} (expected {i:04d})")
            break

    for num, rec in records.items():
        if m := re.fullmatch(r"Superseded by (\d{4})", rec["status"]):
            target = m.group(1)
            if target not in records:
                errors.append(f"{num}: superseded by {target}, which does not exist")
            elif num not in records[target]["supersedes"]:
                errors.append(f"{num}: says superseded by {target}, but {target} "
                              f"does not list 'Supersedes: {num}' (one-way pointer)")
        for old in rec["supersedes"]:
            if old not in records:
                errors.append(f"{num}: supersedes {old}, which does not exist")
            elif records[old]["status"] != f"Superseded by {num}":
                errors.append(f"{num}: supersedes {old}, but {old} status is "
                              f"{records[old]['status']!r} (one-way pointer)")
    return records


def build_index(records: dict[str, dict]) -> str:
    rows = ["| ADR | Title | Status |", "|---|---|---|"]
    for num in sorted(records):
        r = records[num]
        rows.append(f"| [{num}]({r['path'].name}) | {r['title']} | {r['status']} |")
    return "\n".join(rows)


def check_index(records: dict[str, dict], errors: list[str], *, fix: bool) -> None:
    readme = ADR_DIR / "README.md"
    table = build_index(records)
    if not readme.exists():
        if fix:
            readme.write_text(
                f"# Architecture Decision Records\n\n"
                f"Immutable. Superseded, never edited (see CLAUDE.md).\n\n"
                f"{INDEX_START}\n{table}\n{INDEX_END}\n")
            return
        errors.append("docs/adr/README.md missing (run --reindex)")
        return
    text = readme.read_text(encoding="utf-8")
    if INDEX_START not in text or INDEX_END not in text:
        errors.append("docs/adr/README.md: missing ADR-INDEX markers")
        return
    pre, rest = text.split(INDEX_START, 1)
    _, post = rest.split(INDEX_END, 1)
    new = f"{pre}{INDEX_START}\n{table}\n{INDEX_END}{post}"
    if new != text:
        if fix:
            readme.write_text(new)
        else:
            errors.append("docs/adr/README.md index is stale (run --reindex)")


def _git_time(args: list[str]) -> int:
    try:
        out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return 0
    times = [int(x) for x in out.split() if x.isdigit()]
    return max(times, default=0)


def check_staleness(errors: list[str]) -> None:
    newest_adr = _git_time(["log", "-1", "--format=%ct", "--", "docs/adr"])
    if not newest_adr:
        return                                    # no git history yet (fresh scaffold)
    for spec in SPEC_PATHS:
        if not spec.exists():
            continue
        lines = spec.read_text(encoding="utf-8").splitlines()
        start = next((i for i, ln in enumerate(lines) if SECTION_PATTERN.match(ln)), None)
        if start is None:
            continue
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i].startswith("#") and not SECTION_PATTERN.match(lines[i])),
                   len(lines))
        rel = spec.relative_to(ROOT)
        spec_time = _git_time(["log", "-1", "--format=%ct",
                               f"-L{start + 1},{end}:{rel}", "--no-patch"])
        if spec_time > newest_adr:
            errors.append(
                f"{rel}: rationale section (lines {start + 1}-{end}) committed at "
                f"{spec_time}, newer than newest ADR at {newest_adr} — record the "
                "change as a new ADR instead of editing the reasoning in place")


ROW_RE = re.compile(r"^\|\s*\[(\d{4})\]\(([^)]+)\)\s*\|.*\|$")


def _split_index(text: str) -> tuple[str, list[str], list[str], str] | None:
    """(text before the markers, header lines, row lines, text after), or None."""
    if INDEX_START not in text or INDEX_END not in text:
        return None
    pre, rest = text.split(INDEX_START, 1)
    table, post = rest.split(INDEX_END, 1)
    lines = [ln.rstrip() for ln in table.strip("\n").splitlines()]
    return pre, lines[:2], lines[2:], post


def _rows_by_number(rows: list[str]) -> dict[str, str] | None:
    keyed: dict[str, str] = {}
    for row in rows:
        m = ROW_RE.match(row)
        if not m or m.group(1) in keyed:
            return None
        keyed[m.group(1)] = row
    return keyed


def _merge_file(ancestor: str, current: str, other: str) -> tuple[bool, str]:
    """`git merge-file -p` over three strings: (merged cleanly, merged text)."""
    with tempfile.TemporaryDirectory() as tmp:
        paths = [Path(tmp) / name for name in ("o", "a", "b")]
        for path, text in zip(paths, (ancestor, current, other), strict=True):
            path.write_text(text, encoding="utf-8")
        result = subprocess.run(
            ["git", "merge-file", "-p", str(paths[1]), str(paths[0]), str(paths[2])],
            capture_output=True, text=True)
    return result.returncode == 0, result.stdout


def _merge_rows(base: dict[str, str], ours: dict[str, str],
                theirs: dict[str, str]) -> list[str] | None:
    """Three-way merge by ADR number; None when a row cannot be merged."""
    merged: list[str] = []
    for num in sorted(base.keys() | ours.keys() | theirs.keys()):
        o, a, b = base.get(num), ours.get(num), theirs.get(num)
        if a == b or b == o:
            row = a
        elif a == o:
            row = b
        else:
            return None   # changed differently on both sides, or a number collision
        if row is not None:
            merged.append(row)
    return merged


def _merge_index(texts: list[str]) -> str | None:
    parts = [_split_index(t) for t in texts]
    if None in parts:
        return None
    (o_pre, _, o_rows, o_post), (a_pre, a_head, a_rows, a_post), (b_pre, _, b_rows, b_post) = parts  # type: ignore[misc]
    keyed = [_rows_by_number(r) for r in (o_rows, a_rows, b_rows)]
    if None in keyed:
        return None
    pre_ok, pre = _merge_file(o_pre, a_pre, b_pre)
    post_ok, post = _merge_file(o_post, a_post, b_post)
    rows = _merge_rows(*keyed)  # type: ignore[arg-type]
    if not (pre_ok and post_ok) or rows is None:
        return None
    table = "\n".join([*a_head, *rows])
    return f"{pre}{INDEX_START}\n{table}\n{INDEX_END}{post}"


def run_merge_driver(ancestor: str, current: str, other: str) -> int:
    """Git merge driver (`%O %A %B`) for docs/adr/README.md (DX-160).

    Works from the three inputs alone: git runs drivers before it checks the
    merge result out, so the working tree is stale here. Text outside the
    ADR-INDEX markers goes through `git merge-file`; the table merges by ADR
    number. Writes the result to `current` (%A) and returns 0, or returns 1
    leaving a plain whole-file merge in `current` for a human to resolve.
    """
    texts = [Path(p).read_text(encoding="utf-8") for p in (ancestor, current, other)]
    if (merged := _merge_index(texts)) is not None:
        Path(current).write_text(merged, encoding="utf-8")
        return 0
    Path(current).write_text(_merge_file(*texts)[1], encoding="utf-8")
    print("check_adr: docs/adr/README.md index not merged row by row (a row changed "
          "differently on both sides, a number collision, or text outside the markers "
          "conflicts); resolve by hand, then run --reindex", file=sys.stderr)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="structure + index + staleness")
    ap.add_argument("--reindex", action="store_true", help="regenerate the index")
    ap.add_argument("--merge-driver", nargs=3, metavar=("ANCESTOR", "CURRENT", "OTHER"),
                    help="git merge driver for docs/adr/README.md (%%O %%A %%B)")
    args = ap.parse_args()
    if args.merge_driver:
        return run_merge_driver(*args.merge_driver)

    errors: list[str] = []
    records = check_structure(errors)
    check_index(records, errors, fix=args.reindex)
    if args.all:
        check_staleness(errors)

    if errors:
        print("ADR log INVALID:")
        for e in errors:
            print("  -", e)
        return 1
    print(f"OK: {len(records)} ADRs, numbering contiguous, supersession consistent, index fresh.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
