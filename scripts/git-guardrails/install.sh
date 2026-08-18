#!/usr/bin/env bash
# Installs the git PATH-shim guard (scripts/git-guardrails/bin/git) for the
# current user: prepends the shim's directory onto PATH in ~/.bashrc, and
# records this checkout's root plus the real git binary's path as env vars
# the shim reads at runtime.
#
# Run once per machine, from the primary checkout (not a worktree) -- the
# guard only ever restricts that one root. Re-run after `git clone`-ing a
# fresh copy elsewhere, or if the real git binary moves (e.g. a package
# upgrade), to refresh the recorded paths.
#
# Idempotent: re-running replaces the previously-installed block in
# ~/.bashrc rather than duplicating it.
set -euo pipefail

root="$(git rev-parse --show-toplevel)"
git_dir="$(git rev-parse --git-dir)"
case "$git_dir" in
  */worktrees/*)
    echo "install.sh must be run from the primary checkout, not a linked worktree ($root)." >&2
    exit 1
    ;;
esac

shim_dir="$root/scripts/git-guardrails/bin"
chmod +x "$shim_dir/git"

real_git=""
while IFS= read -r candidate; do
  case "$candidate" in
    "$shim_dir"/*) continue ;;
    *)
      real_git="$candidate"
      break
      ;;
  esac
done <<<"$(command -v -a git)"

if [ -z "$real_git" ]; then
  echo "install.sh: could not find a real git binary on PATH (only the shim itself matched)." >&2
  exit 1
fi

profile="$HOME/.bashrc"
begin_marker="# --- NightShift branch-guard (scripts/git-guardrails/install.sh) ---"
end_marker="# --- end NightShift branch-guard ---"

tmp="$(mktemp)"
if [ -f "$profile" ]; then
  awk -v b="$begin_marker" -v e="$end_marker" '
    $0 == b { skip = 1 }
    !skip { print }
    $0 == e { skip = 0 }
  ' "$profile" >"$tmp"
else
  : >"$tmp"
fi

{
  cat "$tmp"
  echo "$begin_marker"
  echo "export NIGHTSHIFT_GIT_GUARD_ROOT=\"$root\""
  echo "export NIGHTSHIFT_GIT_GUARD_REAL_GIT=\"$real_git\""
  echo "export PATH=\"$shim_dir:\$PATH\""
  echo "$end_marker"
} >"$profile.new"
rm -f "$tmp"
mv "$profile.new" "$profile"

echo "Installed. Restart open shells (or run: source $profile) for this to take effect."
echo "Guard root: $root"
echo "Real git:   $real_git"
