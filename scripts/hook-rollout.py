#!/usr/bin/env python3
"""Install the disk-admission gate hook into a live Claude Code settings file.

Modes (nothing is written except by `apply` and `rollback`):
  plan      print the exact change set and the sha256 of both inputs
  apply     back up, install the hook file, add ONE PreToolUse(Bash) group; needs both expected hashes
  verify    check the live install: hook present once, file hash, mode file state, no deny added
  rollback  restore the files saved by `apply` from its receipt, hash-exact

Staged rollout: the installed hook starts in WARN (an absent mode file is warn).
`apply` refuses when the mode file already says `enforce` unless --allow-enforce
is passed. It adds nothing else: every other hook and key is preserved, and no
permission rule is written. Safety helpers (hashing, exclusive-create backups,
atomic fsynced writes that preserve file mode, the merged-to-origin/main check)
are reused from permission-rollout.py. This is not host-wide protection: only
Claude Bash commands that the hook recognises are covered.
"""
import argparse
import fcntl
import hashlib
import json
import os
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import pwd
import re
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
_spec = spec_from_file_location('permission_rollout', ROOT / 'scripts/permission-rollout.py')
rollout = module_from_spec(_spec)
_spec.loader.exec_module(rollout)

HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
HOOK_SRC = ROOT / '.claude/hooks/disk-admission-gate.py'
HOOK_DEST = HOME / '.claude/hooks/disk-admission-gate.py'
LIVE = HOME / '.claude/settings.json'
MODE_FILE = HOME / '.cache/coding-agent-environment/admission/mode'
BACKUPS = HOME / '.claude/backups'
GATE_COMMAND = 'python3 ~/.claude/hooks/disk-admission-gate.py'
GATE_MARK = 'disk-admission-gate.py'


def gate_group():
    return {'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': GATE_COMMAND}]}


def gate_entries(live):
    """(group index, hook entry) for every hook entry that mentions the gate script."""
    found = []
    for index, group in enumerate((live.get('hooks') or {}).get('PreToolUse') or []):
        for entry in group.get('hooks') or []:
            if GATE_MARK in str(entry.get('command', '')):
                found.append((index, group, entry))
    return found


def gate_state(live):
    """'absent', 'installed' (exactly our group) or 'conflict' (some other gate entry)."""
    found = gate_entries(live)
    if not found:
        return 'absent'
    canonical = gate_group()
    if len(found) == 1 and found[0][1] == canonical:
        return 'installed'
    return 'conflict'


def with_gate(live):
    """Copy of live with our group appended last; every existing entry keeps its position."""
    new = json.loads(json.dumps(live))
    new.setdefault('hooks', {}).setdefault('PreToolUse', []).append(gate_group())
    return new


def mode_state(path=MODE_FILE):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, 'rb') as handle:
            value = handle.read(64).decode('ascii', 'replace').strip()
    except FileNotFoundError:
        return 'absent'
    except OSError:
        return 'invalid'
    return value if value in ('warn', 'enforce') else 'invalid'


def sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def serialise(obj, raw):
    # The first indented key line gives the file's indent width.
    found = re.search(r'\n( +)"', raw)
    indent = len(found.group(1)) if found else 2
    return json.dumps(obj, indent=indent, ensure_ascii=False) + ('\n' if raw.endswith('\n') else '')


BAD_SETTINGS_EXIT = 10


class SettingsError(Exception):
    """The settings file cannot be handled safely."""


