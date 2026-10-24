#!/usr/bin/env python3
"""OPERATOR SCRIPT v3 (two independent reviews applied; not run by the agent).

Builds the local-only private overlay for SPE-108 from the live ~/.claude/CLAUDE.md.
  RETIRE  exact titles superseded by the engineering-workflow contract (never copied)
  KEEP    any other '## ' section with a clause OR any body line absent from the public base (copied verbatim)
  LEAVE   any other section fully present in the public base
Auto-Merge Policy is not retired (its sub-agent-must-not-merge rule has no public replacement).
Requires --repo to be a clean checkout whose HEAD equals origin/main (prints the SHA), so a stale
public base cannot misclassify. Fails closed. Prints counts only. Never overwrites.
Atomic create (temp + link), file 0600, directory 0700, no symlinked path component.
With --ledger, lines of the retired sections whose ledger disposition is PRESERVE_IN_PRIVATE_OVERLAY are
also copied verbatim into a 'Retained legacy delivery rules' overlay section, so retiring those sections
(retire-sections.py --ledger) loses nothing. Re-running with identical output is a no-op.
Usage: build-overlay.py --repo PATH_TO_CHECKOUT_OF_origin/main [--ledger LEDGER]
"""
import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import is_marker_line, managed_ranges, split_sections  # noqa: E402
import ledger as L  # noqa: E402

RETIRE = ('Commit Policy', 'Pull Request Policy', 'Git Worktree Workflow', 'Delegation Model')
MARKERS = ('coding-agent-environment:engineering-workflow', 'coding-agent-environment:private-overlay')
EXTRA = ['<!-- clause-keywords: @rules/secret-handling.md, @RTK.md -->\n', '@rules/secret-handling.md\n', '@RTK.md\n']
MAX_BYTES = 262144

parser = argparse.ArgumentParser()
parser.add_argument('--repo', required=True)
parser.add_argument('--ledger')
cli = parser.parse_args()
repo = Path(cli.repo).resolve()


def git(*args):
    return subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


head, upstream = git('rev-parse', 'HEAD'), git('rev-parse', 'origin/main')
if head != upstream or git('status', '--porcelain'):
    sys.exit('refusing: --repo must be a clean checkout of origin/main (run git fetch and checkout first)')
print('public base %s' % head[:12])

spec = importlib.util.spec_from_file_location('drift', repo / 'scripts/claude-clause-drift.py')
drift = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drift)

public_text = (repo / '.claude/CLAUDE.md').read_text(encoding='utf-8')
public_digests = {h for _, _, h in drift.clauses(public_text)}
public_lines = {' '.join(l.split()).lower() for l in public_text.splitlines() if l.strip() and l.strip() != '---'}
live = (Path.home() / '.claude/CLAUDE.md')
if live.is_symlink():
    sys.exit('refusing: live file is a symlink')
live_lines = L.split_keepends(live.read_text(encoding='utf-8'))

sections = split_sections(live_lines)
titles = [t for t, _, _ in sections]
missing = [r for r in RETIRE if r not in titles]
if missing:
    sys.exit('refusing: %d RETIRE title(s) not found in the live file' % len(missing))

out, kept, retired, left = [], 0, 0, 0
for title, start, end in sections:
    body = live_lines[start:end]
    # Text owned by the installer (inside a managed span or overlay block) is never copied: the span is
    # rewritten on install, and copying it would reintroduce a stale duplicate of public policy.
    owned = [r for r in managed_ranges(live_lines) if start <= r[1] and r[0] < end]
    if owned:
        cut = min(max(r[0], start) for r in owned) - start
        body = body[:cut]
        if not any(l.strip() for l in body[1:]):
            left += 1
            continue
    if title in RETIRE:
        retired += 1
        continue
    text = ''.join(body)
    new_clause = any(h not in public_digests for _, _, h in drift.clauses(text))
    new_line = any(' '.join(l.split()).lower() not in public_lines
                   for l in body if l.strip() and l.strip() != '---' and not l.startswith('<!--') and not l.startswith('@'))
    if new_clause or new_line:
        while body and body[-1].strip() in ('', '---'):
            body = body[:-1]
        out += body + ['\n']
        kept += 1
    else:
        left += 1

if cli.ledger:
    try:
        rows = L.load(cli.ledger)
    except L.LedgerError as error:
        sys.exit('refusing: %s' % error)
    retained, runs = [], 0
    for title, start, end in sections:
        if title not in RETIRE or any(r[0] < end and start <= r[1] for r in managed_ranges(live_lines)):
            continue
        picked = [i for i in range(start + 1, end) if not L.is_structural(live_lines[i])
                  and rows.get(L.line_key(live_lines[i]), (None,))[0] == L.OVERLAY]
        previous = None
        for i in picked:
            if previous is None or i - previous > 1:
                retained += ['\n', '### Retained from: %s\n' % title]
                runs += 1
            retained.append(live_lines[i] if live_lines[i].endswith('\n') else live_lines[i] + '\n')
            previous = i
    if retained:
        out += ['## Retained legacy delivery rules\n'] + retained + ['\n']
        print('retained legacy runs %d' % runs)
out += EXTRA
data = ''.join(out).encode('utf-8')
if not kept or not data.strip():
    sys.exit('refusing: empty overlay')
if len(data) > MAX_BYTES:
    sys.exit('refusing: overlay exceeds installer limit')
if any(m.encode() in data for m in MARKERS):
    sys.exit('refusing: output would contain managed markers')

local = Path(os.environ.get('CODING_AGENT_LOCAL_DIR') or Path.home() / '.config/coding-agent-environment/local')
folder = Path(os.environ.get('CODING_AGENT_LOCAL_BASELINE_DIR') or local / 'baseline') / 'claude'
for part in [folder, *folder.parents]:
    if part.is_symlink():
        sys.exit('refusing: symlinked path component')
target = folder / 'CLAUDE.private.md'
if target.is_file() and not target.is_symlink() and target.read_bytes() == data:
    print('overlay unchanged (identical output); nothing written')
    sys.exit(0)
if target.exists() or target.is_symlink():
    sys.exit('refusing: overlay already exists')
for part in reversed([folder, *[p for p in folder.parents if not p.exists()]][:]):
    pass
folder.mkdir(parents=True, exist_ok=True, mode=0o700)
os.chmod(folder, 0o700)
tmp = folder / ('.build-%d.tmp' % os.getpid())
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
try:
    with os.fdopen(fd, 'wb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.link(tmp, target)
finally:
    tmp.unlink(missing_ok=True)
print('overlay bytes %d, kept sections %d, retire sections %d, left in place %d' % (len(data), kept, retired, left))
