#!/usr/bin/env bash
# workflow-state.sh — Read, write, and archive per-ticket workflow state.
#
# Usage:
#   workflow-state.sh write  <ticket> <workflow> <step> <total> <status>
#   workflow-state.sh read   <ticket>
#   workflow-state.sh archive <ticket>
#
# Writes are atomic (mktemp + mv) — partial writes from crashes leave no
# corrupt state. Reads validate JSON before returning; exits 1 on corruption.
#
# State: ${CLAUDE_WORKFLOW_STATE_DIR}/<ticket>.json

set -euo pipefail

[ -f "${ENGINEERING_CONFIG_ENV:-${HOME}/.claude/config.env}" ] && source "${ENGINEERING_CONFIG_ENV:-${HOME}/.claude/config.env}"

STATE_DIR="${ENGINEERING_WORKFLOW_STATE_DIR-${CLAUDE_WORKFLOW_STATE_DIR:-${ENGINEERING_STATE_DIR:-${HOME}/.claude}/workflow-state}}"
ARCHIVE_DIR="${STATE_DIR}/archive"

cmd="${1:-}"
ticket="${2:-}"

if [[ -z "$cmd" || -z "$ticket" ]]; then
    echo "Usage: workflow-state.sh <write|read|archive> <ticket> [workflow step total status]" >&2
    exit 1
fi

mkdir -p "$STATE_DIR" "$ARCHIVE_DIR"

STATE_FILE="${STATE_DIR}/${ticket}.json"

# ── Shared JSON validation ────────────────────────────────────────────────────

_validate_json() {
    local file="$1"
    # Pass file and ticket via env vars — never interpolate free-text paths or
    # ticket refs directly into Python source (single quotes in values break syntax).
    _WS_FILE="$file" _WS_TICKET="$ticket" python3 -c "
import json, sys, os
f = os.environ['_WS_FILE']
t = os.environ['_WS_TICKET']
try:
    with open(f) as fh:
        json.load(fh)
    sys.exit(0)
except json.JSONDecodeError as e:
    print(f'[workflow-state] CORRUPT state file for ticket: {t} — {e}', file=sys.stderr)
    sys.exit(1)
except FileNotFoundError:
    print(f'[workflow-state] state file not found: {f}', file=sys.stderr)
    sys.exit(1)
" 2>&1
}

# ── write ─────────────────────────────────────────────────────────────────────

if [[ "$cmd" == "write" ]]; then
    workflow="${3:-}"
    step="${4:-}"
    total="${5:-}"
    status="${6:-}"

    if [[ -z "$workflow" || -z "$step" || -z "$total" || -z "$status" ]]; then
        echo "workflow-state write: missing arguments (workflow step total status)" >&2
        exit 1
    fi

    timestamp="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

    # Atomic write: write to temp file, then mv (POSIX-atomic on same filesystem)
    # Shell-to-Python boundary: pass ALL values via environment variables and use
    # a quoted heredoc (<<'PYEOF') so that no shell interpolation occurs inside the
    # Python source. step/total are caller-supplied and could theoretically contain
    # special characters in future callers; passing via env vars is consistent with
    # the codebase convention and eliminates the injection surface entirely.
    tmp="$(mktemp "${STATE_FILE}.XXXXXX")"
    _WS_TICKET="$ticket" _WS_WORKFLOW="$workflow" _WS_STEP="$step" \
    _WS_TOTAL="$total" _WS_STATUS="$status" _WS_TIMESTAMP="$timestamp" \
    python3 - > "$tmp" <<'PYEOF'
import json, os
print(json.dumps({
    "ticket":      os.environ["_WS_TICKET"],
    "workflow":    os.environ["_WS_WORKFLOW"],
    "step":        os.environ["_WS_STEP"],
    "total_steps": os.environ["_WS_TOTAL"],
    "status":      os.environ["_WS_STATUS"],
    "timestamp":   os.environ["_WS_TIMESTAMP"],
}, indent=2))
PYEOF
    mv "$tmp" "$STATE_FILE"

    echo "[workflow-state] wrote: ${ticket} | ${workflow} | step ${step}/${total} | ${status} | ${timestamp}"
    exit 0
fi

# ── read ──────────────────────────────────────────────────────────────────────

if [[ "$cmd" == "read" ]]; then
    if [[ ! -f "$STATE_FILE" ]]; then
        echo "[workflow-state] no state file found for ticket: ${ticket}" >&2
        exit 1
    fi

    # Use "if ! var=$(cmd)" rather than "var=$(cmd); if [[ $? -ne 0 ]]".
    # With set -e active, a failing command substitution in an assignment exits
    # the script immediately — the subsequent $? check is never reached.
    # The "if !" form puts the assignment in a conditional context where set -e
    # does not apply, so the failure is handled by the if branch instead.
    if ! validation_output="$(_validate_json "$STATE_FILE")"; then
        echo "$validation_output" >&2
        echo "[workflow-state] Delete the corrupt file and restart from ticket-pickup-check:" >&2
        echo "  rm ${STATE_FILE}" >&2
        exit 1
    fi

    cat "$STATE_FILE"
    exit 0
fi

# ── archive ───────────────────────────────────────────────────────────────────

if [[ "$cmd" == "archive" ]]; then
    if [[ ! -f "$STATE_FILE" ]]; then
        echo "[workflow-state] nothing to archive for ticket: ${ticket}" >&2
        exit 0
    fi

    timestamp="$(date -u +"%Y%m%dT%H%M%SZ")"
    archive_path="${ARCHIVE_DIR}/${ticket}_${timestamp}.json"
    mv "$STATE_FILE" "$archive_path"
    echo "[workflow-state] archived: ${ticket} → ${archive_path}"
    exit 0
fi

echo "workflow-state: unknown command '${cmd}'. Use write, read, or archive." >&2
exit 1