def load_settings(path):
    """(raw bytes, text, parsed object) for a settings file this tool can edit; SettingsError otherwise."""
    try:
        raw_bytes = Path(path).read_bytes()
    except OSError as exc:
        raise SettingsError('cannot read %s: %s' % (path, exc))
    try:
        raw = raw_bytes.decode('utf-8')
    except UnicodeDecodeError:
        raise SettingsError('settings are not valid UTF-8')
    try:
        live = json.loads(raw)
    except ValueError as exc:
        raise SettingsError('settings are not valid JSON: %s' % exc)
    if not isinstance(live, dict):
        raise SettingsError('the settings root is not an object')
    hooks = live.get('hooks', {})
    if not isinstance(hooks, dict):
        raise SettingsError('"hooks" is not an object')
    groups = hooks.get('PreToolUse', [])
    if not isinstance(groups, list) or any(
            not isinstance(group, dict) or not isinstance(group.get('hooks', []), list)
            or any(not isinstance(entry, dict) for entry in group.get('hooks', [])) for group in groups):
        raise SettingsError('"hooks.PreToolUse" is not a list of hook groups')
    return raw_bytes, raw, live


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('mode', choices=['plan', 'apply', 'verify', 'rollback'])
    parser.add_argument('--live', default=str(LIVE))
    parser.add_argument('--hook-src', default=str(HOOK_SRC))
    parser.add_argument('--hook-dest', default=str(HOOK_DEST))
    parser.add_argument('--mode-file', default=str(MODE_FILE))
    parser.add_argument('--backup-dir', default=str(BACKUPS))
    parser.add_argument('--expect-live-sha')
    parser.add_argument('--expect-hook-sha')
    parser.add_argument('--allow-enforce', action='store_true')
    parser.add_argument('--allow-reformat', action='store_true')
    parser.add_argument('--unmerged-ok', action='store_true',
                        help='rehearsal only: allow a hook file that is not merged to origin/main')
    parser.add_argument('--receipt', help='receipt written by apply (rollback, verify)')
    return parser


def plan(args):
    live_p, src = Path(args.live), Path(args.hook_src)
    raw_bytes, raw, live = load_settings(live_p)
    hook_sha = rollout.sha(src)
    state = gate_state(live)
    print('live settings sha256 %s' % sha_bytes(raw_bytes))
    print('hook file     sha256 %s' % hook_sha)
    print('settings gate entry: %s' % state)
    dest = Path(args.hook_dest)
    print('hook destination: %s (%s)' % (dest, 'matches' if dest.exists() and rollout.sha(dest) == hook_sha
                                          else 'exists, differs' if dest.exists() else 'absent'))
    print('mode file: %s (WARN is the staged default)' % mode_state(Path(args.mode_file)))
    if serialise(live, raw) != raw:
        print('NOTE: re-serialising the unmodified settings changes its formatting; apply needs --allow-reformat')
    if state == 'conflict':
        print('REFUSING: the settings already hold a different disk-admission-gate entry')
        return 2
    print('change: %s' % ('none, already installed' if state == 'installed' else 'append 1 PreToolUse group (matcher Bash)'))
    return 0


class SettingsChanged(Exception):
    """The live settings changed between reading and replacing them."""


