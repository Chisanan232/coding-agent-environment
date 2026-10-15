#!/usr/bin/env bash
# Persistent per-ticket notes. The Python helper serializes every ticket write.

set -euo pipefail

[ -f "${ENGINEERING_CONFIG_ENV:-${HOME}/.claude/config.env}" ] && source "${ENGINEERING_CONFIG_ENV:-${HOME}/.claude/config.env}"

NOTES_DIR="${ENGINEERING_SESSION_NOTES_DIR-${CLAUDE_SESSION_NOTES_DIR:-${ENGINEERING_STATE_DIR:-${HOME}/.claude}/session-notes}}"
HELPER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ticket-state.py"
CMD="${1:-}"
shift || true

case "$CMD" in
    read|snapshot|append|clear)
        python3 "$HELPER" notes "$NOTES_DIR" "$CMD" "$@"
        ;;
    list)
        mkdir -p "$NOTES_DIR"
        md_files=$(find "$NOTES_DIR" -maxdepth 1 -name "*.md" 2>/dev/null | sort)
        if [ -z "$md_files" ]; then
            echo "(no session notes found)"
        else
            echo "$md_files" | xargs -I{} basename {} .md
        fi
        ;;
    *)
        echo "Usage: session-memory.sh <read|snapshot|append|clear|list> <ticket> [section body|TOKEN]" >&2
        exit 1
        ;;
esac
