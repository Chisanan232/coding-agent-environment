#!/usr/bin/env bash
# sync-check.sh - report-first bidirectional drift check between this repo's
# tracked desired-state files and the live machine (~/.claude, ~/.codex).
#
# Defaults to reporting all tracked surfaces. --codex checks only managed
# signal-first Codex state. Applying is owned by profile-install.sh --global.
# Usage: ./scripts/sync-check.sh [--codex | --capabilities | --full-parity]
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
SYNC_HOME="${CODING_AGENT_SYNC_HOME:-$HOME}"

if [[ "${1:-}" == "--routing" && "$#" == 1 ]]; then
    exec bash "$REPO_ROOT/scripts/profile-install.sh" --routing --check
elif [[ "${1:-}" == "--codex" && "$#" == 1 ]]; then
    exec bash "$REPO_ROOT/scripts/profile-install.sh" --global --check
elif [[ "${1:-}" == "--full-parity" && "$#" == 1 ]]; then
    result=0
    bash "$REPO_ROOT/scripts/profile-install.sh" --global --check || result=1
    bash "$REPO_ROOT/scripts/profile-install.sh" --claude-skills --check || result=1
    exit "$result"
elif [[ "${1:-}" == "--capabilities" && "$#" == 1 ]]; then
    exec bash "$REPO_ROOT/scripts/profile-install.sh" --capabilities --check
elif [[ "$#" != 0 ]]; then
    echo 'Usage: sync-check.sh [--codex | --capabilities | --full-parity]' >&2
    exit 2
fi

DIFFER=0
LIVE_MISSING=0
IDENTICAL=0

# repo_path:live_path pairs — the canonical file allowlist from docs/ALLOWLIST.md
PAIRS=(
    ".mcp.json:$SYNC_HOME/.claude/.mcp.json"
    ".claude/CLAUDE.md:$SYNC_HOME/.claude/CLAUDE.md"
    ".claude/RTK.md:$SYNC_HOME/.claude/RTK.md"
    ".claude/settings.json:$SYNC_HOME/.claude/settings.json"
    ".claude/config.env:$SYNC_HOME/.claude/config.env"
    ".claude/mcp-servers.runtime.json:$SYNC_HOME/.claude/mcp-servers.runtime.json"
    ".claude/statusline.py:$SYNC_HOME/.claude/statusline.py"
    ".claude/subagent-statusline.py:$SYNC_HOME/.claude/subagent-statusline.py"
)

echo "coding-agent-environment — sync-check"
echo "repo:  $REPO_ROOT"
echo "live:  $SYNC_HOME"
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

    if [[ "$repo_rel" == ".claude/settings.json" ]]; then
        # Native plugin installs add root fields; compare the owned settings only.
        if python3 - "$repo_path" "$live_path" <<'PYEOF'
import json, sys
try:
    desired, live = [json.load(open(p)) for p in sys.argv[1:]]
    ok = all(live.get(k) == v for k, v in desired.items() if k != "_scope_note")
except (OSError, ValueError):
    ok = False
sys.exit(0 if ok else 1)
PYEOF
        then
            IDENTICAL=$((IDENTICAL + 1))
        else
            echo "DIFFERS        $repo_rel (owned settings)"
            DIFFER=$((DIFFER + 1))
        fi
        continue
    fi

    if diff -q "$repo_path" "$live_path" >/dev/null 2>&1; then
        IDENTICAL=$((IDENTICAL + 1))
    else
        echo "DIFFERS        $repo_rel"
        echo "               repo:  $repo_path"
        echo "               live:  $live_path"
        DIFFER=$((DIFFER + 1))
    fi
done

# Live directories with a tracked repo counterpart but no per-file mapping
# above (e.g. hooks/, skills/) — checked by directory-listing diff instead.
for dir in ".claude/hooks" ".claude/skills" "bin"; do
    repo_dir="$REPO_ROOT/$dir"
    case "$dir" in
        bin) live_dir="$SYNC_HOME/.local/bin" ;;
        *)   live_dir="$SYNC_HOME/$dir" ;;
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

# The split global-only reference is merged into live settings, not copied.
if python3 - "$REPO_ROOT/.claude/settings.global-only.json" "$SYNC_HOME/.claude/settings.json" <<'PYEOF'
import json, sys
try:
    desired, live = [json.load(open(p)) for p in sys.argv[1:]]
    plugin = "requirement-zero@requirement-zero"
    marketplace = "requirement-zero"
    ok = (live.get("enabledPlugins", {}).get(plugin) is True
          and live.get("extraKnownMarketplaces", {}).get(marketplace)
          == desired["extraKnownMarketplaces"][marketplace])
except (OSError, ValueError, KeyError):
    ok = False
sys.exit(0 if ok else 1)
PYEOF
then
    IDENTICAL=$((IDENTICAL + 1))
else
    echo "DIFFERS        canonical subtraction plugin desired state"
    echo "               merge plugin/marketplace entries per README installation instructions"
    DIFFER=$((DIFFER + 1))
fi

if bash "$REPO_ROOT/scripts/profile-install.sh" --global --check; then
    IDENTICAL=$((IDENTICAL + 1))
else
    DIFFER=$((DIFFER + 1))
fi

echo ""
echo "Summary: $IDENTICAL identical, $DIFFER differ, $LIVE_MISSING missing-on-live"
echo ""
echo "This is a REPORT, not a sync. No files were copied in either direction."
echo "For managed Codex state: scripts/profile-install.sh --global"
echo "For other surfaces, review ownership before copying:"
echo "  repo -> live:  cp <repo-path> <live-path>   (apply this repo's desired state)"
echo "  live -> repo:  cp <live-path> <repo-path>    (capture a live change into the repo, then commit)"

if [[ "$DIFFER" -gt 0 || "$LIVE_MISSING" -gt 0 ]]; then
    exit 1
fi
exit 0
