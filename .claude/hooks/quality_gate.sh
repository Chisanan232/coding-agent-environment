#!/usr/bin/env bash
# quality_gate.sh
# Fires: PostToolUse[Write|Edit]
# Purpose: Lightweight quality check after every file write or edit.
# Operates in fast mode by default; strict mode when CLAUDE_STRICT=1.
#
# Claude Code passes hook context via stdin as JSON (not env vars).
# PostToolUse[Write|Edit] JSON shape:
#   { "hook_event_name": "PostToolUse", "tool_name": "Write",
#     "tool_input": { "file_path": "...", "content": "..." }, ... }

set -euo pipefail

[ -f "${ENGINEERING_CONFIG_ENV:-${HOME}/.claude/config.env}" ] && source "${ENGINEERING_CONFIG_ENV:-${HOME}/.claude/config.env}"

STRICT_MODE="${ENGINEERING_STRICT-${CLAUDE_STRICT:-0}}"
COOLDOWN_FILE="${ENGINEERING_QUALITY_COOLDOWN_FILE:-/tmp/.claude_quality_gate_last_run}"
COOLDOWN_SECONDS=30

# Cooldown: do not run more than once per 30 seconds to avoid noise.
if [ -f "$COOLDOWN_FILE" ]; then
    LAST_RUN=$(cat "$COOLDOWN_FILE")
    NOW=$(date +%s)
    ELAPSED=$((NOW - ${LAST_RUN:-0}))
    if [ "$ELAPSED" -lt "$COOLDOWN_SECONDS" ]; then
        exit 0
    fi
fi
date +%s > "$COOLDOWN_FILE"

# Read the full stdin JSON once — stdin can only be consumed once.
HOOK_INPUT=$(cat)

FILE_PATH=$(echo "$HOOK_INPUT" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get('tool_input', {}).get('file_path', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")

if [ -z "$FILE_PATH" ] || [ ! -f "$FILE_PATH" ]; then
    exit 0
fi

WARNINGS=0

# --- Check 1: Debug statements ---
if echo "$FILE_PATH" | grep -qE '\.(py)$'; then
    if grep -nE '^\s*(print\(|import pdb|pdb\.set_trace|breakpoint\()' "$FILE_PATH" 2>/dev/null; then
        echo "[HOOK] WARNING: Debug statement detected in $FILE_PATH" >&2
        WARNINGS=$((WARNINGS + 1))
    fi
fi

if echo "$FILE_PATH" | grep -qE '\.(js|ts|jsx|tsx)$'; then
    if grep -nE '^\s*console\.(log|debug|trace|warn)\(' "$FILE_PATH" 2>/dev/null; then
        echo "[HOOK] WARNING: console.log/debug detected in $FILE_PATH" >&2
        WARNINGS=$((WARNINGS + 1))
    fi
    if grep -nE '^\s*debugger' "$FILE_PATH" 2>/dev/null; then
        echo "[HOOK] WARNING: debugger statement detected in $FILE_PATH" >&2
        WARNINGS=$((WARNINGS + 1))
    fi
fi

# --- Check 2: TODO without issue reference ---
# Note: ERE (grep -E) does not support lookaheads on macOS or GNU grep,
# so the negative-lookahead pattern silently matches nothing. Use a pipeline instead.
# Store results in a variable so the pipeline executes only once.
_todo_hits=$(grep -n 'TODO' "$FILE_PATH" 2>/dev/null | grep -v '#[0-9]' | head -5 || true)
if [ -n "$_todo_hits" ]; then
    echo "$_todo_hits"
    echo "[HOOK] WARNING: TODO comment without issue reference in $FILE_PATH" >&2
    WARNINGS=$((WARNINGS + 1))
fi

# --- Check 3: File size ---
FILE_SIZE=$(wc -c < "$FILE_PATH" 2>/dev/null || echo 0)
if [ "$FILE_SIZE" -gt 1048576 ]; then
    echo "[HOOK] WARNING: File exceeds 1MB: $FILE_PATH (${FILE_SIZE} bytes)" >&2
    WARNINGS=$((WARNINGS + 1))
fi

# --- Strict mode: treat warnings as errors ---
if [ "$STRICT_MODE" = "1" ] && [ "$WARNINGS" -gt 0 ]; then
    echo "[HOOK] STRICT MODE: $WARNINGS quality warning(s) in $FILE_PATH. Fix before proceeding." >&2
    exit 1
fi

exit 0
