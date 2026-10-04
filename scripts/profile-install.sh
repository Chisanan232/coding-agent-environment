#!/usr/bin/env bash
# profile-install.sh - deterministic setup for directory-scoped coding-agent
# profiles. Idempotent: safe to re-run.
#
# What it does:
#   1. Symlinks bin/coding-agent-profile, bin/coding-agent-profile-explain,
#      bin/ca-claude, bin/ca-codex into ~/.local/bin (or $INSTALL_BIN_DIR).
#   2. For each profile directory under $CODING_AGENT_PROFILE_DIR that has a
#      codex/config.toml, symlinks it to
#      $CODEX_DIR/<profile-name>.config.toml (Codex only reads --profile
#      tomls from $CODEX_HOME — see docs/PROFILES.md).
#
# Refuses to overwrite a file that is not already a symlink pointing at the
# expected target — never clobbers unrelated user state.
#
# Usage:
#   ./scripts/profile-install.sh [--dry-run]
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_BIN_DIR="${INSTALL_BIN_DIR:-$HOME/.local/bin}"
PROFILE_DIR="${CODING_AGENT_PROFILE_DIR:-$HOME/.coding-agent-profiles}"
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
DRY_RUN=0
GLOBAL=0
CHECK=0

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --global) GLOBAL=1 ;;
        --check) CHECK=1 ;;
        *) echo "profile-install.sh: unknown option: $arg" >&2; exit 2 ;;
    esac
done

if (( GLOBAL )); then
    python3 - "$REPO_ROOT" "${CODING_AGENT_SYNC_HOME:-$HOME}" "$DRY_RUN" "$CHECK" <<'PYGLOBAL'
import datetime
import os
from pathlib import Path
import sys
import tempfile

root, live = map(Path, sys.argv[1:3])
dry, check = map(int, sys.argv[3:])
source = root / '.claude/skills/evidence-first-briefing/SKILL.md'
target = live / '.codex/skills/evidence-first-briefing/SKILL.md'
expected = source.read_bytes()
if target.is_symlink() or target.parent.is_symlink():
    raise SystemExit('Refusing to replace a symlinked skill; reconcile ownership first.')
changed = not target.exists() or target.read_bytes() != expected
print(f"{'DIFFERS' if changed else 'IDENTICAL'} {target} <- {source.relative_to(root)}")
if changed and not (dry or check):
    if target.exists():
        backup = live / '.codex/backups/coding-agent-environment' / datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        backup.mkdir(parents=True, mode=0o700)
        saved = backup / 'evidence-first-briefing.SKILL.md'
        saved.write_bytes(target.read_bytes())
        saved.chmod(0o600)
        print(f'Prior state: {saved}')
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=target.parent)
    with os.fdopen(fd, 'wb') as f:
        f.write(expected)
    os.chmod(name, 0o644)
    os.replace(name, target)
    assert target.read_bytes() == expected
print(f'Managed Codex drift: {int(changed and (dry or check))}')
raise SystemExit(int(changed and check))
PYGLOBAL
    exit $?
fi
if (( CHECK )); then
    echo '--check requires --global' >&2
    exit 2
fi

info() { echo "[profile-install] $*"; }

link_bin() {
    local name="$1"
    local src="$REPO_ROOT/bin/$name"
    local dst="$INSTALL_BIN_DIR/$name"

    if [[ ! -f "$src" ]]; then
        echo "[profile-install] ERROR: $src does not exist" >&2
        return 1
    fi

    if [[ -L "$dst" ]]; then
        local current
        current="$(readlink "$dst")"
        if [[ "$current" == "$src" ]]; then
            info "$name already linked correctly"
            return 0
        fi
        echo "[profile-install] WARN: $dst is a symlink to a different target ($current) — skipping, remove it manually if you want it replaced" >&2
        return 1
    fi

    if [[ -e "$dst" ]]; then
        echo "[profile-install] WARN: $dst exists and is not a symlink — refusing to overwrite" >&2
        return 1
    fi

    if (( DRY_RUN )); then
        info "would symlink $src -> $dst"
    else
        mkdir -p "$INSTALL_BIN_DIR"
        ln -s "$src" "$dst"
        info "linked $name -> $dst"
    fi
}

link_codex_profile() {
    local profile_name="$1"
    local src="$PROFILE_DIR/$profile_name/codex/config.toml"
    local dst="$CODEX_DIR/$profile_name.config.toml"

    [[ -f "$src" ]] || return 0

    if [[ -L "$dst" ]]; then
        local current
        current="$(readlink "$dst")"
        if [[ "$current" == "$src" ]]; then
            info "codex profile '$profile_name' already linked correctly"
            return 0
        fi
        echo "[profile-install] WARN: $dst is a symlink to a different target ($current) — skipping" >&2
        return 1
    fi

    if [[ -e "$dst" ]]; then
        echo "[profile-install] WARN: $dst exists and is not a symlink — refusing to overwrite" >&2
        return 1
    fi

    if (( DRY_RUN )); then
        info "would symlink $src -> $dst"
    else
        mkdir -p "$CODEX_DIR"
        ln -s "$src" "$dst"
        info "linked codex profile '$profile_name' -> $dst"
    fi
}

info "installing launcher symlinks into $INSTALL_BIN_DIR"
for tool in coding-agent-profile coding-agent-profile-explain ca-claude ca-codex; do
    link_bin "$tool" || true
done

if [[ -d "$PROFILE_DIR" ]]; then
    info "scanning $PROFILE_DIR for Codex profile configs"
    for dir in "$PROFILE_DIR"/*/; do
        [[ -d "$dir" ]] || continue
        name="$(basename "$dir")"
        link_codex_profile "$name" || true
    done
else
    info "$PROFILE_DIR does not exist yet — nothing to link for Codex. Create it and re-run when you add a profile."
fi

info "done. Add $INSTALL_BIN_DIR to PATH if it isn't already, then run:"
info "  coding-agent-profile --help"
info "  ca-claude / ca-codex   (drop-in replacements for claude/codex under a profile root)"
