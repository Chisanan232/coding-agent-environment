#!/usr/bin/env bash
# Rehearse the whole SPE-108 live migration in a SCRATCH HOME built from copies of the operator's real files.
# It reads the real files and writes only under the scratch tree (HOME and every install-location override point
# there). The one exception is the Codex installer, which may fetch pinned shared skills when ~/.agents is absent. Prints
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
        --keep-dir) [[ -n "${2:-}" ]] || { echo "refusing: --keep-dir needs a non-empty path" >&2; exit 2; }; KEEP="$2"; shift 2 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done
[[ -d "$REPO" && -f "$LEDGER" && ( "$MODE" == keep || "$MODE" == retire ) ]] || { echo "usage: see header" >&2; exit 2; }
# Absolute, symlink-resolved paths first: the scripts below run with HOME=$SIM and from inside $REPO, so a
# relative path would otherwise resolve somewhere else.
REAL_HOME="$(cd "$HOME" && pwd -P)"
TOOLS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO="$(cd "$REPO" && pwd -P)"
LEDGER="$(cd "$(dirname "$LEDGER")" && pwd -P)/$(basename "$LEDGER")"
if [[ -n "$KEEP" ]]; then
    case "/$KEEP/" in */../*) echo "refusing: --keep-dir must not contain .." >&2; exit 2 ;; esac
    # Resolve through the nearest existing parent, guard, and only then create anything.
    parent="$KEEP"; tail=""
    while [[ ! -d "$parent" ]]; do tail="/$(basename "$parent")$tail"; parent="$(dirname "$parent")"; done
    SIM="$(cd "$parent" && pwd -P)$tail"
else
    # mktemp: private (0700), unpredictable name, collision-proof; guarded and removed below if refused.
    SIM="$(mktemp -d "$(cd "${TMPDIR:-/tmp}" && pwd -P)/rehearse.XXXXXX")"
    SIM="$(cd "$SIM" && pwd -P)"
    AUTO_SIM=1
fi
case "$SIM/" in
    "$REAL_HOME/.claude/"*|"$REAL_HOME/.codex/"*|"$REAL_HOME/.agents/"*|"$REAL_HOME/"|"$REPO/"*)
        [[ -z "${AUTO_SIM:-}" ]] || rmdir "$SIM"
        echo "refusing: scratch dir must not be inside the real HOME config or the checkout" >&2; exit 2 ;;
esac
mkdir -p "$SIM"
# Check emptiness before touching the mode: an operator-supplied non-empty dir must be left exactly as found.
[[ -z "$(ls -A "$SIM")" ]] || { echo "scratch dir must be empty" >&2; exit 2; }
chmod 700 "$SIM"
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
# Pinned shared skills. Without a copy the Codex installer may fetch them over the network (npx) into the scratch HOME.
[[ ! -d "$REAL_HOME/.agents" ]] || cp -PR "$REAL_HOME/.agents" "$SIM/.agents"
cp -p "$LEDGER" "$BASE/policy-ledger.tsv"
chmod 600 "$BASE/policy-ledger.tsv"
mkdir -p -m 700 "$SIM/before"
cp -p "$SIM/.claude/CLAUDE.md" "$SIM/before/CLAUDE.md"
cp -p "$SIM/.codex/AGENTS.md" "$SIM/before/AGENTS.md"
before_claude="$(tree_sha "$SIM/.claude")" before_codex="$(tree_sha "$SIM/.codex")" before_agents="$(tree_sha "$SIM/.agents" 2>/dev/null || echo none)"
(cd "$SIM" && find .claude .codex -type f ! -path '*/backups/*' | sort) > "$SIM/before/files.txt"

export HOME="$SIM" CODING_AGENT_SYNC_HOME="$SIM"
# Every location override the installers read, so none can point at the operator's real files.
unset CODEX_HOME CODING_AGENT_LOCAL_DIR CODING_AGENT_LOCAL_BASELINE_DIR CODING_AGENT_PROFILE_DIR XDG_CONFIG_HOME \
    CODING_AGENT_MANAGED_SETTINGS_PATH CODING_AGENT_CODEX_MANAGED_CONFIG_PATH
export INSTALL_BIN_DIR="$SIM/.local/bin"
cd "$REPO"
LED="$BASE/policy-ledger.tsv"

MODE_FLAG=""
[[ "$MODE" != retire ]] || MODE_FLAG="--strict"

# 2. apply in the documented order
scripts/profile-install.sh --claude-skills --dry-run >/dev/null
scripts/profile-install.sh --claude-skills >/dev/null
# The retained-legacy overlay section only exists for the retire path; on the keep path the originals stay in place
# and copying them again would just add duplicate lines to every session.
if [[ "$MODE" == retire ]]; then OVERLAY_LEDGER=(--ledger "$LED"); else OVERLAY_LEDGER=(); fi
python3 "$TOOLS/build-overlay.py" --repo "$REPO" ${OVERLAY_LEDGER[@]+"${OVERLAY_LEDGER[@]}"} >/dev/null
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
python3 "$TOOLS/verify-effective.py" ${MODE_FLAG:+$MODE_FLAG} --host claude --before "$SIM/before/CLAUDE.md" --after "$SIM/.claude/CLAUDE.md" \
    --skills-dir "$SIM/.claude/skills" --ledger "$LED" --repo "$REPO" | sed 's/^/claude: /' | tee "$SIM/claude-effective.txt" | grep -E '^claude: (FAIL|info)' || true
! grep -q 'claude: FAIL' "$SIM/claude-effective.txt" && pass "claude effective checks" || fail "claude effective checks"
python3 "$TOOLS/verify-effective.py" --host codex --before "$SIM/before/AGENTS.md" --after "$SIM/.codex/AGENTS.md" \
    --skills-dir "$SIM/.codex/skills" --ledger "$LED" --repo "$REPO" | sed 's/^/codex: /' | tee "$SIM/codex-effective.txt" | grep -E '^codex: (FAIL|info)' || true
! grep -q 'codex: FAIL' "$SIM/codex-effective.txt" && pass "codex effective checks" || fail "codex effective checks"
python3 "$TOOLS/../claude-clause-drift.py" | tail -1 | grep -q 'Claude clause drift: 0' && pass "clause drift 0 (ledger dispositioned)" || fail "clause drift"

# what the migration costs every session
python3 - "$SIM/before/CLAUDE.md" "$SIM/.claude/CLAUDE.md" "$SIM/before/AGENTS.md" "$SIM/.codex/AGENTS.md" <<'PY'
import sys
for label, a, b in (("claude", sys.argv[1], sys.argv[2]), ("codex", sys.argv[3], sys.argv[4])):
    print("info %s context: %d -> %d bytes" % (label, len(open(a, "rb").read()), len(open(b, "rb").read())))
PY

# 4. idempotent second apply
after_claude="$(tree_sha "$SIM/.claude")" after_codex="$(tree_sha "$SIM/.codex")"
scripts/profile-install.sh --claude-skills >/dev/null
scripts/profile-install.sh --global >/dev/null
python3 "$TOOLS/build-overlay.py" --repo "$REPO" ${OVERLAY_LEDGER[@]+"${OVERLAY_LEDGER[@]}"} >/dev/null 2>&1 || true
[[ "$(tree_sha "$SIM/.claude")" == "$after_claude" && "$(tree_sha "$SIM/.codex")" == "$after_codex" ]] && pass "second apply changes nothing" || fail "second apply changed files"

# keep the migrated instruction files for inspection (the rollback below restores the originals)
mkdir -p -m 700 "$SIM/final"
cp -p "$SIM/.claude/CLAUDE.md" "$SIM/final/CLAUDE.md"
cp -p "$SIM/.codex/AGENTS.md" "$SIM/final/AGENTS.md"

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
