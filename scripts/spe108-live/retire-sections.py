#!/usr/bin/env python3
"""OPERATOR SCRIPT v3: OPTIONAL. Zero managed drift never requires it. Remove the superseded legacy sections from the
unmarked live ~/.claude/CLAUDE.md. Run it ONLY after `profile-install.sh --claude-skills` has delivered the
replacement sections inside the managed span (this script refuses otherwise).
Dry-run by default (prints line ranges, counts, and the line numbers of residual references; no text).
--apply requires --expect-sha256 of the live file, takes a byte-exact 0600 backup, preserves the file mode,
writes atomically, and refuses if a managed marker lies inside a removed range.
--apply additionally requires --ledger and --repo: every content line of the legacy sections must be ledgered,
none may be a REAL_CONFLICT, every PRESERVE_IN_PRIVATE_OVERLAY line must already exist outside the removed
blocks (the installed overlay), and every citation must exist in the pinned checkout. Otherwise it refuses
and removes nothing. Re-running after a successful retirement is a no-op.
Rollback: copy the printed backup path back over ~/.claude/CLAUDE.md.
"""
import argparse
import hashlib
import os
import re
import stat
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import SPAN_END, SPAN_START, is_marker_line, managed_ranges, split_sections  # noqa: E402
import ledger as L  # noqa: E402

RETIRE = ('Commit Policy', 'Pull Request Policy', 'Git Worktree Workflow', 'Delegation Model')
REPLACEMENTS = ('Workflow Path', 'Engineering Workflow Contract', 'Push Gate and Worktree Adapter',
                'Delegation Model', 'Merge Policy', 'Decision Delegation')
RESIDUAL = re.compile(r'Delegation Model|squash|Push directly if all checks pass|CLAUDE_CURRENT_RELEASE|\.current-worktree',
                      re.IGNORECASE)

parser = argparse.ArgumentParser()
parser.add_argument('--apply', action='store_true')
parser.add_argument('--expect-sha256')
parser.add_argument('--repo', help='checkout of origin/main, for the per-section coverage report and citation check')
parser.add_argument('--ledger', help='local policy ledger; required for --apply')
args = parser.parse_args()
live = Path.home() / '.claude/CLAUDE.md'
if live.is_symlink():
    sys.exit('refusing: live file is a symlink')
raw = live.read_bytes()
digest = hashlib.sha256(raw).hexdigest()
lines = raw.decode('utf-8').splitlines(keepends=True)
text = ''.join(lines)
if text.count(SPAN_START) != 1 or text.count(SPAN_END) != 1:
    sys.exit('refusing: managed span absent; run profile-install.sh --claude-skills first')
sections = split_sections(lines)
span_lo, span_hi = text.index(SPAN_START), text.index(SPAN_END)
offsets, running = [], 0
for line in lines:
    offsets.append(running)
    running += len(line)
inside = [t for t, s, e in sections if span_lo <= offsets[s] < span_hi]
if any(inside.count(r) != 1 for r in REPLACEMENTS):
    sys.exit('refusing: replacement sections are not all inside the managed span (install the merged baseline first)')
legacy = [(s, e) for t, s, e in sections if t in RETIRE and not (span_lo <= offsets[s] < span_hi)]
if not legacy and args.ledger:
    print('already retired: no legacy section outside the managed span; nothing to do')
    sys.exit(0)
if len(legacy) != len(RETIRE) or any(SPAN_START in ''.join(lines[s:e]) or SPAN_END in ''.join(lines[s:e]) for s, e in legacy):
    sys.exit('refusing: expected %d legacy sections outside the span, found %d (or a marker lies inside one)' % (len(RETIRE), len(legacy)))
# Only the legacy blocks themselves may be removed: no marker, HTML comment, import line or H1 inside them.
for s_, e_ in legacy:
    for line in lines[s_ + 1:e_]:
        if is_marker_line(line) or line.lstrip().startswith(('<!--', '@')) or re.match(r'^# ', line):
            sys.exit('refusing: a legacy section contains a marker, comment, import or H1 line; move it out first')
    if any(r[0] < e_ and s_ <= r[1] for r in managed_ranges(lines)):
        sys.exit('refusing: a legacy section overlaps a managed or overlay block')
