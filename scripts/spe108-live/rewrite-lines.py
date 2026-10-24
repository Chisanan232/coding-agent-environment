#!/usr/bin/env python3
"""OPERATOR SCRIPT: replace named superseded policy lines with their approved canonical line.

Only lines whose hash is listed in the ledger WITH a replacement are touched; every other byte is kept.
The file must match --expect-sha256, the dry run is deterministic (no timestamps, sorted), and --apply
takes a byte-exact 0600 backup first and writes atomically with the original mode. A second run finds
nothing to rewrite and writes nothing. Output carries line numbers and hashes, never policy text.
Rollback: copy the printed backup over the file.
Usage: rewrite-lines.py --file ~/.claude/CLAUDE.md --ledger LEDGER --expect-sha256 SHA [--apply]
"""
import argparse
import hashlib
import os
import stat
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ledger as L  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument('--file', required=True)
parser.add_argument('--ledger', required=True)
parser.add_argument('--expect-sha256', required=True)
parser.add_argument('--apply', action='store_true')
parser.add_argument('--backup-dir', help='default: <file dir>/backups/spe-108-rewrite-<UTC>')
args = parser.parse_args()
target = Path(args.file)
if target.is_symlink() or not target.is_file():
    sys.exit('refusing: target must be a regular file, not a symlink')
try:
    rows = L.load(args.ledger)
except L.LedgerError as error:
    sys.exit('refusing: %s' % error)
raw = target.read_bytes()
digest = hashlib.sha256(raw).hexdigest()
if digest != args.expect_sha256:
    sys.exit('refusing: file hash %s differs from --expect-sha256' % digest[:16])
lines = raw.decode('utf-8').splitlines(keepends=True)
plans = {k: v[2] for k, v in rows.items() if v[2]}
if any(L.line_key(text) in plans for text in plans.values()):
    sys.exit('refusing: a replacement line is itself listed for rewriting')
out, changed = [], []
for number, line in enumerate(lines, 1):
    key = L.line_key(line)
    if key in plans:
        body = line.rstrip('\r\n')
        indent = body[:len(body) - len(body.lstrip())]
        out.append(indent + plans[key] + line[len(body):])
        changed.append((number, key, L.line_key(plans[key])))
    else:
        out.append(line)
print('file sha256 %s' % digest[:16])
for number, key, new_key in changed:
    print('  line %d %s -> %s' % (number, key, new_key))
print('lines to rewrite: %d' % len(changed))
if not changed or not args.apply:
    sys.exit(0)
backup_dir = Path(args.backup_dir) if args.backup_dir else target.parent / ('backups/spe-108-rewrite-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()))
backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
backup = backup_dir / target.name
fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'wb') as handle:
    handle.write(raw)
if hashlib.sha256(backup.read_bytes()).hexdigest() != digest:
    sys.exit('backup verification failed; file untouched')
mode = stat.S_IMODE(target.stat().st_mode)
tmp = target.with_name('.' + target.name + '.rewrite.tmp')
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
try:
    with os.fdopen(fd, 'wb') as handle:
        handle.write(''.join(out).encode('utf-8'))
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, target)
finally:
    tmp.unlink(missing_ok=True)
print('applied; backup %s' % backup)
