#!/usr/bin/env bash
# Test suite for bin/coding-agent-profile, bin/ca-claude, bin/ca-codex.
# Pure bash, no dependencies beyond python3 (for JSON parsing). Offline —
# no CLI/API calls, everything runs against fixtures built in a temp dir.
#
# Usage: bash tests/test-profile-resolver.sh
set -uo pipefail  # not -e: we want to keep running after assertion failures

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="$REPO_ROOT/bin/coding-agent-profile"
CA_CLAUDE="$REPO_ROOT/bin/ca-claude"
CA_CODEX="$REPO_ROOT/bin/ca-codex"

WORK="$(mktemp -d)"
WORK="$(cd -P -- "$WORK" && pwd)"  # physical path — assertions compare against resolver output, which is always physical
trap 'rm -rf "$WORK"' EXIT

# Dry-run launcher assertions need resolvable binaries, never real host clients.
mkdir -p "$WORK/cli-stubs"
for client in claude codex; do
    printf '#!/usr/bin/env bash\nexit 99\n' > "$WORK/cli-stubs/$client"
    chmod +x "$WORK/cli-stubs/$client"
done
export PATH="$WORK/cli-stubs:$PATH"

PASS=0
FAIL=0

assert_eq() {
    local desc="$1" expected="$2" actual="$3"
    if [[ "$expected" == "$actual" ]]; then
        PASS=$((PASS + 1))
        echo "ok   - $desc"
    else
        FAIL=$((FAIL + 1))
        echo "FAIL - $desc"
        echo "       expected: $expected"
        echo "       actual:   $actual"
    fi
}

assert_contains() {
    local desc="$1" haystack="$2" needle="$3"
    if [[ "$haystack" == *"$needle"* ]]; then
        PASS=$((PASS + 1))
        echo "ok   - $desc"
    else
        FAIL=$((FAIL + 1))
        echo "FAIL - $desc"
        echo "       expected to contain: $needle"
        echo "       actual: $haystack"
    fi
}

assert_not_contains() {
    local desc="$1" haystack="$2" needle="$3"
    if [[ "$haystack" != *"$needle"* ]]; then
        PASS=$((PASS + 1))
        echo "ok   - $desc"
    else
        FAIL=$((FAIL + 1))
        echo "FAIL - $desc"
        echo "       expected NOT to contain: $needle"
        echo "       actual: $haystack"
    fi
}

field() { # field JSON_STR KEY -> value via python (dotted key path)
    python3 -c "
import json, sys
d = json.loads(sys.argv[1])
for k in sys.argv[2].split('.'):
    d = d[k] if d is not None else None
print(d if d is not None else '')
" "$1" "$2"
}

echo "=== Case 1: profile root is cwd ==="
mkdir -p "$WORK/c1/root"
echo "acme" > "$WORK/c1/root/.coding-agent-profile"
mkdir -p "$WORK/c1/overlay/acme"
out=$(CODING_AGENT_PROFILE_DIR="$WORK/c1/overlay" "$BIN" resolve --cwd "$WORK/c1/root")
assert_contains "case1: status=ok" "$out" "status=ok"
assert_contains "case1: root == cwd" "$out" "root=$WORK/c1/root"

echo "=== Case 2: git repo nested inside profile root ==="
mkdir -p "$WORK/c2/root/repo/.git"
echo "acme" > "$WORK/c2/root/.coding-agent-profile"
mkdir -p "$WORK/c2/overlay/acme"
out=$(CODING_AGENT_PROFILE_DIR="$WORK/c2/overlay" "$BIN" resolve --cwd "$WORK/c2/root/repo")
assert_contains "case2: root is profile root, not repo root" "$out" "root=$WORK/c2/root"

echo "=== Case 3: deeply nested child ==="
mkdir -p "$WORK/c3/root/repo/a/b/c/d"
echo "acme" > "$WORK/c3/root/.coding-agent-profile"
mkdir -p "$WORK/c3/overlay/acme"
json=$(CODING_AGENT_PROFILE_DIR="$WORK/c3/overlay" "$BIN" explain --json --cwd "$WORK/c3/root/repo/a/b/c/d")
assert_eq "case3: status ok" "ok" "$(field "$json" profile.status)"
scanned=$(field "$json" profile.ancestors_scanned)
[[ "$scanned" -ge 5 ]] && { PASS=$((PASS+1)); echo "ok   - case3: ancestors_scanned >= 5 ($scanned)"; } || { FAIL=$((FAIL+1)); echo "FAIL - case3: ancestors_scanned >= 5, got $scanned"; }

