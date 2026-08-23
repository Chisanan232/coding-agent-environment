#!/usr/bin/env bash
# sync-check.sh - report-first bidirectional drift check between this repo's
# tracked desired-state files and the live machine (~/.claude, ~/.codex).
#
# Reports drift. Never copies/overwrites either side — that decision is
# always explicit and manual (`cp` in whichever direction you decide is
# correct after reading the report).
#
# Usage: ./scripts/sync-check.sh
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

DIFFER=0
LIVE_MISSING=0
REPO_ONLY=0
IDENTICAL=0

# repo_path:live_path pairs — the canonical file allowlist from docs/ALLOWLIST.md
PAIRS=(
    ".mcp.json:$HOME/.claude/.mcp.json"
    ".claude/CLAUDE.md:$HOME/.claude/CLAUDE.md"
    ".claude/RTK.md:$HOME/.claude/RTK.md"
    ".claude/settings.json:$HOME/.claude/settings.json"
    ".claude/config.env:$HOME/.claude/config.env"
    ".claude/mcp-servers.runtime.json:$HOME/.claude/mcp-servers.runtime.json"
    ".claude/statusline.py:$HOME/.claude/statusline.py"
    ".claude/subagent-statusline.py:$HOME/.claude/subagent-statusline.py"
    "codex/AGENTS.md:$HOME/.codex/AGENTS.md"
)

echo "coding-agent-environment — sync-check"
echo "repo:  $REPO_ROOT"
echo "live:  \$HOME = $HOME"
echo ""

for pair in "${PAIRS[@]}"; do
    repo_rel="${pair%%:*}"
    live_path="${pair#*:}"
    repo_path="$REPO_ROOT/$repo_rel"

    if [[ ! -f "$repo_path" ]]; then
        continue  # not applicable in this repo snapshot
    fi

    if [[ ! -f "$live_path" ]]; then
        echo "MISSING-LIVE   $repo_rel"
        echo "               tracked in repo, absent on live machine ($live_path)"
        LIVE_MISSING=$((LIVE_MISSING + 1))
        continue
    fi

    if diff -q "$repo_path" "$live_path" >/dev/null 2>&1; then
        IDENTICAL=$((IDENTICAL + 1))
    else
        echo "DIFFERS        $repo_rel"
        echo "               repo:  $repo_path"
        echo "               live:  $live_path"
        echo "               $(diff "$repo_path" "$live_path" 2>/dev/null | head -1 || echo '(binary or unreadable diff)')"
        DIFFER=$((DIFFER + 1))
    fi
done

# Live directories with a tracked repo counterpart but no per-file mapping
# above (e.g. hooks/, skills/) — checked by directory-listing diff instead.
for dir in ".claude/hooks" ".claude/skills" "bin"; do
    repo_dir="$REPO_ROOT/$dir"
    case "$dir" in
        bin) live_dir="$HOME/.local/bin" ;;
        *)   live_dir="$HOME/$dir" ;;
    esac
    [[ -d "$repo_dir" ]] || continue
    if [[ ! -d "$live_dir" ]]; then
        echo "MISSING-LIVE   $dir/ (directory)"
        echo "               tracked in repo, no corresponding live directory ($live_dir)"
        LIVE_MISSING=$((LIVE_MISSING + 1))
        continue
    fi
    while IFS= read -r -d '' f; do
        rel="${f#"$repo_dir"/}"
        live_f="$live_dir/$rel"
        if [[ ! -f "$live_f" ]]; then
            echo "MISSING-LIVE   $dir/$rel"
            LIVE_MISSING=$((LIVE_MISSING + 1))
        elif ! diff -q "$f" "$live_f" >/dev/null 2>&1; then
            echo "DIFFERS        $dir/$rel"
            DIFFER=$((DIFFER + 1))
        else
            IDENTICAL=$((IDENTICAL + 1))
        fi
    done < <(find "$repo_dir" -type f -print0)
done

echo ""
echo "Summary: $IDENTICAL identical, $DIFFER differ, $LIVE_MISSING missing-on-live"
echo ""
echo "This is a REPORT, not a sync. No files were copied in either direction."
echo "Decide per-file which side is correct, then copy explicitly:"
echo "  repo -> live:  cp <repo-path> <live-path>   (apply this repo's desired state)"
echo "  live -> repo:  cp <live-path> <repo-path>    (capture a live change into the repo, then commit)"

if [[ "$DIFFER" -gt 0 || "$LIVE_MISSING" -gt 0 ]]; then
    exit 1
fi
exit 0
