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
#   ./scripts/profile-install.sh --global [--dry-run | --check]
#   ./scripts/profile-install.sh --capabilities [--dry-run | --check | --remove]
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_BIN_DIR="${INSTALL_BIN_DIR:-$HOME/.local/bin}"
PROFILE_DIR="${CODING_AGENT_PROFILE_DIR:-$HOME/.coding-agent-profiles}"
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
DRY_RUN=0
GLOBAL=0
HOST_MODE=codex
CHECK=0
CAPABILITIES=0
REMOVE=0

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --global) GLOBAL=1 ;;
        --claude-skills) GLOBAL=1; HOST_MODE=claude ;;
        --check) CHECK=1 ;;
        --capabilities) CAPABILITIES=1 ;;
        --remove) REMOVE=1 ;;
        *) echo "profile-install.sh: unknown option: $arg" >&2; exit 2 ;;
    esac
done

if (( GLOBAL && CAPABILITIES )); then
    echo 'Choose --global or --capabilities, not both.' >&2
    exit 2
fi
if (( REMOVE && ! CAPABILITIES )); then
    echo '--remove requires --capabilities.' >&2
    exit 2
fi
if (( DRY_RUN + CHECK + REMOVE > 1 )); then
    echo 'Choose only one of --dry-run, --check, or --remove.' >&2
    exit 2
fi

if (( CAPABILITIES )); then
    if [[ -n "${CODEX_HOME:-}" && "$CODEX_HOME" != "${CODING_AGENT_SYNC_HOME:-$HOME}/.codex" ]]; then
        echo 'Capabilities mode requires the selected user Codex home; custom CODEX_HOME is not managed.' >&2
        exit 2
    fi
    python3 - "$REPO_ROOT" "${CODING_AGENT_SYNC_HOME:-$HOME}" "$DRY_RUN" "$CHECK" "$REMOVE" <<'PYCAPABILITIES'
import datetime
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile

root, live = map(Path, sys.argv[1:3])
dry, check, remove = map(int, sys.argv[3:])
marker = b'# coding-agent-environment:managed-capability-profile:v1\n'
source = root / 'codex/capabilities-readonly.config.toml'
target = live / '.codex/capabilities-readonly.config.toml'

def reject_symlinks(path):
    for part in [path, *path.parents]:
        if part == live.parent:
            break
        if part.is_symlink():
            raise SystemExit('Refusing capability mutation through a symlinked path.')

desired = source.read_bytes()
if not desired.startswith(marker):
    raise SystemExit('Tracked capability profile is missing its ownership marker.')
reject_symlinks(target)
if target.exists() and not target.is_file():
    raise SystemExit('Refusing to manage a non-regular capability profile path.')
prior = target.read_bytes() if target.exists() else None
if prior is not None and not prior.startswith(marker):
    raise SystemExit('Refusing to overwrite an unowned capability profile.')
if remove and prior is not None and prior != desired:
    raise SystemExit('Refusing to remove a drifted capability profile; reconcile owned content first.')

expected = None if remove else desired
mode_ok = prior is None or stat.S_IMODE(target.stat().st_mode) == 0o600
changed = prior != expected or not mode_ok
action = 'REMOVE' if remove else 'DIFFERS'
print(f"{action if changed else 'IDENTICAL'} {target} <- {source.relative_to(root)}")
if dry or check:
    print(f'Managed capability drift: {int(changed)}')
    raise SystemExit(int(changed and check))
if not changed:
    print('Managed capability drift: 0')
    raise SystemExit(0)

if prior is not None:
    backup = live / '.codex/backups/coding-agent-environment' / datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    reject_symlinks(backup)
    saved = backup / target.relative_to(live)
    saved.parent.mkdir(parents=True, mode=0o700)
    shutil.copy2(target, saved)
    saved.chmod(0o600)
    print(f'Prior state: {backup} (restore the corresponding path if needed)')
if remove:
    target.unlink()
else:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(desired)
        os.chmod(name, 0o600)
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    if target.read_bytes() != desired or stat.S_IMODE(target.stat().st_mode) != 0o600:
        raise SystemExit('Capability profile validation failed after atomic update.')
