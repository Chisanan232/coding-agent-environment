#!/usr/bin/env bash
# Owner-run: plan, apply, verify and roll back the ASK-first merge candidate (SPE-118) with fresh hashes.
#
#   scripts/ask-first-apply.sh            plan only: regenerate + lint the candidate, plan against the live file, change nothing
#   scripts/ask-first-apply.sh --apply    the same, then back up, apply pinned to the hashes just printed, and read the result back
#   scripts/ask-first-apply.sh --rollback restore the exact original bytes recorded by the last --apply
#
# Only candidate-rollout.py touches the live file, and only on --apply / --rollback. Nothing from the file is printed.
# Environment overrides (for tests): LIVE_SETTINGS (default ~/.claude/settings.json), BACKUP_DIR (default ~/.claude/backups).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIVE="${LIVE_SETTINGS:-$HOME/.claude/settings.json}"
BACKUPS="${BACKUP_DIR:-$HOME/.claude/backups}"
CAND="$ROOT/docs/examples/candidate-settings-merge-scope.example.json"
RO="$ROOT/scripts/candidate-rollout.py"
WSP="$ROOT/scripts/workstation-policy.py"
RECORD="$BACKUPS/ask-first-rollback.json"
MODE="${1:-plan}"

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
python3 "$WSP" render-settings --kind personal --merge-scope --out "$TMP/candidate.json"
cmp -s "$TMP/candidate.json" "$CAND" || die "generator output differs from the committed candidate; update this checkout first"
python3 "$WSP" lint-settings "$CAND" >/dev/null || die "candidate lint is not CLEAN"
echo "candidate: generator output == committed example, lint CLEAN"

python3 "$RO" plan --live "$LIVE" --candidate "$CAND" > "$TMP/plan.json" || die "plan reported a blocking finding; run the plan command by hand to read it"
read -r LIVE_SHA RESULT_SHA ADDED ALLOWS BLOCKING < <(python3 - "$TMP/plan.json" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1])); r = d["report"]
print(d["live_sha"], d["result_sha"], len(r["asks_added"]), r["allow_added"] if isinstance(r["allow_added"], int) else len(r["allow_added"]), str(d["blocking"]).lower())
EOF
)
echo "plan: live_sha=$LIVE_SHA result_sha=$RESULT_SHA asks_added=$ADDED allow_added=$ALLOWS blocking=$BLOCKING"
[ "$BLOCKING" = "false" ] || die "plan is blocking"
[ "$ALLOWS" = "0" ] || die "candidate would add allow rules"
if [ "$LIVE_SHA" = "$RESULT_SHA" ] || [ "$ADDED" = "0" ]; then echo "already applied: nothing to do"; exit 0; fi

if [ "$MODE" = "plan" ]; then
  echo "PLAN ONLY: nothing was changed. Re-run with --apply to apply exactly this plan."
  exit 0
fi

mkdir -p "$BACKUPS"; chmod 700 "$BACKUPS" 2>/dev/null || true
EXTRA="$BACKUPS/settings.pre-ask-first.$(date +%Y%m%d-%H%M%S).json"
cp -p "$LIVE" "$EXTRA"; chmod 600 "$EXTRA"
python3 "$RO" apply --live "$LIVE" --candidate "$CAND" --expect-live-sha "$LIVE_SHA" --expect-result-sha "$RESULT_SHA" --backup-dir "$BACKUPS" > "$TMP/apply.json"
BACKUP="$(field backup < "$TMP/apply.json")"
python3 "$RO" verify --live "$LIVE" --expect-result-sha "$RESULT_SHA" | grep -q '"verdict": "VERIFIED"' || die "readback did not verify; roll back with: $0 --rollback"
python3 - "$RECORD" "$BACKUP" "$LIVE_SHA" "$RESULT_SHA" <<'EOF'
import json, os, sys
path, backup, backup_sha, result_sha = sys.argv[1:]
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as fh:
    json.dump({"backup": backup, "backup_sha": backup_sha, "result_sha": result_sha}, fh)
EOF
echo "APPLIED and VERIFIED: live sha256 is now $RESULT_SHA"
echo "backup (exact original bytes): $BACKUP"
echo "extra copy: $EXTRA"
echo "rollback: $0 --rollback"
echo "NEXT: start a fresh Claude Code session; the host prompt is NOT yet verified (see docs/OWNER-MERGE-ASK-FIRST-HANDOFF.md, step 5)."