def replace_if_unchanged(path, data, expected):
    """Write data over path only if path still holds the bytes that were read (checked right before the rename)."""
    mode = path.stat().st_mode & 0o777
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + '.hookroll.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        if path.read_bytes() != expected:
            raise SettingsChanged()
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def take_lock(backups):
    """Exclusive non-blocking lock so two applies cannot interleave; None when another holds it."""
    descriptor = os.open(backups / '.hook-rollout.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    return descriptor


def redirected(*paths):
    """Message for the first path (or its parent directory) that is a symlink."""
    for path in paths:
        for candidate in (path, path.parent):
            if candidate.is_symlink():
                return 'REFUSING: %s is a symlink; resolve it and pass the real path' % candidate
    return None


def install_bytes(path, data, mode):
    """Private temp sibling, fsync, mode set before the rename, then fsync of the directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + '.hookroll.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def apply(args):
    live_p, src, dest = Path(args.live), Path(args.hook_src), Path(args.hook_dest)
    message = redirected(live_p, dest)
    if message:
        print(message)
        return 4
    raw_bytes, raw, live = load_settings(live_p)
    state = gate_state(live)
    if state == 'conflict':
        print('REFUSING: the settings already hold a different disk-admission-gate entry')
        return 2
    if mode_state(Path(args.mode_file)) == 'enforce' and not args.allow_enforce:
        print('REFUSING: the mode file says enforce; the staged rollout starts in WARN (pass --allow-enforce to override)')
        return 9
    hook_bytes = src.read_bytes()
    hook_sha, live_sha = sha_bytes(hook_bytes), sha_bytes(raw_bytes)
    dest_sha = rollout.sha(dest) if dest.exists() else None
    if state == 'installed' and dest_sha == hook_sha:
        print('already installed: settings entry present once and hook file identical; nothing changed')
        return 0
    if not args.unmerged_ok:
        why = rollout.baseline_not_merged(src)
        if why:
            print('REFUSING: %s. Deploy only a merged hook file (or pass --unmerged-ok for a rehearsal on a copy).' % why)
            return 7
    if args.expect_hook_sha != hook_sha or (state == 'absent' and args.expect_live_sha != live_sha):
        print('REFUSING: --expect-hook-sha / --expect-live-sha do not match the inputs; re-review required')
        return 3
    new_text = None
    if state == 'absent':
        if serialise(live, raw) != raw and not args.allow_reformat:
            print('REFUSING: re-serialising the settings would change unrelated formatting; pass --allow-reformat')
            return 8
        new_text = serialise(with_gate(live), raw)
    backups = Path(args.backup_dir)
    message = redirected(backups / 'receipt')
    if message:
        print(message)
        return 4
    backups.mkdir(parents=True, exist_ok=True)
    lock = take_lock(backups)
    if lock is None:
        print('REFUSING: another apply holds the lock in %s' % backups)
        return 11
    try:
        return _apply_locked(args, live_p, dest, backups, raw_bytes, live_sha, hook_bytes, hook_sha, dest_sha, new_text)
    finally:
        os.close(lock)


def _apply_locked(args, live_p, dest, backups, raw_bytes, live_sha, hook_bytes, hook_sha, dest_sha, new_text):
    stamp = time.time_ns()
    receipt = {'schema': 1, 'live': str(live_p), 'settings_backup': None, 'settings_sha_before': live_sha,
               'hook_dest': str(dest), 'hook_backup': None, 'hook_created': dest_sha is None,
               'hook_sha_installed': hook_sha}
    if new_text is not None:
        receipt['settings_backup'] = str(backups / ('settings.json.pre-SPE-109.%d' % stamp))
        rollout.write_new(Path(receipt['settings_backup']), raw_bytes, live_p.stat().st_mode & 0o777)
    if dest_sha is not None and dest_sha != hook_sha:
        receipt['hook_backup'] = str(backups / ('disk-admission-gate.py.pre-SPE-109.%d' % stamp))
        rollout.write_new(Path(receipt['hook_backup']), dest.read_bytes(), dest.stat().st_mode & 0o777)
    if live_p.read_bytes() != raw_bytes:
        print('REFUSING: the live file changed while the install was prepared; nothing was written')
        return 5
    if dest_sha != hook_sha:
        install_bytes(dest, hook_bytes, 0o755)
    if new_text is not None:
        try:
            replace_if_unchanged(live_p, new_text.encode(), raw_bytes)
        except SettingsChanged:
            if receipt['hook_created'] and dest.exists():
                dest.unlink()
            elif receipt['hook_backup']:
                install_bytes(dest, Path(receipt['hook_backup']).read_bytes(), 0o755)
            print('REFUSING: the live file changed immediately before the write; the hook install was undone')
            return 5
        receipt['settings_sha_after'] = sha_bytes(new_text.encode())
    receipt_path = backups / ('disk-admission-gate.receipt.%d.json' % stamp)
    rollout.write_new(receipt_path, (json.dumps(receipt, indent=2) + '\n').encode(), 0o600)
    print('applied | receipt %s | rollback: python3 %s rollback --live %s --receipt %s' % (
        receipt_path, sys.argv[0], live_p, receipt_path))
    return 0


def verify(args):
    live_p, dest = Path(args.live), Path(args.hook_dest)
    problems = []
    live = load_settings(live_p)[2]
    found = gate_entries(live)
    if len(found) != 1:
        problems.append('expected the gate hook exactly once in settings, found %d' % len(found))
    elif gate_state(live) != 'installed':
        problems.append('the gate entry is not the canonical PreToolUse Bash group')
    expected = args.expect_hook_sha or rollout.sha(args.hook_src)
    if redirected(dest):
        problems.append('hook destination is redirected by a symlink')
    elif not dest.exists():
        problems.append('hook file %s is missing' % dest)
    else:
        if rollout.sha(dest) != expected:
            problems.append('hook file hash differs from the expected hash')
        if not os.access(dest, os.X_OK):
            problems.append('hook file is not executable')
    mode = mode_state(Path(args.mode_file))
    if mode == 'invalid':
        problems.append('mode file is invalid (it is treated as warn)')
    if mode == 'enforce' and not args.allow_enforce:
        problems.append('mode file says enforce but --allow-enforce was not passed')
    deny = (live.get('permissions') or {}).get('deny') or []
    if any(GATE_MARK in str(rule) or 'disk-admission' in str(rule) for rule in deny):
        problems.append('a permissions.deny rule mentions the gate')
    if args.receipt:
        receipt = json.loads(Path(args.receipt).read_text())
        if receipt.get('settings_backup'):
            before = json.loads(Path(receipt['settings_backup']).read_text())
            if live != with_gate(before):
                problems.append('settings differ from the pre-apply backup plus the gate group (a deny or other key changed)')
    print('mode file: %s' % mode)
    print('\n'.join(problems) if problems else 'verify OK: gate hook present once, file hash matches, no deny added')
    return 1 if problems else 0


def rollback(args):
    if not args.receipt:
        print('rollback needs --receipt')
        return 2
    receipt = json.loads(Path(args.receipt).read_text())
    live_p, dest = Path(receipt['live']), Path(receipt['hook_dest'])
    message = redirected(live_p, dest)
    if message:
        print(message)
        return 4
    backup, hook_backup = receipt.get('settings_backup'), receipt.get('hook_backup')
    data = hook_data = None
    if backup:
        data = Path(backup).read_bytes()
        if sha_bytes(data) != receipt['settings_sha_before']:
            print('REFUSING: the settings backup no longer matches its recorded hash')
            return 3
        json.loads(data)
    if hook_backup:
        hook_data = Path(hook_backup).read_bytes()
    if data is not None and live_p.exists() and live_p.read_bytes() != data:
        current = live_p.read_bytes()
        saved = live_p.with_name('%s.pre-rollback.%d' % (live_p.name, time.time_ns()))
        rollout.write_new(saved, current, live_p.stat().st_mode & 0o777)
        print('current settings saved to %s' % saved)
        try:
            replace_if_unchanged(live_p, data, current)
        except SettingsChanged:
            print('REFUSING: the live file changed immediately before the restore; nothing was restored')
            return 5
        print('restored %s from %s' % (live_p, backup))
    elif data is not None:
        print('settings already match the backup; nothing to restore')
    if hook_data is not None:
        if not dest.exists() or dest.read_bytes() != hook_data:
            install_bytes(dest, hook_data, 0o755)
            print('restored %s from %s' % (dest, hook_backup))
    elif receipt.get('hook_created') and dest.exists():
        if rollout.sha(dest) == receipt['hook_sha_installed']:
            dest.unlink()
            print('removed %s (created by apply)' % dest)
        else:
            print('left %s in place: it no longer matches what apply installed' % dest)
    return 0


def main(argv=None):
    args = build_parser().parse_args(argv)
    handlers = {'plan': plan, 'apply': apply, 'verify': verify, 'rollback': rollback}
    try:
        return handlers[args.mode](args)
    except SettingsError as exc:
        print('REFUSING: %s' % exc)
        return BAD_SETTINGS_EXIT


if __name__ == '__main__':
    sys.exit(main())