print('Managed capability drift: 0')
PYCAPABILITIES
    exit $?
fi

if (( GLOBAL )); then
    if [[ "$HOST_MODE" == codex && -n "${CODEX_HOME:-}" && "$CODEX_HOME" != "${CODING_AGENT_SYNC_HOME:-$HOME}/.codex" ]]; then
        echo 'Global mode requires the standard user Codex home; custom CODEX_HOME is not managed.' >&2
        exit 2
    fi
    python3 - "$REPO_ROOT" "${CODING_AGENT_SYNC_HOME:-$HOME}" "$DRY_RUN" "$CHECK" "$HOST_MODE" <<'PYGLOBAL'
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

root, live = map(Path, sys.argv[1:3])
dry, check = map(int, sys.argv[3:5])
host_mode = sys.argv[5]
host_dir = '.' + host_mode
drift_label = 'Managed Codex drift' if host_mode == 'codex' else 'Managed Claude practice drift'
if dry and check:
    raise SystemExit('Choose --dry-run or --check, not both.')
start = b'<!-- coding-agent-environment:signal-first:start -->'
end = b'<!-- coding-agent-environment:signal-first:end -->'

def managed_span(content):
    if content.count(start) != 1 or content.count(end) != 1:
        raise ValueError('Expected one matching managed instruction marker pair.')
    lo, hi = content.index(start), content.index(end) + len(end)
    if lo >= hi - len(end):
        raise ValueError('Reversed managed instruction markers.')
    return lo, hi

def reject_symlinks(path):
    for part in [path, *path.parents]:
        if part == live.parent:
            break
        if part.is_symlink():
            raise ValueError(f'Refusing mutation through symlink: {part}')

# Preflight must finish before mutation so a bad skill target cannot leave AGENTS partly applied.
if host_mode == 'claude':
    start = b'<!-- coding-agent-environment:engineering-workflow:start -->'
    end = b'<!-- coding-agent-environment:engineering-workflow:end -->'
    policy = (root / '.claude/CLAUDE.md').read_bytes()
    agents = live / '.claude/CLAUDE.md'
else:
    policy = (root / 'codex/AGENTS.md').read_bytes()
    agents = live / '.codex/AGENTS.md'
    if (agents.parent / 'AGENTS.override.md').exists():
        raise SystemExit('AGENTS.override.md overrides AGENTS.md; reconcile it explicitly first.')
lo, hi = managed_span(policy)
block = policy[lo:hi]
reject_symlinks(agents)
prior = agents.read_bytes() if agents.exists() else b''
if start in prior or end in prior:
    lo, hi = managed_span(prior)
    merged = prior[:lo] + block + prior[hi:]
elif prior:
    merged = prior + (b'' if prior.endswith(b'\n') else b'\n') + b'\n' + block + b'\n'
else:
    merged = policy if host_mode == 'codex' else block + b'\n'
shared_skills = ['evidence-first-briefing', 'engineering-workflow']
shared_manifest = json.loads((root / 'codex/shared-skills.json').read_text())
shared_skills += [name for name in shared_manifest['skills'] if name not in shared_skills]
actual_skills = {p.parent.name for p in (root / '.claude/skills').glob('*/SKILL.md')}
if actual_skills != set(shared_skills):
    raise SystemExit('Shared skill manifest does not cover the complete authored inventory.')
managed = [(agents, merged, 'managed instruction block; preserve outside bytes')]
for skill in shared_skills:
    folder = root / '.claude/skills' / skill
    body = (folder / 'SKILL.md').read_text()
    if not body.startswith('---\n') or ('name: ' + skill + '\n') not in body:
        raise SystemExit(f'Invalid native skill metadata: {skill}')
    sources = {str(p.relative_to(folder)): p for p in folder.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}
    for relative, source in shared_manifest['resources'].get(skill, {}).items():
        sources[relative] = root / source
    for relative, source in sources.items():
        target = live / host_dir / 'skills' / skill / relative
        reject_symlinks(target)
        managed.append((target, source.read_bytes(), str(source.relative_to(root))))
