#!/usr/bin/env bash
# Per-ticket workflow state. Writes and archival share ticket-state's lock.

set -euo pipefail

[ -f "${ENGINEERING_CONFIG_ENV:-${HOME}/.claude/config.env}" ] && source "${ENGINEERING_CONFIG_ENV:-${HOME}/.claude/config.env}"

STATE_DIR="${ENGINEERING_WORKFLOW_STATE_DIR-${CLAUDE_WORKFLOW_STATE_DIR:-${ENGINEERING_STATE_DIR:-${HOME}/.claude}/workflow-state}}"
HELPER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ticket-state.py"
cmd="${1:-}"
ticket="${2:-}"

if [[ -z "$cmd" || -z "$ticket" ]]; then
    echo "Usage: workflow-state.sh <write|read|snapshot|archive> <ticket> [workflow step total status|TOKEN]" >&2
    exit 1
fi

shift 2
case "$cmd" in
    write|read|snapshot|archive)
        python3 "$HELPER" workflow "$STATE_DIR" "$cmd" "$ticket" "$@"
        ;;
    *)
        echo "workflow-state: unknown command '$cmd'. Use write, read, snapshot, or archive." >&2
        exit 1
        ;;
esac