echo "=== Case 4: no marker anywhere ==="
mkdir -p "$WORK/c4/x/y/z"
out=$("$BIN" resolve --cwd "$WORK/c4/x/y/z"); rc=$?
assert_contains "case4: status=none" "$out" "status=none"
assert_eq "case4: exit 0" "0" "$rc"
dry=$(CA_DRY_RUN=1 "$CA_CLAUDE" --version 2>&1)
assert_eq "case4: ca-claude passes argv through unmodified" "$(command -v claude 2>/dev/null)
--version" "$dry"

echo "=== Case 5: nested/conflicting markers (nearest wins) ==="
mkdir -p "$WORK/c5/root/mid/cwd"
echo "outer" > "$WORK/c5/root/.coding-agent-profile"
echo "inner" > "$WORK/c5/root/mid/.coding-agent-profile"
mkdir -p "$WORK/c5/overlay/inner" "$WORK/c5/overlay/outer"
json=$(CODING_AGENT_PROFILE_DIR="$WORK/c5/overlay" "$BIN" explain --json --cwd "$WORK/c5/root/mid/cwd")
assert_eq "case5: nearest (inner) wins" "inner" "$(field "$json" profile.name)"
shadowed=$(field "$json" profile.shadowed)
assert_contains "case5: outer reported as shadowed" "$shadowed" "$WORK/c5/root"

echo "=== Case 6a: malformed - empty file ==="
mkdir -p "$WORK/c6a"
touch "$WORK/c6a/.coding-agent-profile"
out=$("$BIN" resolve --cwd "$WORK/c6a"); rc=$?
assert_contains "case6a: status=malformed" "$out" "status=malformed"
assert_eq "case6a: exit 3" "3" "$rc"

echo "=== Case 6b: malformed - two names ==="
mkdir -p "$WORK/c6b"
printf 'first\nsecond\n' > "$WORK/c6b/.coding-agent-profile"
out=$("$BIN" resolve --cwd "$WORK/c6b"); rc=$?
assert_contains "case6b: status=malformed" "$out" "status=malformed"
assert_eq "case6b: exit 3" "3" "$rc"

echo "=== Case 6c: malformed - invalid name (path traversal attempt) ==="
mkdir -p "$WORK/c6c/overlay"
mkdir -p "$WORK/c6c/scope"
echo "../evil" > "$WORK/c6c/scope/.coding-agent-profile"
out=$(CODING_AGENT_PROFILE_DIR="$WORK/c6c/overlay" "$BIN" resolve --cwd "$WORK/c6c/scope"); rc=$?
assert_contains "case6c: status=malformed" "$out" "status=malformed"
assert_eq "case6c: exit 3" "3" "$rc"
assert_not_contains "case6c: overlay path never contains ../evil" "$out" "evil"

echo "=== Case 7: marker names an unknown profile ==="
mkdir -p "$WORK/c7/scope" "$WORK/c7/overlay"
echo "ghost" > "$WORK/c7/scope/.coding-agent-profile"
out=$(CODING_AGENT_PROFILE_DIR="$WORK/c7/overlay" "$BIN" resolve --cwd "$WORK/c7/scope"); rc=$?
assert_contains "case7: status=unresolved" "$out" "status=unresolved"
assert_eq "case7: exit 4" "4" "$rc"

