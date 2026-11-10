#!/usr/bin/env python3
"""Install the SPE-170 observe-only hook (scripts/ask-observe.py) into a live Claude Code settings file.

Modes (nothing is written except by `apply` and `rollback`):
  plan      print the hashes, the per-event state and the exact settings diff
  apply     back up, install the hook file, append FOUR hook groups; needs both expected hashes
  verify    check the live install: each group present once, file hash, no permission rule changed
  rollback  remove exactly the four groups apply added (later unrelated edits to settings are kept)

Exit codes: 0 ok, 1 verify problem, 2 refused (conflict, bad receipt, unreadable input), 3 pin mismatch, 4 symlink, 5 live file changed,
7 hook file not merged, 8 reformat needed (--allow-reformat), 10 bad settings, 11 lock held, 12 bad --hook-dest.
Use the newest receipt in the backup directory; apply on an already installed copy prints no new receipt.

The hook only observes: it prints nothing and exits 0, so it can never allow, deny or ask. This installer adds
no permission rule, changes no existing hook and key, and refuses when any of the four events already holds a different
ask-observe entry. The change set is pinned by `--expect-change-sha` (the hash of the groups to add) and the hook
file by `--expect-hook-sha`, so the reviewed plan is the applied change whatever else the settings hold. Safety
helpers (exclusive backups, atomic fsynced replace that re-checks the bytes just before the rename, the merged-to-origin/main
check) are reused from hook-rollout.py and permission-rollout.py.
"""
import argparse
import difflib
import hashlib
import json
import os
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import pwd
import shlex
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
_spec = spec_from_file_location('hook_rollout', ROOT / 'scripts/hook-rollout.py')
hr = module_from_spec(_spec)
_spec.loader.exec_module(hr)
rollout = hr.rollout

HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
HOOK_SRC = ROOT / 'scripts/ask-observe.py'
HOOK_NAME = 'ask-observe.py'
HOOK_DEST = HOME / '.claude/hooks' / HOOK_NAME
LIVE = HOME / '.claude/settings.json'
BACKUPS = HOME / '.claude/backups'
# event -> matcher (None: the group has no matcher)
EVENTS = {'PermissionRequest': None, 'PostToolUse': 'Bash', 'PostToolUseFailure': 'Bash', 'Notification': 'permission_prompt'}
HOOK_TIMEOUT = 5  # seconds; an observer must never stall a prompt

BAD_SETTINGS_EXIT = 10
BAD_DEST_EXIT = 12


def command_for(dest):
    dest = Path(dest)
    try:
        return 'python3 ~/%s hook' % shlex.quote(str(dest.relative_to(HOME)))
    except ValueError:
        return 'python3 %s hook' % shlex.quote(str(dest))


def group_for(event, command):
    group = {'hooks': [{'type': 'command', 'command': command, 'timeout': HOOK_TIMEOUT}]}
    if EVENTS[event]:
        group = {'matcher': EVENTS[event], **group}
    return group


def change_set(command):
    return {event: group_for(event, command) for event in EVENTS}


def change_sha(command):
    return hr.sha_bytes(json.dumps(change_set(command), sort_keys=True).encode())


def entries(live, event):
    """Every group of `event` whose hook commands mention the observer script."""
    return [group for group in (live.get('hooks') or {}).get(event) or []
            if any(HOOK_NAME in str(entry.get('command', '')) for entry in group.get('hooks') or [])]


def event_state(live, event, command):
    """'absent', 'installed' (exactly our group, once) or 'conflict'."""
    found = entries(live, event)
    if not found:
        return 'absent'
    return 'installed' if found == [group_for(event, command)] else 'conflict'


def states(live, command):
    return {event: event_state(live, event, command) for event in EVENTS}


def overall(live, command):
    values = set(states(live, command).values())
    if values == {'absent'}:
        return 'absent'
    if values == {'installed'}:
        return 'installed'
    return 'conflict'


def with_hooks(live, command):
    new = json.loads(json.dumps(live))
    hooks = new.setdefault('hooks', {})
    for event, group in change_set(command).items():
        hooks.setdefault(event, []).append(group)
    return new


def without_hooks(live, command, events_created=None, hooks_created=None):
    """Copy of live with exactly our canonical groups removed.

    An event list or the hooks object that apply created is dropped when removal empties it; one that existed before apply is
    left as it was (possibly empty). With no receipt facts (None) every list emptied by the removal is dropped."""
    new = json.loads(json.dumps(live))
    hooks = new.get('hooks') or {}
    removed = False
    for event in EVENTS:
        groups = hooks.get(event)
        if groups and group_for(event, command) in groups:
            removed = True
            hooks[event] = [group for group in groups if group != group_for(event, command)]
            if not hooks[event] and (events_created is None or event in events_created):
                del hooks[event]
    if removed and not hooks and (hooks_created is None or hooks_created):
        del new['hooks']
    return new