for name in shared_manifest['profiles'] if host_mode == 'codex' else []:
    source = root / 'codex' / name
    target = live / '.codex' / name
    reject_symlinks(target)
    if target.exists() and not target.read_bytes().startswith(b'# coding-agent-environment:managed-'):
        raise SystemExit(f'Refusing unowned native profile: {target}')
    managed.append((target, source.read_bytes(), str(source.relative_to(root))))
plans = []
for path, data, label in managed:
    profile = path.name in shared_manifest['profiles']
    changed = not path.exists() or path.read_bytes() != data or (profile and stat.S_IMODE(path.stat().st_mode) != 0o600)
    print(f"{'DIFFERS' if changed else 'IDENTICAL'} {path} <- {label}")
    if changed:
        plans.append((path, data))
        if dry:
            if path == agents:
                print('Desired managed block:\n' + block.decode())
            else:
                print(f'Desired SHA256: {hashlib.sha256(data).hexdigest()} ({len(data)} bytes)')
manifest = json.loads((root / 'codex/subtraction-skills.json').read_text())
missing = []
for skill, spec in manifest['skills'].items() if host_mode == 'codex' else []:
    folder = live / '.agents/skills' / skill
    bad = [name for name, digest in spec['files'].items()
           if not (folder / name).is_file()
           or hashlib.sha256((folder / name).read_bytes()).hexdigest() != digest]
    print(f"{'DIFFERS' if bad else 'IDENTICAL'} {folder} <- {manifest['repository']}@{manifest['revision']}")
    if bad:
        reject_symlinks(folder)
        for path in folder.rglob('*') if folder.exists() else []:
            reject_symlinks(path)
        missing.append(skill)
        for name in bad:
            print(f'  {name} -> SHA256 {spec["files"][name]}')
remaining = len(plans) + len(missing)
if dry or check:
    print(f'{drift_label}: {remaining}')
    raise SystemExit(int(bool(remaining) and check))
if missing and live.resolve() != Path.home().resolve():
    raise SystemExit('External installation requires the actual home; fixture checks stay offline.')
lock = live / '.agents/.skill-lock.json'
if missing:
    reject_symlinks(lock)
    prior_lock = json.loads(lock.read_text()) if lock.exists() else {}
    unrelated_skills = {name: value for name, value in prior_lock.get('skills', {}).items()
                        if name not in missing}
if remaining:
    backup = live / host_dir / 'backups/coding-agent-environment' / datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    reject_symlinks(backup)
    backup.mkdir(parents=True, mode=0o700)
    for path in [p for p, _ in plans] + [live / '.agents/skills' / name for name in missing] + ([lock] if missing else []):
        if not path.exists():
            continue
        saved = backup / path.relative_to(live)
        saved.parent.mkdir(parents=True, exist_ok=True)
        if path.is_dir():
            shutil.copytree(path, saved, symlinks=True)
        else:
            shutil.copy2(path, saved)
            saved.chmod(0o600)
    print(f'Prior state: {backup} (restore corresponding paths if needed)')
if missing:
    subprocess.run(['npx', '--yes', manifest['installer'], 'add',
                    f"https://github.com/{manifest['repository']}/tree/{manifest['revision']}",
                    '--global', '--agent', 'codex', '--skill', *missing,
                    '--full-depth', '--yes'], check=True)
    updated_lock = json.loads(lock.read_text())
    if any(updated_lock.get('skills', {}).get(name) != value for name, value in unrelated_skills.items()):
        if (backup / '.agents/.skill-lock.json').exists():
            shutil.copy2(backup / '.agents/.skill-lock.json', lock)
        raise SystemExit('Installer changed unrelated skill records; lock restored, inspect backup before continuing.')
    for skill in missing:
        folder = live / '.agents/skills' / skill
        if not all((folder / name).is_file() and hashlib.sha256((folder / name).read_bytes()).hexdigest() == digest
                   for name, digest in manifest['skills'][skill]['files'].items()):
            raise SystemExit('External installer did not produce the pinned canonical content; restore from backup.')
for path, data in plans:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = 0o600 if path.name in shared_manifest['profiles'] else (stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    if path.read_bytes() != data:
        raise SystemExit(f'Validation failed: {path}')
print(f'{drift_label}: 0')
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
