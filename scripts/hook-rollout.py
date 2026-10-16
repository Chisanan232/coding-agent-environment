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
import hashlib
import json
import os
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import pwd
import re
import sys

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
    raw_bytes = live_p.read_bytes()
    live = json.loads(raw_bytes)
    hook_sha = rollout.sha(src)
    state = gate_state(live)
    print('live settings sha256 %s' % sha_bytes(raw_bytes))
    print('hook file     sha256 %s' % hook_sha)
    print('settings gate entry: %s' % state)
    dest = Path(args.hook_dest)
    print('hook destination: %s (%s)' % (dest, 'matches' if dest.exists() and rollout.sha(dest) == hook_sha
                                          else 'exists, differs' if dest.exists() else 'absent'))
    print('mode file: %s (WARN is the staged default)' % mode_state(Path(args.mode_file)))
    raw = raw_bytes.decode()
    if serialise(live, raw) != raw:
        print('NOTE: re-serialising the unmodified settings changes its formatting; apply needs --allow-reformat')
    if state == 'conflict':
        print('REFUSING: the settings already hold a different disk-admission-gate entry')
        return 2
    print('change: %s' % ('none, already installed' if state == 'installed' else 'append 1 PreToolUse group (matcher Bash)'))
    return 0


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.mode == 'plan':
        return plan(args)
    print('mode %s is not available yet' % args.mode)
    return 2


if __name__ == '__main__':
    sys.exit(main())