class SettingsError(Exception):
    """The settings file cannot be handled safely."""


def load_settings(path):
    try:
        raw_bytes = Path(path).read_bytes()
    except OSError as exc:
        raise SettingsError('cannot read %s: %s' % (path, exc))
    try:
        raw = raw_bytes.decode('utf-8')
        live = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise SettingsError('settings are not valid UTF-8 JSON: %s' % exc)
    if not isinstance(live, dict):
        raise SettingsError('the settings root is not an object')
    hooks = live.get('hooks', {})
    if not isinstance(hooks, dict):
        raise SettingsError('"hooks" is not an object')
    for event in EVENTS:
        groups = hooks.get(event, [])
        if not isinstance(groups, list) or any(
                not isinstance(group, dict) or not isinstance(group.get('hooks', []), list)
                or any(not isinstance(entry, dict) for entry in group.get('hooks', [])) for group in groups):
            raise SettingsError('"hooks.%s" is not a list of hook groups' % event)
    return raw_bytes, raw, live


def dest_problem(value):
    if not isinstance(value, str) or not value.startswith('/'):
        return '--hook-dest must be an absolute path (no ~, no relative path): %r' % (value,)
    if os.path.normpath(value) != value or '..' in Path(value).parts:
        return '--hook-dest must be normalised (no "..", ".", doubled or trailing slashes): %r' % value
    if os.path.basename(value) != HOOK_NAME:
        return '--hook-dest must end in %s: %r' % (HOOK_NAME, value)
    return None


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('mode', choices=['plan', 'apply', 'verify', 'rollback'])
    parser.add_argument('--live', default=str(LIVE))
    parser.add_argument('--hook-src', default=str(HOOK_SRC))
    parser.add_argument('--hook-dest', default=str(HOOK_DEST))
    parser.add_argument('--backup-dir', default=str(BACKUPS))
    parser.add_argument('--expect-hook-sha')
    parser.add_argument('--expect-change-sha')
    parser.add_argument('--allow-reformat', action='store_true')
    parser.add_argument('--unmerged-ok', action='store_true', help='rehearsal only: allow a hook file that is not merged to origin/main')
    parser.add_argument('--receipt', help='receipt written by apply (rollback, verify)')
    return parser


def diff_text(raw, new_text):
    return ''.join(difflib.unified_diff(raw.splitlines(True), new_text.splitlines(True), 'settings.json (live)', 'settings.json (after apply)', n=1))


def plan(args):
    raw_bytes, raw, live = load_settings(args.live)
    src, dest = Path(args.hook_src), Path(args.hook_dest)
    command = command_for(dest)
    if not src.is_file():
        print('REFUSING: cannot read the hook file %s' % src)
        return 2
    hook_sha = rollout.sha(src)
    state = overall(live, command)
    print('live settings sha256 %s' % hr.sha_bytes(raw_bytes))
    print('hook file     sha256 %s' % hook_sha)
    print('change set    sha256 %s' % change_sha(command))
    print('hook command: %s' % command)
    for event, value in states(live, command).items():
        print('  %-18s %s' % (event, value))
    print('hook destination: %s (%s)' % (dest, 'matches' if dest.exists() and rollout.sha(dest) == hook_sha
                                          else 'exists, differs' if dest.exists() else 'absent'))
    if hr.serialise(live, raw) != raw:
        print('NOTE: re-serialising the unmodified settings changes its formatting; apply needs --allow-reformat')
    if state == 'conflict':
        print('REFUSING: some event already holds an ask-observe entry that is not exactly ours (or only some events do)')
        return 2
    if state == 'installed':
        print('change: none for settings, already installed')
        return 0
    print('change: append 4 hook groups (PermissionRequest, PostToolUse, PostToolUseFailure, Notification); no permission rule, no existing entry touched')
    sys.stdout.write(diff_text(raw, hr.serialise(with_hooks(live, command), raw)))
    print('apply: python3 %s apply --expect-hook-sha %s --expect-change-sha %s' % (sys.argv[0], hook_sha, change_sha(command)))
    return 0


