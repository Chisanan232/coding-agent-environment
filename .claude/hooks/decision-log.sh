#!/usr/bin/env bash
# decision-log.sh — Structured decision log for skills and agents.
#
# Records WHY the system made a decision, not just WHAT it ran.
# Complements the audit log (what commands ran) and workflow state (what phase).
#
# Usage:
#   decision-log.sh record \
#     --ticket   "PROJ-123"     \
#     --agent    "main-agent"   \
#     --skill    "dev-impl-loop" \
#     --phase    "2"            \
#     --decision "proceed"      \
#     --reason   "47 tests pass after fix" \
#     --context  "pytest: 47 passed, 0 failed"
#
#   decision-log.sh tail [N]              — show last N entries (default: 20)
#   decision-log.sh query --ticket PROJ   — show all entries for a ticket
#
# Output: ${CLAUDE_DECISION_LOG_DIR}/<YYYY-MM-DD>.jsonl (one JSON object per line)
#
# Shell-to-Python boundary: all free-text fields (reason, context) are passed
# via environment variables, not interpolated into Python source. This prevents
# single quotes, backslashes, and newlines in those values from breaking the
# JSON output or causing injection.

set -euo pipefail

if [ -f "${ENGINEERING_CONFIG_ENV:-${HOME}/.claude/config.env}" ]; then
    bash -n "${ENGINEERING_CONFIG_ENV:-${HOME}/.claude/config.env}" 2>/dev/null \
        && source "${ENGINEERING_CONFIG_ENV:-${HOME}/.claude/config.env}" \
        || echo "[decision-log] Warning: ~/.claude/config.env has syntax errors — using defaults" >&2
fi

ENABLED="${ENGINEERING_DECISION_LOG_ENABLED-${CLAUDE_DECISION_LOG_ENABLED:-1}}"
[[ "$ENABLED" == "0" ]] && exit 0

LOG_DIR="${ENGINEERING_DECISION_LOG_DIR-${CLAUDE_DECISION_LOG_DIR:-${ENGINEERING_STATE_DIR:-${HOME}/.claude}/decisions}}"
MAX_CONTEXT="${ENGINEERING_DECISION_LOG_MAX_CONTEXT-${CLAUDE_DECISION_LOG_MAX_CONTEXT:-500}}"
mkdir -p "$LOG_DIR"

DATE_KEY="$(date -u +"%Y-%m-%d")"
LOG_FILE="${LOG_DIR}/${DATE_KEY}.jsonl"

# ── record ────────────────────────────────────────────────────────────────────

if [[ "${1:-}" == "record" ]]; then
    shift
    ticket="" agent="" skill="" phase="" decision="" reason="" context=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --ticket)   ticket="$2";   shift 2 ;;
            --agent)    agent="$2";    shift 2 ;;
            --skill)    skill="$2";    shift 2 ;;
            --phase)    phase="$2";    shift 2 ;;
            --decision) decision="$2"; shift 2 ;;
            --reason)   reason="$2";   shift 2 ;;
            --context)  context="$2";  shift 2 ;;
            *) echo "decision-log record: unknown flag '$1'" >&2; exit 1 ;;
        esac
    done

    if [[ -z "$decision" || -z "$reason" ]]; then
        echo "decision-log record: --decision and --reason are required" >&2
        exit 1
    fi

    # Truncate context to MAX_CONTEXT characters (safe: bash substring, no Python)
    if [[ ${#context} -gt $MAX_CONTEXT ]]; then
        context="${context:0:$MAX_CONTEXT}…"
    fi

    timestamp="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

    # Pass all values as environment variables — never interpolate free-text
    # strings into Python source. This makes quotes, backslashes, and newlines
    # safe regardless of what the caller passes.
    _DL_TIMESTAMP="$timestamp" \
    _DL_TICKET="$ticket"       \
    _DL_AGENT="$agent"         \
    _DL_SKILL="$skill"         \
    _DL_PHASE="$phase"         \
    _DL_DECISION="$decision"   \
    _DL_REASON="$reason"       \
    _DL_CONTEXT="$context"     \
    python3 - >> "$LOG_FILE" <<'PYEOF'
import json, os
entry = {
    "timestamp": os.environ["_DL_TIMESTAMP"],
    "ticket":    os.environ["_DL_TICKET"],
    "agent":     os.environ["_DL_AGENT"],
    "skill":     os.environ["_DL_SKILL"],
    "phase":     os.environ["_DL_PHASE"],
    "decision":  os.environ["_DL_DECISION"],
    "reason":    os.environ["_DL_REASON"],
    "context":   os.environ["_DL_CONTEXT"],
}
print(json.dumps(entry))
PYEOF

    echo "[decision-log] recorded: ${ticket:-?} | ${skill:-?} phase ${phase:-?} | ${decision}"
    exit 0
fi

# ── tail ──────────────────────────────────────────────────────────────────────

if [[ "${1:-}" == "tail" ]]; then
    n="${2:-20}"
    find "$LOG_DIR" -name "*.jsonl" | sort | xargs cat 2>/dev/null | tail -n "$n" \
    | python3 -c "
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        d = json.loads(line)
        ts  = d.get('timestamp', '?')
        tkt = d.get('ticket', '?')[:12]
        agt = d.get('agent', '?')[:15]
        skl = d.get('skill', '?')[:20]
        ph  = d.get('phase', '?')
        dec = d.get('decision', '?')
        rsn = d.get('reason', '?')[:80]
        print(f'{ts}  {tkt:12}  {agt:15}  {skl:20}  ph={ph:2}  [{dec}]  {rsn}')
    except Exception:
        print(line)
"
    exit 0
fi

# ── query ─────────────────────────────────────────────────────────────────────

if [[ "${1:-}" == "query" ]]; then
    shift
    filter_ticket=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --ticket) filter_ticket="$2"; shift 2 ;;
            *) echo "decision-log query: unknown flag '$1'" >&2; exit 1 ;;
        esac
    done

    # python3 - <<'HEREDOC' conflicts with pipeline stdin (heredoc wins).
    # Write the script to a temp file so pipe data reaches python3 normally.
    _tmpscript="$(mktemp /tmp/dl_query_XXXXXX.py)"
    cat > "$_tmpscript" <<'PYEOF'
import json, os, sys
ft = os.environ.get("_DL_FILTER_TICKET", "")
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        d = json.loads(line)
        if ft and d.get("ticket", "") != ft:
            continue
        print(json.dumps(d, indent=2))
    except Exception:
        pass
PYEOF
    export _DL_FILTER_TICKET="$filter_ticket"
    find "$LOG_DIR" -name "*.jsonl" | sort | xargs cat 2>/dev/null \
        | python3 "$_tmpscript"
    rm -f "$_tmpscript"
    exit 0
fi

echo "decision-log: unknown sub-command '${1:-}'.
Usage:
  decision-log.sh record --ticket T --agent A --skill S --phase P --decision D --reason R [--context C]
  decision-log.sh tail [N]
  decision-log.sh query --ticket T" >&2
exit 1
