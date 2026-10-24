#!/usr/bin/env bash
# Rehearse the whole SPE-108 live migration in a SCRATCH HOME built from copies of the operator's real files.
# Nothing under the real HOME is written (the scratch tree is the only HOME the scripts see). Prints
# PASS/FAIL lines and never any policy text; exit status is non-zero on the first failed assertion.
#
# Usage: rehearse.sh --repo CHECKOUT_AT_origin/main --ledger LEDGER [--mode keep|retire] [--keep-dir DIR]
#   keep    default: leave the legacy sections in place and rewrite only the ledger-named superseded lines
#   retire  also remove the four legacy sections (lossless gate must pass)
# Steps: copy -> Claude install -> overlay -> install -> (rewrite | retire) -> Codex install -> effective
# checks -> second apply is a no-op -> rollback restores every pre-migration byte.
set -euo pipefail

REPO="" LEDGER="" MODE=keep KEEP=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo) REPO="$2"; shift 2 ;;
        --ledger) LEDGER="$2"; shift 2 ;;
        --mode) MODE="$2"; shift 2 ;;
        --keep-dir) KEEP="$2"; shift 2 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done
[[ -d "$REPO" && -f "$LEDGER" && ( "$MODE" == keep || "$MODE" == retire ) ]] || { echo "usage: see header" >&2; exit 2; }
REAL_HOME="$HOME"
TOOLS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SIM="${KEEP:-$(mktemp -d)}"
[[ -z "$KEEP" ]] || mkdir -p "$SIM"
[[ -z "$(ls -A "$SIM")" ]] || { echo "scratch dir must be empty" >&2; exit 2; }
BASE="$SIM/.config/coding-agent-environment/local/baseline/claude"
mkdir -p -m 700 "$SIM/.claude" "$SIM/.codex" "$BASE"

pass() { echo "PASS $1"; }
fail() { echo "FAIL $1"; exit 1; }
tree_sha() { (cd "$1" && find . -type f ! -path './backups/*' -print0 | sort -z | xargs -0 shasum -a 256 | shasum -a 256 | cut -c1-16); }

# 1. copy (symlinks preserved, never followed)
cp -p "$REAL_HOME/.claude/CLAUDE.md" "$SIM/.claude/"
cp -PR "$REAL_HOME/.claude/rules" "$SIM/.claude/rules"
cp -PR "$REAL_HOME/.claude/skills" "$SIM/.claude/skills"
cp -p "$REAL_HOME/.codex/AGENTS.md" "$SIM/.codex/"
cp -PR "$REAL_HOME/.codex/skills" "$SIM/.codex/skills"
[[ ! -d "$REAL_HOME/.agents" ]] || cp -PR "$REAL_HOME/.agents" "$SIM/.agents"  # pinned shared skills: no network fetch
cp -p "$LEDGER" "$BASE/policy-ledger.tsv"
chmod 600 "$BASE/policy-ledger.tsv"
mkdir -p -m 700 "$SIM/before"
cp -p "$SIM/.claude/CLAUDE.md" "$SIM/before/CLAUDE.md"
cp -p "$SIM/.codex/AGENTS.md" "$SIM/before/AGENTS.md"
before_claude="$(tree_sha "$SIM/.claude")" before_codex="$(tree_sha "$SIM/.codex")" before_agents="$(tree_sha "$SIM/.agents" 2>/dev/null || echo none)"
(cd "$SIM" && find .claude .codex -type f ! -path '*/backups/*' | sort) > "$SIM/before/files.txt"

export HOME="$SIM" CODING_AGENT_SYNC_HOME="$SIM"
unset CODEX_HOME CODING_AGENT_LOCAL_DIR CODING_AGENT_LOCAL_BASELINE_DIR
cd "$REPO"
LED="$BASE/policy-ledger.tsv"

# 2. apply in the documented order
scripts/profile-install.sh --claude-skills --dry-run >/dev/null
scripts/profile-install.sh --claude-skills >/dev/null
python3 "$TOOLS/build-overlay.py" --repo "$REPO" --ledger "$LED" >/dev/null
scripts/profile-install.sh --claude-skills >/dev/null
scripts/profile-install.sh --claude-skills --check | tail -1 | grep -q 'drift: 0' && pass "claude managed drift 0" || fail "claude managed drift"
if [[ "$MODE" == retire ]]; then
    sha="$(shasum -a 256 "$SIM/.claude/CLAUDE.md" | cut -d' ' -f1)"
    python3 "$TOOLS/retire-sections.py" --repo "$REPO" --ledger "$LED" --apply --expect-sha256 "$sha" >/dev/null || fail "retire (lossless gate)"