def apply(args):
    live_p, src, dest = Path(args.live), Path(args.hook_src), Path(args.hook_dest)
    message = hr.redirected(live_p, dest)
    if message:
        print(message)
        return 4
    raw_bytes, raw, live = load_settings(live_p)
    command = command_for(dest)
    state = overall(live, command)
    if state == 'conflict':
        print('REFUSING: some event already holds an ask-observe entry that is not exactly ours (or only some events do)')
        return 2
    try:
        hook_bytes = src.read_bytes()
    except OSError as exc:
        print('REFUSING: cannot read the hook file: %s' % exc)
        return 2
    hook_sha, live_sha = hr.sha_bytes(hook_bytes), hr.sha_bytes(raw_bytes)
    dest_sha = rollout.sha(dest) if dest.exists() else None
    if state == 'installed' and dest_sha == hook_sha:
        print('already installed: all four groups present once and hook file identical; nothing changed')
        return 0
    if not args.unmerged_ok:
        why = rollout.baseline_not_merged(src)
        if why:
            print('REFUSING: %s. Deploy only a merged hook file (or pass --unmerged-ok for a rehearsal on a copy).' % why)
            return 7
    if args.expect_hook_sha != hook_sha or args.expect_change_sha != change_sha(command):
        print('REFUSING: --expect-hook-sha / --expect-change-sha do not match the inputs; re-review required')
        return 3
    new_text = None
    if state == 'absent':
        if hr.serialise(live, raw) != raw and not args.allow_reformat:
            print('REFUSING: re-serialising the settings would change unrelated formatting; pass --allow-reformat')
            return 8
        new_text = hr.serialise(with_hooks(live, command), raw)
    backups = Path(args.backup_dir)
    message = hr.redirected(backups / 'receipt')
    if message:
        print(message)
        return 4
    backups.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backups, 0o700)
    lock = hr.take_lock(backups)
    if lock is None:
        print('REFUSING: another apply holds the lock in %s' % backups)
        return 11
    try:
        return _apply_locked(live_p, dest, backups, raw_bytes, live_sha, hook_bytes, hook_sha, dest_sha, new_text, command)
    finally:
        os.close(lock)


def _apply_locked(live_p, dest, backups, raw_bytes, live_sha, hook_bytes, hook_sha, dest_sha, new_text, command):
    stamp = time.time_ns()
    before = json.loads(raw_bytes)
    receipt = {'schema': 1, 'live': str(live_p), 'command': command, 'settings_backup': None, 'settings_sha_before': live_sha,
               'hook_dest': str(dest), 'hook_backup': None, 'hook_created': dest_sha is None, 'hook_sha_installed': hook_sha,
               'hooks_created': 'hooks' not in before,
               'events_created': [event for event in EVENTS if event not in (before.get('hooks') or {})]}
    if new_text is not None:
        receipt['settings_backup'] = str(backups / ('settings.json.pre-SPE-170.%d' % stamp))
        rollout.write_new(Path(receipt['settings_backup']), raw_bytes, 0o600)
    if dest_sha is not None and dest_sha != hook_sha:
        receipt['hook_backup'] = str(backups / ('ask-observe.py.pre-SPE-170.%d' % stamp))
        rollout.write_new(Path(receipt['hook_backup']), dest.read_bytes(), 0o600)
    if live_p.read_bytes() != raw_bytes:
        print('REFUSING: the live file changed while the install was prepared; nothing was written')
        return 5
    receipt_path = backups / ('ask-observe.receipt.%d.json' % stamp)
    if new_text is not None:
        receipt['settings_sha_after'] = hr.sha_bytes(new_text.encode())

    def undo():
        if receipt['hook_created'] and dest.exists():
            dest.unlink()
        elif receipt['hook_backup']:
            hr.install_bytes(dest, Path(receipt['hook_backup']).read_bytes(), 0o755)
        if receipt_path.exists():
            receipt_path.unlink()

    try:
        if dest_sha != hook_sha:
            hr.install_bytes(dest, hook_bytes, 0o755)
        # The receipt exists before settings change, so a failure after the replace is still reversible with `rollback`.
        rollout.write_new(receipt_path, (json.dumps(receipt, indent=2) + '\n').encode(), 0o600)
        if new_text is not None:
            hr.replace_if_unchanged(live_p, new_text.encode(), raw_bytes)
    except hr.SettingsChanged:
        undo()
        print('REFUSING: the live file changed immediately before the write; the hook install was undone')
        return 5
    except BaseException:
        undo()
        raise
    print('applied | receipt %s | rollback: python3 %s rollback --receipt %s' % (receipt_path, sys.argv[0], receipt_path))
    return 0


def verify(args):
    live_p, dest = Path(args.live), Path(args.hook_dest)
    live = load_settings(live_p)[2]
    command = command_for(dest)
    problems = [f'{event}: {value}' for event, value in states(live, command).items() if value != 'installed']
    if not args.expect_hook_sha and not Path(args.hook_src).is_file():
        print('REFUSING: cannot read the hook file %s' % args.hook_src)
        return 2
    expected = args.expect_hook_sha or rollout.sha(args.hook_src)
    if hr.redirected(dest):
        problems.append('hook destination is redirected by a symlink')
    elif not dest.exists():
        problems.append('hook file %s is missing' % dest)
    else:
        if rollout.sha(dest) != expected:
            problems.append('hook file hash differs from the expected hash')
        if not os.access(dest, os.X_OK):
            problems.append('hook file is not executable')
    if args.receipt:
        receipt, bad = load_receipt(args.receipt, args.backup_dir)
        if bad:
            problems.append(bad)
        elif receipt.get('settings_backup'):
            before = json.loads(Path(receipt['settings_backup']).read_text())
            if (live.get('permissions') or {}) != (before.get('permissions') or {}):
                problems.append('permissions differ from the pre-apply backup')
    print('\n'.join(problems) if problems else 'verify OK: four observer groups present once, file hash matches')
    return 1 if problems else 0


