#!/usr/bin/env bash
# Owner-run: plan, apply, verify and roll back the ASK-first merge candidate (SPE-118) with fresh hashes.
#
#   scripts/ask-first-apply.sh            plan only: regenerate + lint the candidate, plan against the live file, change nothing
#   scripts/ask-first-apply.sh --apply    the same, then back up, apply pinned to the hashes just printed, and read the result back
#   scripts/ask-first-apply.sh --rollback restore the exact original bytes recorded by the last --apply
#   scripts/trusted-allow-apply.sh [...]  the same three modes for the single owner-merge-runner allow rule (needs the runner installed first)
#
# Only candidate-rollout.py touches the live file, and only on --apply / --rollback. Nothing from the file is printed.
# Environment overrides (for tests): LIVE_SETTINGS (default ~/.claude/settings.json), BACKUP_DIR (default ~/.claude/backups).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIVE="${LIVE_SETTINGS:-$HOME/.claude/settings.json}"
BACKUPS="${BACKUP_DIR:-$HOME/.claude/backups}"
RO="$ROOT/scripts/candidate-rollout.py"
WSP="$ROOT/scripts/workstation-policy.py"
MODE="${1:-plan}"
# ASK_FIRST_KIND=trusted-allow (via scripts/trusted-allow-apply.sh) applies the single runner allow rule instead of the ask-first asks.
KIND="${ASK_FIRST_KIND:-merge-scope}"
RUNNER_BIN="$HOME/.claude/bin/owner-merge-runner"   # the exact path the allow rule names; deliberately not overridable
case "$KIND" in
  merge-scope)   RENDER="--merge-scope";   CAND="$ROOT/docs/examples/candidate-settings-merge-scope.example.json"; RECORD="$BACKUPS/ask-first-rollback.json";;
  trusted-allow) RENDER="--trusted-allow"; CAND="";                                                                RECORD="$BACKUPS/trusted-allow-rollback.json";;
  *) echo "REFUSED: unknown ASK_FIRST_KIND" >&2; exit 1;;
esac

die() { echo "REFUSED: $*" >&2; exit 1; }
field() { python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }

[ -f "$LIVE" ] || die "live settings not found: $LIVE"

if [ "$MODE" = "--rollback" ]; then
  [ -f "$RECORD" ] || die "no rollback record at $RECORD (nothing was applied by this script)"
  BACKUP="$(field backup < "$RECORD")"; BACKUP_SHA="$(field backup_sha < "$RECORD")"; RESULT_SHA="$(field result_sha < "$RECORD")"
  python3 "$RO" rollback --live "$LIVE" --backup "$BACKUP" --expect-backup-sha "$BACKUP_SHA" --expect-live-sha "$RESULT_SHA"
  echo "ROLLED BACK: live file restored to sha256 $BACKUP_SHA"
  rm -f "$RECORD"
  exit 0
fi
case "$MODE" in plan|--apply) ;; *) die "usage: $0 [--apply|--rollback]";; esac

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
python3 "$WSP" render-settings --kind personal $RENDER --out "$TMP/candidate.json"
if [ "$KIND" = "merge-scope" ]; then
  cmp -s "$TMP/candidate.json" "$CAND" || die "generator output differs from the committed candidate; update this checkout first"
  echo "candidate: generator output == committed example"
else
  # The runner rule embeds your home directory, so the committed example is only a sample: judge the rendered file itself.
  CAND="$TMP/candidate.json"
  [ ! -L "$RUNNER_BIN" ] || die "$RUNNER_BIN is a symlink; the allow rule must name a regular file"
  [ -f "$RUNNER_BIN" ] && [ -x "$RUNNER_BIN" ] || die "owner-merge-runner is not installed at $RUNNER_BIN; run scripts/install-owner-merge-runner.sh first"
  cmp -s "$ROOT/scripts/owner-merge-runner.py" "$RUNNER_BIN" || die "the installed runner differs from this checkout's scripts/owner-merge-runner.py; re-run scripts/install-owner-merge-runner.sh --update"
  echo "runner: installed at $RUNNER_BIN and identical to this checkout"