drop = {i for s, e in legacy for i in range(s, e)}
kept_lines = [l for i, l in enumerate(lines) if i not in drop]
print('live sha256 %s; legacy sections %d; lines to remove %d' % (digest[:16], len(legacy), len(drop)))
for s, e in legacy:
    print('  lines %d-%d' % (s + 1, e))
# Residual references, numbered as they will be AFTER removal and labelled by region. Span text is
# installer-owned (do not edit); overlay text is edited in CLAUDE.private.md and reinstalled.
after_regions = managed_ranges(kept_lines)
labelled = {'original': [], 'managed span (do not edit)': [], 'overlay copy (edit CLAUDE.private.md)': []}
for number, line in enumerate(kept_lines, 1):
    if not RESIDUAL.search(line):
        continue
    where = 'original'
    for lo, hi in after_regions:
        if lo <= number - 1 <= hi:
            where = 'managed span (do not edit)' if 'engineering-workflow' in kept_lines[lo] else 'overlay copy (edit CLAUDE.private.md)'
    labelled[where].append(number)
for region, numbers in labelled.items():
    print('residual references in %s: %s' % (region, numbers))
# Per-section review aid: how many non-blank lines are absent from the public base (counts only).
if args.repo:
    public = ' '.join(((Path(args.repo) / '.claude/CLAUDE.md').read_text(encoding='utf-8') + ' '.join(
        p.read_text(encoding='utf-8') for p in (Path(args.repo) / '.claude/skills').rglob('*.md'))).split()).lower()
    for s_, e_ in legacy:
        body = [l for l in lines[s_:e_] if l.strip() and l.strip() != '---']
        absent = sum(1 for l in body if ' '.join(l.split()).lower() not in public)
        print('  legacy section %d-%d: %d non-blank lines, %d absent from the public base and skills (kept only in the backup)' % (s_ + 1, e_, len(body), absent))
gate_failures = []
if args.ledger:
    try:
        rows = L.load(args.ledger)
    except L.LedgerError as error:
        sys.exit('refusing: %s' % error)
    kept_text = {L.normalize(l) for _, l in L.content_lines(''.join(kept_lines))}
    counts = {}
    for s_, e_ in legacy:
        for number in range(s_, e_):
            if L.is_structural(lines[number]):
                continue
            row = rows.get(L.line_key(lines[number]))
            if row is None:
                gate_failures.append('line %d unledgered' % (number + 1))
                continue
            counts[row[0]] = counts.get(row[0], 0) + 1
            if row[0] == L.CONFLICT:
                gate_failures.append('line %d open conflict' % (number + 1))
            elif row[0] == L.OVERLAY and L.normalize(lines[number]) not in kept_text:
                gate_failures.append('line %d overlay copy missing' % (number + 1))
    for name in sorted(counts):
        print('  ledger %s: %d' % (name, counts[name]))
    if args.repo:
        bad = L.check_citations(rows, args.repo)
        gate_failures += ['citation %s missing in checkout' % key for key in bad]
    print('lossless gate: %s (%d failures)' % ('PASS' if not gate_failures else 'FAIL', len(gate_failures)))
    for failure in gate_failures[:40]:
        print('  ' + failure)
if not args.apply:
    sys.exit(1 if gate_failures else 0)
if not args.ledger or not args.repo:
    sys.exit('refusing: --apply needs --ledger and --repo (lossless preservation must be shown first)')
if gate_failures:
    sys.exit('refusing: lossless gate failed; nothing removed')
if args.expect_sha256 != digest:
    sys.exit('refusing: live file hash differs from --expect-sha256')
backup_dir = Path.home() / ('.claude/backups/spe-108-retire-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()))
backup_dir.mkdir(parents=True, mode=0o700)
backup = backup_dir / 'CLAUDE.md'
fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'wb') as handle:
    handle.write(raw)
if hashlib.sha256(backup.read_bytes()).hexdigest() != digest:
    sys.exit('backup verification failed; live file untouched')
new = ''.join(kept_lines).encode('utf-8')
mode = stat.S_IMODE(live.stat().st_mode)
tmp = live.with_name('.CLAUDE.md.retire.tmp')
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
try:
    with os.fdopen(fd, 'wb') as handle:
        handle.write(new)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, live)
finally:
    tmp.unlink(missing_ok=True)
print('applied; backup %s' % backup)