REQUIRED_RECEIPT_KEYS = ('live', 'command', 'hook_dest', 'hook_created', 'hook_sha_installed')


def load_receipt(path, backups=None):
    """Receipt dict, or a refusal string when it is missing, malformed or points at a path this tool would never have written."""
    try:
        receipt = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        return None, 'cannot read the receipt: %s' % exc
    if not isinstance(receipt, dict) or any(key not in receipt for key in REQUIRED_RECEIPT_KEYS):
        return None, 'the receipt is malformed'
    problem = dest_problem(receipt['hook_dest'])
    if problem or not str(receipt['live']).startswith('/') or os.path.normpath(receipt['live']) != receipt['live']:
        return None, 'the receipt holds an unexpected path (%s)' % (problem or receipt['live'])
    for key in ('hook_backup', 'settings_backup'):
        if receipt.get(key) and backups is not None and Path(receipt[key]).parent != Path(backups):
            return None, 'the receipt names a %s outside the backup directory' % key
    return receipt, None


def rollback(args):
    if not args.receipt:
        print('rollback needs --receipt')
        return 2
    receipt, bad = load_receipt(args.receipt, args.backup_dir)
    if bad:
        print('REFUSING: %s' % bad)
        return 2
    live_p, dest = Path(receipt['live']), Path(receipt['hook_dest'])
    message = hr.redirected(live_p, dest)
    if message:
        print(message)
        return 4
    backups = Path(args.backup_dir)
    backups.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backups, 0o700)
    lock = hr.take_lock(backups)
    if lock is None:
        print('REFUSING: another apply or rollback holds the lock in %s' % backups)
        return 11
    try:
        return _rollback_locked(args, receipt, live_p, dest)
    finally:
        os.close(lock)


def _rollback_locked(args, receipt, live_p, dest):
    command = receipt['command']
    raw_bytes, raw, live = load_settings(live_p)
    present = [event for event, value in states(live, command).items() if value == 'installed']
    if entries_other(live, command):
        print('REFUSING: an ask-observe entry that apply did not write is present; remove it by hand')
        return 2
    if present:
        if hr.serialise(live, raw) != raw and not args.allow_reformat:
            print('REFUSING: re-serialising the settings would change unrelated formatting; pass --allow-reformat')
            return 8
        new_text = hr.serialise(without_hooks(live, command, receipt.get('events_created'), receipt.get('hooks_created')), raw)
        saved = live_p.with_name('%s.pre-rollback.%d' % (live_p.name, time.time_ns()))
        rollout.write_new(saved, raw_bytes, 0o600)
        print('current settings saved to %s' % saved)
        try:
            hr.replace_if_unchanged(live_p, new_text.encode(), raw_bytes)
        except hr.SettingsChanged:
            print('REFUSING: the live file changed immediately before the rollback; nothing was changed')
            return 5
        print('removed %d observer groups; every other setting kept as it is now' % len(present))
    else:
        print('no observer groups in settings; nothing to remove')
    hook_backup = receipt.get('hook_backup')
    if hook_backup:
        data = Path(hook_backup).read_bytes()
        if not dest.exists() or dest.read_bytes() != data:
            hr.install_bytes(dest, data, 0o755)
            print('restored %s from %s' % (dest, hook_backup))
    elif receipt.get('hook_created') and dest.exists():
        if rollout.sha(dest) == receipt['hook_sha_installed']:
            dest.unlink()
            print('removed %s (created by apply)' % dest)
        else:
            print('left %s in place: it no longer matches what apply installed' % dest)
    return 0


def entries_other(live, command):
    """Observer entries that are not exactly our canonical group."""
    return [event for event in EVENTS if event_state(live, event, command) == 'conflict']


def main(argv=None):
    args = build_parser().parse_args(argv)
    problem = dest_problem(args.hook_dest) if args.mode != 'rollback' else None
    if problem:
        print('REFUSING: %s' % problem)
        return BAD_DEST_EXIT
    try:
        return {'plan': plan, 'apply': apply, 'verify': verify, 'rollback': rollback}[args.mode](args)
    except SettingsError as exc:
        print('REFUSING: %s' % exc)
        return BAD_SETTINGS_EXIT


if __name__ == '__main__':
    sys.exit(main())