fi
python3 "$WSP" lint-settings "$CAND" >/dev/null || die "candidate lint is not CLEAN"
echo "candidate: lint CLEAN ($KIND)"

python3 "$RO" plan --live "$LIVE" --candidate "$CAND" > "$TMP/plan.json" || die "plan reported a blocking finding; run the plan command by hand to read it"
read -r LIVE_SHA RESULT_SHA ADDED ALLOWS BLOCKING < <(python3 - "$TMP/plan.json" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1])); r = d["report"]
print(d["live_sha"], d["result_sha"], len(r["asks_added"]), r["allow_added"] if isinstance(r["allow_added"], int) else len(r["allow_added"]), str(d["blocking"]).lower())
EOF
)
echo "plan: live_sha=$LIVE_SHA result_sha=$RESULT_SHA asks_added=$ADDED allow_added=$ALLOWS blocking=$BLOCKING"
[ "$BLOCKING" = "false" ] || die "plan is blocking"
if [ "$KIND" = "merge-scope" ]; then
  [ "$ALLOWS" = "0" ] || die "candidate would add allow rules"
else
  [ "$ADDED" = "0" ] || die "trusted-allow candidate would add asks"
  [ "$ALLOWS" = "1" ] || [ "$LIVE_SHA" = "$RESULT_SHA" ] || die "trusted-allow candidate must add exactly one allow rule (adds $ALLOWS)"
fi
if [ "$LIVE_SHA" = "$RESULT_SHA" ]; then echo "already applied: nothing to do"; exit 0; fi

if [ "$MODE" = "plan" ]; then
  echo "PLAN ONLY: nothing was changed. Re-run with --apply to apply exactly this plan."
  exit 0
fi

mkdir -p "$BACKUPS"; chmod 700 "$BACKUPS" 2>/dev/null || true
EXTRA="$BACKUPS/settings.pre-ask-first.$(date +%Y%m%d-%H%M%S).json"
cp -p "$LIVE" "$EXTRA"; chmod 600 "$EXTRA"
python3 "$RO" apply --live "$LIVE" --candidate "$CAND" --expect-live-sha "$LIVE_SHA" --expect-result-sha "$RESULT_SHA" --backup-dir "$BACKUPS" > "$TMP/apply.json"
BACKUP="$(field backup < "$TMP/apply.json")"
# Record the rollback pointer BEFORE reading back, so a failed readback still leaves a working --rollback.
python3 - "$RECORD" "$BACKUP" "$LIVE_SHA" "$RESULT_SHA" <<'EOF'
import json, os, sys
path, backup, backup_sha, result_sha = sys.argv[1:]
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as fh:
    json.dump({"backup": backup, "backup_sha": backup_sha, "result_sha": result_sha}, fh)
EOF
python3 "$RO" verify --live "$LIVE" --expect-result-sha "$RESULT_SHA" | grep -q '"verdict": "VERIFIED"' \
  || die "readback did not verify. The live file WAS modified. Backup of the original: $BACKUP ; extra copy: $EXTRA ; restore with: $0 --rollback"
echo "APPLIED and VERIFIED: live sha256 is now $RESULT_SHA"
echo "backup (exact original bytes): $BACKUP"
echo "extra copy: $EXTRA"
echo "rollback: $0 --rollback"
if [ "$KIND" = "trusted-allow" ]; then
  echo "NEXT: start a fresh Claude Code session; the runner ALLOW is NOT yet verified on the real host (see docs/OWNER-MERGE-TRUSTED-ALLOW-HANDOFF.md)."
else
  echo "NEXT: start a fresh Claude Code session to use it (see docs/OWNER-MERGE-ASK-FIRST-HANDOFF.md)."
fi
