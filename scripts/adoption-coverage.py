#!/usr/bin/env python3
"""READ-ONLY report: which projects and live sessions are covered by the shared policy.

Per project it looks only at the repository instruction files (`.claude/CLAUDE.md`, `CLAUDE.md`, `AGENTS.md`) and
reports counts and line numbers, never their text. Per live Claude session it reads the session JSON (pid, cwd, start
time) and checks that the pid is alive and is the same process; it never reads a process command line.

A project file overrides the global one, so a stale commit format or a merge-strategy override there defeats the shared
rule even after the global file is updated. A session loads instruction files when it starts, so a session that began
before an instruction file last changed needs a reload (start a new session or resume it; whether a resumed session
re-reads the files is verified only by a fresh/resume probe, not by this report).

Usage: adoption-coverage.py --map projects.json [--global-file ~/.claude/CLAUDE.md] [--sessions-dir DIR] [--json]
  projects.json: {"Product": ["/abs/path/to/checkout", ...], ...}   (machine local; not committed)
"""
import argparse
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

FILES = ('.claude/CLAUDE.md', 'CLAUDE.md', 'AGENTS.md')
# Legacy commit/PR form: scope without parentheses after an emoji or the placeholder pair.
LEGACY_FORMAT = re.compile(r'<emoji>\s*<?scope>?\s*:|(?:✨|🐛|♻️|✅|📝|🔧|🔌|🪝|🧭|⬆️|🗑️|🚨)\s+[A-Za-z][\w/-]*:')
# A clause (split on ; and |) that permits squash merging: it names squash and no negation attaches to it.
SQUASH = re.compile(r'squash', re.IGNORECASE)
NEGATION = re.compile(r"\b(?:never|no|not|disabled|forbidden|prohibit\w*|destroy\w*|avoid|don['\u2019]t|without|"
                      r"instead of|rather than)\b", re.IGNORECASE)
# Three-part branch form: the ticket is followed directly by the summary, with no <type> segment.
BRANCH_THREE_PART = re.compile(r'<ticket(?:-?id|-?number)?>/<(?:short|summary)', re.IGNORECASE)
MAX_BYTES = 1 << 20


def permits_squash(clause):
    """Names squash with no negation attached to it. Negation counts only inside the same sentence and the words
    right around the term, so `never rebase. Squash all PRs` and `Squash merge, not rebase` still permit, while
    `never squash`, `squash is disabled` and `instead of squash` do not."""
    for hit in SQUASH.finditer(clause):
        before = re.split(r'[.!?]', clause[:hit.start()])[-1][-30:]
        after = re.split(r'[.,(]', clause[hit.end():])[0][:30]
        if not NEGATION.search(before) and not NEGATION.search(after):
            return True
    return False


def scan_file(path):
    """(found, skipped): line numbers per finding kind, or why the file was not scanned. Opens the already
    resolved path without blocking and only if it is a regular file."""
    found = {'legacy_format': [], 'merge_override': [], 'old_branch_format': []}
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return None, 'not a regular file'
            raw = handle.read(MAX_BYTES + 1)
    except OSError:
        return None, 'unreadable'
    if len(raw) > MAX_BYTES:
        return None, 'larger than 1 MiB'
    for number, line in enumerate(raw.decode('utf-8', errors='replace').split('\n'), 1):
        if LEGACY_FORMAT.search(line):
            found['legacy_format'].append(number)
        if any(permits_squash(c) for c in re.split(r'[;|]', line)):
            found['merge_override'].append(number)
        if BRANCH_THREE_PART.search(line):
            found['old_branch_format'].append(number)
    return found, None


def classify(checkout):
    checkout = Path(checkout)
    if not checkout.is_dir():
        return {'path': str(checkout), 'status': 'NOT_FOUND', 'files': {}}
    root = checkout.resolve()
    git = checkout / '.git'
    kind = 'main checkout' if git.is_dir() else ('linked worktree' if git.exists() else 'not a git checkout')
    files, flagged, unverified = {}, False, False
    for name in FILES:
        target = checkout / name
        resolved = target.resolve()
        if not resolved.is_file():
            continue
        entry = {'mtime': resolved.stat().st_mtime, 'symlink': target.is_symlink(), 'legacy_format': [],
                 'merge_override': [], 'old_branch_format': []}
        # Every path component counts: a linked parent directory or file that resolves outside the checkout
        # is not read, because it could point at anything.
        if not resolved.is_relative_to(root):
            found, skipped = None, 'resolves outside the checkout'
        else:
            found, skipped = scan_file(resolved)
        if found is None:
            entry['skipped'] = skipped
            unverified = True
        else:
            entry.update(found)
            flagged = flagged or any(found.values())
        files[name] = entry
    if not files:
        status = 'NO_PROJECT_FILE'
    elif flagged:
        status = 'OVERRIDE_REVIEW'
    elif unverified:
        status = 'UNVERIFIED'
    else:
        status = 'ADOPTED'
    return {'path': str(checkout), 'checkout': kind, 'status': status, 'files': files}