echo "=== Case 8: managed setting must win (informational, non-bypass) ==="
mkdir -p "$WORK/c8/root" "$WORK/c8/overlay/acme/claude" "$WORK/c8/managed"
echo "acme" > "$WORK/c8/root/.coding-agent-profile"
echo '{"model":"opus"}' > "$WORK/c8/overlay/acme/claude/settings.json"
echo '{"model":"sonnet"}' > "$WORK/c8/managed/managed-settings.json"
text=$(CODING_AGENT_PROFILE_DIR="$WORK/c8/overlay" CODING_AGENT_MANAGED_SETTINGS_PATH="$WORK/c8/managed/managed-settings.json" "$BIN" explain --cwd "$WORK/c8/root")
assert_contains "case8: managed-settings reported present" "$text" "managed-settings.json"
dry=$(CODING_AGENT_PROFILE_DIR="$WORK/c8/overlay" CA_DRY_RUN=1 "$CA_CLAUDE" --cwd "$WORK/c8/root" 2>&1)
assert_not_contains "case8: no --setting-sources in emitted argv" "$dry" "--setting-sources"
assert_not_contains "case8: no CLAUDE_CONFIG_DIR-style flag in emitted argv" "$dry" "--dangerously"

echo "=== Case 9: cwd reached through a symlink into the profile tree ==="
mkdir -p "$WORK/c9/real/root"
echo "acme" > "$WORK/c9/real/root/.coding-agent-profile"
mkdir -p "$WORK/c9/overlay/acme"
ln -s "$WORK/c9/real" "$WORK/c9/link"
out=$(CODING_AGENT_PROFILE_DIR="$WORK/c9/overlay" "$BIN" resolve --cwd "$WORK/c9/link/root"); rc=$?
assert_contains "case9: resolves through symlink" "$out" "status=ok"

echo "=== Case 10: marker at HOME is ignored by default ==="
out=$(HOME="$WORK/c10-home" "$BIN" resolve --cwd "$WORK/c10-home" 2>&1)
mkdir -p "$WORK/c10-home"
echo "should-not-apply" > "$WORK/c10-home/.coding-agent-profile"
out=$(HOME="$WORK/c10-home" "$BIN" resolve --cwd "$WORK/c10-home")
assert_contains "case10: HOME marker ignored, status=none" "$out" "status=none"

echo "=== Case 11: ca-claude argv, profile present / absent ==="
dry_absent=$(CA_DRY_RUN=1 "$CA_CLAUDE" foo 2>&1)
assert_eq "case11a: no profile -> unmodified argv" "$(command -v claude 2>/dev/null)
foo" "$dry_absent"
mkdir -p "$WORK/c11/root" "$WORK/c11/overlay/acme/claude"
echo "acme" > "$WORK/c11/root/.coding-agent-profile"
echo '{}' > "$WORK/c11/overlay/acme/claude/settings.json"
cd "$WORK/c11/root" || exit 1
dry_present=$(CODING_AGENT_PROFILE_DIR="$WORK/c11/overlay" CA_DRY_RUN=1 "$CA_CLAUDE" foo 2>&1)
cd "$REPO_ROOT" || exit 1
assert_contains "case11b: profile present -> --settings injected" "$dry_present" "--settings"
assert_contains "case11b: user argv preserved" "$dry_present" "foo"

echo "=== Case 12: ca-codex argv with no profile has no marker override ==="
dry=$(CA_DRY_RUN=1 "$CA_CODEX" exec bar 2>&1)
assert_not_contains "case12: no project_root_markers override when no profile" "$dry" "project_root_markers"
assert_contains "case12: user argv preserved" "$dry" "bar"

echo "=== Case 13: both profile-root and overlay CLAUDE.md present -> no double-inject ==="
mkdir -p "$WORK/c13/root" "$WORK/c13/overlay/acme/claude"
echo "acme" > "$WORK/c13/root/.coding-agent-profile"
echo "# root instructions" > "$WORK/c13/root/CLAUDE.md"
echo "# overlay instructions" > "$WORK/c13/overlay/acme/claude/CLAUDE.md"
cd "$WORK/c13/root" || exit 1
dry=$(CODING_AGENT_PROFILE_DIR="$WORK/c13/overlay" CA_DRY_RUN=1 "$CA_CLAUDE" 2>&1)
cd "$REPO_ROOT" || exit 1
assert_not_contains "case13: --append-system-prompt-file NOT injected when root CLAUDE.md exists" "$dry" "--append-system-prompt-file"
explain=$(CODING_AGENT_PROFILE_DIR="$WORK/c13/overlay" "$BIN" explain --cwd "$WORK/c13/root")
assert_contains "case13: --explain warns about suppressed overlay CLAUDE.md" "$explain" "suppressed"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[[ "$FAIL" -eq 0 ]]
