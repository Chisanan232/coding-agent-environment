#!/usr/bin/env bash
# session-memory.sh
# Utility: persistent per-ticket session notes across interrupted Claude Code sessions.
# Usage:
#   session-memory.sh read   <ticket>              → print existing notes (empty if none)
#   session-memory.sh append <ticket> <section> <body>  → append a section entry
#   session-memory.sh clear  <ticket>              → delete notes for a ticket
#   session-memory.sh list                         → list all tickets with notes
#
# Notes are stored as Markdown in ${CLAUDE_SESSION_NOTES_DIR}/<ticket>.md
# Default dir: ~/.claude/session-notes/

set -euo pipefail

[ -f "${ENGINEERING_CONFIG_ENV:-${HOME}/.claude/config.env}" ] && source "${ENGINEERING_CONFIG_ENV:-${HOME}/.claude/config.env}"

NOTES_DIR="${ENGINEERING_SESSION_NOTES_DIR-${CLAUDE_SESSION_NOTES_DIR:-${ENGINEERING_STATE_DIR:-${HOME}/.claude}/session-notes}}"
mkdir -p "$NOTES_DIR"

_usage() {
    echo "Usage:"
    echo "  session-memory.sh read   <ticket>"
    echo "  session-memory.sh append <ticket> <section> <body>"
    echo "  session-memory.sh clear  <ticket>"
    echo "  session-memory.sh list"
    exit 1
}

CMD="${1:-}"
shift || true

case "$CMD" in
    read)
        TICKET="${1:?ticket required}"
        NOTE_FILE="${NOTES_DIR}/${TICKET}.md"
        if [ -f "$NOTE_FILE" ]; then
            cat "$NOTE_FILE"
        else
            echo "(no session notes for ${TICKET})"
        fi
        ;;

    append)
        TICKET="${1:?ticket required}"
        SECTION="${2:?section required}"
        BODY="${3:?body required}"
        NOTE_FILE="${NOTES_DIR}/${TICKET}.md"
        TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

        # Bootstrap frontmatter on first write
        if [ ! -f "$NOTE_FILE" ]; then
            cat > "$NOTE_FILE" <<FRONTMATTER
---
ticket: ${TICKET}
created: ${TIMESTAMP}
---

# Session Notes — ${TICKET}

FRONTMATTER
        fi

        # Append the section entry atomically
        TMP=$(mktemp "${NOTES_DIR}/.tmp.XXXXXX")
        cat "$NOTE_FILE" > "$TMP"
        printf '\n## %s — %s\n\n%s\n' "$SECTION" "$TIMESTAMP" "$BODY" >> "$TMP"
        mv "$TMP" "$NOTE_FILE"
        echo "[session-memory] Appended '${SECTION}' to ${TICKET}" >&2
        ;;

    clear)
        TICKET="${1:?ticket required}"
        NOTE_FILE="${NOTES_DIR}/${TICKET}.md"
        if [ -f "$NOTE_FILE" ]; then
            rm "$NOTE_FILE"
            echo "[session-memory] Cleared notes for ${TICKET}" >&2
        else
            echo "[session-memory] No notes found for ${TICKET}" >&2
        fi
        ;;

    list)
        # Use find instead of a glob+ls pipeline. With set -euo pipefail,
        # "ls $DIR/*.md" fails (and exits the script) if no .md files exist,
        # even when $DIR has other files — the glob passes a literal "*.md"
        # argument to ls, which exits non-zero.
        md_files=$(find "$NOTES_DIR" -maxdepth 1 -name "*.md" 2>/dev/null | sort)
        if [ -z "$md_files" ]; then
            echo "(no session notes found)"
        else
            echo "$md_files" | xargs -I{} basename {} .md
        fi
        ;;

    *)
        _usage
        ;;
esac