else
    sha="$(shasum -a 256 "$SIM/.claude/CLAUDE.md" | cut -d' ' -f1)"
    python3 "$TOOLS/rewrite-lines.py" --file "$SIM/.claude/CLAUDE.md" --ledger "$LED" --expect-sha256 "$sha" --apply >/dev/null || fail "rewrite lines"
fi
scripts/profile-install.sh --global >/dev/null
scripts/profile-install.sh --global --check | tail -1 | grep -q 'drift: 0' && pass "codex managed drift 0" || fail "codex managed drift"

# 3. effective final instruction files
python3 "$TOOLS/verify-effective.py" --host claude --before "$SIM/before/CLAUDE.md" --after "$SIM/.claude/CLAUDE.md" \
    --skills-dir "$SIM/.claude/skills" --ledger "$LED" --repo "$REPO" | sed 's/^/claude: /' | tee "$SIM/claude-effective.txt" | grep -E '^claude: (FAIL|info)' || true
! grep -q 'claude: FAIL' "$SIM/claude-effective.txt" && pass "claude effective checks" || fail "claude effective checks"
python3 "$TOOLS/verify-effective.py" --host codex --before "$SIM/before/AGENTS.md" --after "$SIM/.codex/AGENTS.md" \
    --skills-dir "$SIM/.codex/skills" --ledger "$LED" --repo "$REPO" | sed 's/^/codex: /' | tee "$SIM/codex-effective.txt" | grep -E '^codex: (FAIL|info)' || true
! grep -q 'codex: FAIL' "$SIM/codex-effective.txt" && pass "codex effective checks" || fail "codex effective checks"
python3 "$TOOLS/../claude-clause-drift.py" | tail -1 | grep -q 'Claude clause drift: 0' && pass "clause drift 0 (ledger dispositioned)" || fail "clause drift"

# 4. idempotent second apply
after_claude="$(tree_sha "$SIM/.claude")" after_codex="$(tree_sha "$SIM/.codex")"
scripts/profile-install.sh --claude-skills >/dev/null
scripts/profile-install.sh --global >/dev/null
python3 "$TOOLS/build-overlay.py" --repo "$REPO" --ledger "$LED" >/dev/null 2>&1 || true
[[ "$(tree_sha "$SIM/.claude")" == "$after_claude" && "$(tree_sha "$SIM/.codex")" == "$after_codex" ]] && pass "second apply changes nothing" || fail "second apply changed files"

# 5. rollback, exactly as the operator packet says: installer "Prior state" dirs newest first, then the byte-exact
#    pre-migration copies of the two instruction files, then delete files that did not exist before
restore() { # $1 = installer backup dir; paths inside it are relative to HOME (e.g. .claude/skills/x/SKILL.md)
    (cd "$1" && find . -type f -print0) | while IFS= read -r -d '' rel; do
        rel="${rel#./}"
        mkdir -p "$(dirname "$SIM/$rel")" && cp -p "$1/$rel" "$SIM/$rel"
    done
}
for host in .claude .codex; do
    while IFS= read -r dir; do restore "$dir"; done < <(find "$SIM/$host/backups/coding-agent-environment" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort -r)
done
cp -p "$SIM/before/CLAUDE.md" "$SIM/.claude/CLAUDE.md"
cp -p "$SIM/before/AGENTS.md" "$SIM/.codex/AGENTS.md"
(cd "$SIM" && find .claude .codex -type f ! -path '*/backups/*' | sort) | comm -13 "$SIM/before/files.txt" - | while read -r created; do rm -f "$SIM/$created"; done
rm -rf "$SIM/.config/coding-agent-environment/local/baseline/claude/CLAUDE.private.md"
[[ "$(tree_sha "$SIM/.claude")" == "$before_claude" && "$(tree_sha "$SIM/.codex")" == "$before_codex" ]] && pass "rollback restores every pre-migration byte" || fail "rollback is not byte exact"
echo "scratch tree: $SIM"
