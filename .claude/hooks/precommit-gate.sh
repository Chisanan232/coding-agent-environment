#!/usr/bin/env bash
# precommit-gate.sh
# Fires: PreToolUse[Bash] — before git push commands
# Purpose: Require pre-commit hooks to pass before any push.
# Runs 'pre-commit run --all-files' and blocks if any check fails.
# If pre-commit is not installed, warns but does not block.
#
# IMPORTANT: If checks fail, delegate repair to the language-appropriate
# pre-commit repair skill (e.g., python-precommit-repair for Python projects).
# Do not enter a self-repair loop from within this hook.
#
# Claude Code passes hook context via stdin as JSON (not env vars).
# PreToolUse JSON shape:
#   { "hook_event_name": "PreToolUse", "tool_name": "Bash",
#     "tool_input": { "command": "..." }, ... }

set -euo pipefail

[ -f "${ENGINEERING_CONFIG_ENV:-${HOME}/.claude/config.env}" ] && source "${ENGINEERING_CONFIG_ENV:-${HOME}/.claude/config.env}"

# Allow opt-out for engineers who run pre-commit manually or find the gate too slow.
if [[ "${ENGINEERING_SKIP_PRECOMMIT_GATE-${CLAUDE_SKIP_PRECOMMIT_GATE:-0}}" == "1" ]]; then
    echo "[HOOK] precommit-gate: skipped (CLAUDE_SKIP_PRECOMMIT_GATE=1)." >&2
    exit 0
fi

# Read the full stdin JSON once — stdin can only be consumed once.
HOOK_INPUT=$(cat)

COMMAND=$(echo "$HOOK_INPUT" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get('tool_input', {}).get('command', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")

if [ -z "$COMMAND" ]; then
    exit 0
fi

# Only gate on push commands.
if ! echo "$COMMAND" | grep -qiE "git push"; then
    exit 0
fi

# Check if pre-commit is available.
if ! command -v pre-commit &>/dev/null; then
    echo "[HOOK] WARNING: precommit-gate — pre-commit not found. Skipping pre-commit check." >&2
    echo "[HOOK] Install pre-commit and run 'pre-commit install' before pushing to protected branches." >&2
    exit 0
fi

# Check if .pre-commit-config.yaml exists in the repo root.
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || echo ".")
if [ ! -f "${REPO_ROOT}/.pre-commit-config.yaml" ]; then
    echo "[HOOK] precommit-gate: No .pre-commit-config.yaml found. Skipping." >&2
    exit 0
fi

echo "[HOOK] precommit-gate: Running pre-commit checks..." >&2

if pre-commit run --all-files 2>&1; then
    echo "[HOOK] precommit-gate: All pre-commit checks passed." >&2
    exit 0
else
    echo "[HOOK] BLOCKED: precommit-gate — pre-commit checks failed." >&2
    echo "[HOOK] Fix the violations above before pushing." >&2
    echo "[HOOK] Use the language-appropriate pre-commit repair skill (see CLAUDE.md Skill Invocation Guide)." >&2
    echo "[HOOK] Do not bypass with --no-verify." >&2
    exit 2
fi
