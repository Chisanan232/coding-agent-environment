#!/usr/bin/env bash
# audit_log.sh
# Fires: PostToolUse[Bash]
# Purpose: Log every shell command Claude Code executes for traceability.
#
# Claude Code passes hook context via stdin as JSON (not env vars).
# PostToolUse[Bash] JSON shape:
#   { "hook_event_name": "PostToolUse", "tool_name": "Bash",
#     "tool_input": { "command": "..." },
#     "tool_response": { "output": "...", "exitCode": N }, ... }

set -euo pipefail

[ -f "${ENGINEERING_CONFIG_ENV:-${HOME}/.claude/config.env}" ] && source "${ENGINEERING_CONFIG_ENV:-${HOME}/.claude/config.env}"

LOG_DIR="${ENGINEERING_AUDIT_LOG_DIR-${CLAUDE_AUDIT_LOG_DIR:-${ENGINEERING_STATE_DIR:-${HOME}/.claude}/audit}}"
LOG_FILE="$LOG_DIR/commands.jsonl"
MAX_LOG_SIZE_BYTES=10485760  # 10 MB

# Skip if audit is disabled
if [ "${ENGINEERING_SKIP_AUDIT-${CLAUDE_SKIP_AUDIT:-0}}" = "1" ]; then
    exit 0
fi

mkdir -p "$LOG_DIR"

# Rotate log if it exceeds max size
if [ -f "$LOG_FILE" ]; then
    LOG_SIZE=$(wc -c < "$LOG_FILE" 2>/dev/null || echo 0)
    if [ "$LOG_SIZE" -gt "$MAX_LOG_SIZE_BYTES" ]; then
        mv "$LOG_FILE" "${LOG_FILE}.$(date +%Y%m%d_%H%M%S).bak"
    fi
fi

# Read the full stdin JSON once — stdin can only be consumed once.
HOOK_INPUT=$(cat)

# Extract command from tool_input.command
COMMAND=$(echo "$HOOK_INPUT" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get('tool_input', {}).get('command', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")

# Extract exit code from tool_response (field name may vary by Claude Code version)
EXIT_CODE=$(echo "$HOOK_INPUT" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    resp = data.get('tool_response', {})
    code = resp.get('exitCode', resp.get('exit_code', resp.get('returnCode', 0)))
    print(int(code) if code is not None else 0)
except Exception:
    print(0)
" 2>/dev/null || echo "0")

TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
USER=$(whoami 2>/dev/null || echo "unknown")
PWD_VAL=$(pwd 2>/dev/null || echo "unknown")

# Write JSON log entry (audit failures must never block execution).
# Pass free-text fields via env vars to prevent shell-interpolation injection
# into the Python source. If COMMAND contains ''', r'''$COMMAND''' terminates
# prematurely and the audit entry is silently dropped.
_AL_COMMAND="$COMMAND" \
_AL_TIMESTAMP="$TIMESTAMP" \
_AL_USER="$USER" \
_AL_PWD="$PWD_VAL" \
_AL_EXIT_CODE="$EXIT_CODE" \
python3 -c "
import json, os
entry = {
    'timestamp': os.environ['_AL_TIMESTAMP'],
    'user': os.environ['_AL_USER'],
    'working_directory': os.environ['_AL_PWD'],
    'command': os.environ.get('_AL_COMMAND', ''),
    'exit_code': int(os.environ.get('_AL_EXIT_CODE', '0') or '0'),
}
print(json.dumps(entry))
" >> "$LOG_FILE" 2>/dev/null || true

exit 0
