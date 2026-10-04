#!/usr/bin/env bash
# Capability lifecycle tests use disposable homes and never touch live Codex state.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL="$ROOT/scripts/profile-install.sh"
DESIRED="$ROOT/codex/capabilities-readonly.config.toml"
PASS=0

new_home() {
    local fixture
    fixture="$(mktemp -d)"
    mkdir -p "$fixture/.codex"
    printf 'model = "user-owned"\n' > "$fixture/.codex/config.toml"
    printf 'private-auth-state\n' > "$fixture/.codex/auth.json"
    printf '%s\n' "$fixture"
}

assert_eq() {
    local expected="$1" actual="$2" message="$3"
    if [[ "$actual" != "$expected" ]]; then
        printf 'FAIL: %s (expected %q, got %q)\n' "$message" "$expected" "$actual" >&2
        exit 1
    fi
}

assert_contains() {
    local haystack="$1" needle="$2" message="$3"
    if [[ "$haystack" != *"$needle"* ]]; then
        printf 'FAIL: %s (missing %q)\n' "$message" "$needle" >&2
        exit 1
    fi
}

test_apply_installs_only_external_profile() {
    local fixture
    fixture="$(new_home)"
    CODING_AGENT_SYNC_HOME="$fixture" bash "$INSTALL" --capabilities >/dev/null
    cmp -s "$DESIRED" "$fixture/.codex/capabilities-readonly.config.toml"
    assert_eq "600" "$(python3 -c 'import os, stat, sys; print(oct(stat.S_IMODE(os.stat(sys.argv[1]).st_mode))[2:])' "$fixture/.codex/capabilities-readonly.config.toml")" "installed profile mode"
    assert_eq 'model = "user-owned"' "$(cat "$fixture/.codex/config.toml")" "base config remains untouched"
    assert_eq 'private-auth-state' "$(cat "$fixture/.codex/auth.json")" "auth state remains untouched"
    PASS=$((PASS + 1))
}

test_dry_run_reports_without_mutation() {
    local fixture output
    fixture="$(new_home)"
    output="$(CODING_AGENT_SYNC_HOME="$fixture" bash "$INSTALL" --capabilities --dry-run)"
    [[ ! -e "$fixture/.codex/capabilities-readonly.config.toml" ]]
    assert_contains "$output" 'Managed capability drift: 1' "dry-run drift report"
    PASS=$((PASS + 1))
}

test_apply_installs_only_external_profile
test_dry_run_reports_without_mutation
printf 'capability profile tests: %d passed\n' "$PASS"