def elapsed_seconds(pid):
    """Seconds since the process started, from `ps -o etime=` ([[dd-]hh:]mm:ss, portable to macOS); None if gone."""
    out = subprocess.run(['ps', '-o', 'etime=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()
    match = re.fullmatch(r'(?:(?:(\d+)-)?(\d+):)?(\d+):(\d+)', out)
    if not match:
        return None
    days, hours, minutes, seconds = (int(g or 0) for g in match.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def process_matches(pid, started_ms, tolerance=120):
    """Live pid whose start time matches the session record (guards against pid reuse)."""
    try:
        os.kill(pid, 0)
    except PermissionError:
        pass
    except OSError:
        return 'gone'
    elapsed = elapsed_seconds(pid)
    if elapsed is None:
        return 'gone'
    return 'live' if abs((time.time() - elapsed) - started_ms / 1000) <= tolerance else 'pid-reused'


def sessions(directory, instruction_mtime, stats=None):
    rows = []
    stats = stats if stats is not None else {}
    stats.setdefault('unparsed', 0)
    for record in sorted(Path(directory).glob('*.json')):
        try:
            data = json.loads(record.read_text())
            pid, cwd, started = int(data['pid']), str(data['cwd']), int(data['startedAt'])
        except (OSError, ValueError, KeyError, TypeError):
            stats['unparsed'] += 1
            continue
        state = process_matches(pid, started)
        if state != 'live':
            continue
        # Longest project path that contains the session's cwd, else the global file alone.
        cwd = os.path.realpath(cwd)
        owner = max((p for p in instruction_mtime if p != '*' and (cwd == p or cwd.startswith(p + '/'))), key=len, default='*')
        stale = started / 1000 < instruction_mtime[owner]
        rows.append({'pid': pid, 'cwd': cwd, 'status': data.get('status', '?'),
                     'started': time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(started / 1000)),
                     'reload': 'RELOAD_NEEDED' if stale else 'current'})
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--map', required=True)
    parser.add_argument('--global-file', default=str(Path.home() / '.claude/CLAUDE.md'))
    parser.add_argument('--sessions-dir', default=str(Path.home() / '.claude/sessions'))
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    mapping = json.loads(Path(args.map).read_text())
    if not isinstance(mapping, dict) or not all(
            isinstance(v, list) and v and all(isinstance(p, str) and os.path.isabs(os.path.expanduser(p)) for p in v)
            for v in mapping.values()):
        sys.exit('refusing: --map must be {"Product": ["/abs/path", ...]} with a non-empty list of absolute paths')
    projects = {name: [classify(os.path.realpath(os.path.expanduser(p))) for p in paths] for name, paths in mapping.items()}
    global_mtime = Path(args.global_file).stat().st_mtime if Path(args.global_file).is_file() else 0
    per_cwd = {'*': global_mtime}
    for rows in projects.values():
        for row in rows:
            if row['files']:
                per_cwd[row['path']] = max(global_mtime, max(i['mtime'] for i in row['files'].values()))
    stats = {}
    live = sessions(args.sessions_dir, per_cwd, stats) if Path(args.sessions_dir).is_dir() else []
    if stats.get('unparsed'):
        print('note: %d session records could not be parsed; the record format may have changed' % stats['unparsed'],
              file=sys.stderr)
    result = {'projects': projects, 'sessions': live}
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print('project coverage (counts and line numbers only)')
    for name, rows in projects.items():
        for row in rows:
            marks = []
            for file_name, info in row['files'].items():
                for kind in ('legacy_format', 'merge_override', 'old_branch_format'):
                    if info[kind]:
                        marks.append('%s %s@%s' % (file_name, kind, ','.join(map(str, info[kind][:5]))))
            for file_name, info in row['files'].items():
                if info.get('skipped'):
                    marks.append('%s not scanned (%s)' % (file_name, info['skipped']))
            print('%-12s %-16s %-15s %s' % (name, row['status'], row.get('checkout', '-'), '; '.join(marks) or '-'))
            print('             %s' % row['path'])
    print('live sessions: %d; reload needed: %d' % (len(live), sum(r['reload'] == 'RELOAD_NEEDED' for r in live)))
    for row in live:
        print('  pid %-6d %-14s %-13s started %s  %s' % (row['pid'], row['status'], row['reload'], row['started'], row['cwd']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
