#!/usr/bin/env bash
# Owner-run: install the validating merge runner at its one stable path (default ~/.claude/bin/owner-merge-runner), mode 0755, atomically.
#   scripts/install-owner-merge-runner.sh            install (refuses to replace a different existing file)
#   scripts/install-owner-merge-runner.sh --update   replace an existing runner with this checkout's version
# Installing the file grants nothing: without the allow rule it is only a program, and without ~/.claude/owner-merge-runner-allowlist.json it refuses every merge.
# Env override for tests: OWNER_MERGE_RUNNER_PATH.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/scripts/owner-merge-runner.py"
DEST="${OWNER_MERGE_RUNNER_PATH:-$HOME/.claude/bin/owner-merge-runner}"
die() { echo "REFUSED: $*" >&2; exit 1; }
[ -f "$SRC" ] || die "runner source not found: $SRC"
head -1 "$SRC" | grep -q '^#!.*python3' || die "runner source has no python3 shebang"
if [ -e "$DEST" ] || [ -L "$DEST" ]; then
  [ ! -L "$DEST" ] || die "$DEST is a symlink"
  if cmp -s "$SRC" "$DEST"; then echo "already installed and identical: $DEST"; exit 0; fi
  [ "${1:-}" = "--update" ] || die "$DEST exists and differs; inspect it, then re-run with --update"
fi
mkdir -p "$(dirname "$DEST")"
TMP="$(mktemp "$(dirname "$DEST")/.owner-merge-runner.XXXXXX")"; trap 'rm -f "$TMP"' EXIT
cp "$SRC" "$TMP"; chmod 755 "$TMP"
mv -f "$TMP" "$DEST"; trap - EXIT
echo "installed: $DEST sha256=$(shasum -a 256 "$DEST" | cut -d' ' -f1)"
echo "NEXT: create the allowlist (docs/OWNER-MERGE-TRUSTED-ALLOW-HANDOFF.md step 2), then scripts/trusted-allow-apply.sh"
