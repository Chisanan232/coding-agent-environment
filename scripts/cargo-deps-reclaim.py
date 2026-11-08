#!/usr/bin/env python3
"""Evidence-gated reclaim of a shared Cargo target's `<profile>/deps` (SPE-164).

  cargo-deps-reclaim.py classify --deps DIR [--state DIR] [--max-seconds N] [--prefix P ...]
  cargo-deps-reclaim.py report   --deps DIR [--state DIR]
  cargo-deps-reclaim.py apply    --deps DIR [--state DIR] [--execute] [--batch-gib G] [--max-gib G] [--max-seconds N]

`classify` is read-only and resumable: it works through the 16 hash-prefix partitions of `deps/` inside a time budget and
records finished partitions, so a directory of millions of files is classified in bounded batches. `apply` is a dry run
unless --execute is given.

Attribution comes from the artifact's own record, not from age: the `# env-dep:CARGO_MANIFEST_DIR=` line in its `.d` file
names the source root it was built from. A group (crate name + 16-hex hash) is obsolete only when that one root lies under
a configured prefix whose directory still exists and the root itself is gone. Everything else is kept and counted by
reason: root present, no recorded root, conflicting roots, root outside the prefixes, executables and shared libraries
(a QA ledger may reference a test binary), unrecognised names. `.d` files are never deleted.

`apply` re-checks every file immediately before unlinking it (same inode, link count, mtime, older than --min-age-hours,
root still missing) and unlinks only when EVERY hard link of the inode is in the deletion set, because removing one link
of a shared inode frees nothing. It holds Cargo's own build locks for the whole run (so no build starts and none is
running: a lock it cannot take means a build is active and nothing is deleted), refuses a target with a keep marker, and
stops when a batch's measured free-space change is under 30% of the hard-link-deduplicated estimate. The estimate and the
measured change are printed separately; only the measured change counts as reclaimed.
"""
import argparse
import collections
import fcntl
import json
import os
import re
import stat
import sys
import time
from pathlib import Path

HOME = Path.home()
GIB = 2 ** 30
KEEP_MARKER = '.ca-keep'
LOCK_NAMES = ('.cargo-lock', '.cargo-build-lock', '.cargo-artifact-lock')
GROUP = re.compile(r'^(?P<stem>.+?)-(?P<hash>[0-9a-f]{16})(?P<rest>\..*)?$')
ROOT_LINE = '# env-dep:CARGO_MANIFEST_DIR='
D_READ_LIMIT = 1 << 20
# `.d` files stay: they are the evidence (and tiny), and the policy forbids touching Cargo metadata.
RECLAIMABLE = {'rcgu.o', 'o', 'rlib', 'rmeta'}
EXECUTABLE = {'exe', 'dylib', 'so'}
PLAN_MAX_AGE_S = 7 * 86400
STOP_RATIO = 0.3
DEFAULT_PREFIXES = tuple(str(p) for p in (HOME / 'Bryant-Developments', Path('/private/tmp'), Path('/tmp'),
                                          Path('/private/var/folders')))


def state_dir(arg=None):
    return Path(arg) if arg else HOME / '.local/state/coding-agent-environment/cargo-deps'


def kind_of(rest):
    if rest is None:
        return 'exe'
    rest = rest[1:]
    if rest.endswith('rcgu.o'):
        return 'rcgu.o'
    if rest in ('o', 'rlib', 'rmeta', 'd'):
        return rest
    return 'dylib' if rest in ('dylib', 'so') else 'other'


def recorded_roots(path):
    """Distinct CARGO_MANIFEST_DIR values in one .d file (bounded read; unreadable means no evidence)."""
    try:
        with open(path, 'rb') as handle:
            text = handle.read(D_READ_LIMIT).decode('utf-8', 'replace')
    except OSError:
        return set()
    return {line[len(ROOT_LINE):].strip() for line in text.splitlines() if line.startswith(ROOT_LINE)}


def under_live_prefix(root, prefixes):
    """The root sits under a prefix that exists now. A path below an unmounted or renamed prefix proves nothing."""
    for prefix in prefixes:
        base = prefix.rstrip('/')
        if root.startswith(base + '/') and os.path.isdir(prefix):
            return True
    return False


def verdict_for(roots, prefixes, exists=os.path.lexists):
    if not roots:
        return 'keep:no-recorded-root'
    if len(roots) > 1:
        return 'keep:conflicting-roots'
    root = next(iter(roots))
    if not os.path.isabs(root) or '\0' in root:
        return 'keep:no-recorded-root'
    if not under_live_prefix(root, prefixes):
        return 'keep:root-outside-prefixes'
    return 'keep:root-present' if exists(root) else 'obsolete'


def check_target(deps):
    """(profile dir, target root) for a real Cargo deps directory, or SystemExit."""
    deps = Path(deps)
    if deps.is_symlink() or not deps.is_dir() or deps.name != 'deps':
        raise SystemExit('refusing: --deps must be a real directory named deps')
    profile = deps.resolve().parent
    root = profile.parent
    if not ((root / 'CACHEDIR.TAG').is_file() or (root / '.rustc_info.json').is_file()):
        raise SystemExit('refusing: %s does not look like a Cargo target directory' % root)
    for place in (root, profile, deps):
        if (place / KEEP_MARKER).exists():
            raise SystemExit('refusing: keep marker in %s' % place)
    return profile, root


def write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(text)


def private_state(directory, create=False):
    directory = Path(directory)
    if create:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        info = directory.lstat()
    except OSError:
        raise SystemExit('refusing: state directory is missing')
    if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise SystemExit('refusing: state directory must be a private (0700) directory owned by this user')
    return directory


def load_private(path):
    try:
        info = Path(path).lstat()
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise SystemExit('refusing: %s is not a private file written by this tool' % path)
    return Path(path).read_text()


def plan_paths(state):
    return state / 'progress.json', state / 'obsolete.jsonl'


def load_progress(state, deps):
    text = load_private(plan_paths(state)[0])
    if text is None:
        return {'deps': str(deps), 'done': [], 'started': time.time(), 'seen': 0, 'keep': {}, 'obsolete': 0}
    progress = json.loads(text)
    if progress.get('deps') != str(deps):
        raise SystemExit('refusing: the state directory holds a classification of a different deps directory')
    return progress


def classify_partition(deps, digit, prefixes, deadline, out_lines, exists=os.path.lexists):
    """Classify every file whose hash starts with `digit`. Returns (seen, keep-by-reason bytes, completed)."""
    groups = collections.defaultdict(list)
    with os.scandir(deps) as entries:
        for entry in entries:
            match = GROUP.match(entry.name)
            if match and match.group('hash')[0] == digit:
                groups[(match.group('stem'), match.group('hash'))].append((entry.name, match.group('rest')))
    keep, seen, root_cache = collections.defaultdict(int), 0, {}
    for (stem, digest), files in groups.items():
        if time.monotonic() > deadline:
            return seen, keep, False
        roots = set()
        for name, rest in files:
            if kind_of(rest) == 'd':
                roots |= recorded_roots(os.path.join(deps, name))
        key = tuple(sorted(roots))
        if key not in root_cache:
            root_cache[key] = verdict_for(roots, prefixes, exists)
        group_verdict = root_cache[key]
        for name, rest in files:
            kind = kind_of(rest)
            try:
                st = os.lstat(os.path.join(deps, name))
            except OSError:
                continue
            seen += 1
            share = st.st_blocks * 512 // max(1, st.st_nlink)
            if not stat.S_ISREG(st.st_mode):
                keep['keep:not-a-regular-file'] += share
            elif group_verdict != 'obsolete':
                keep[group_verdict] += share
            elif kind in EXECUTABLE:
                keep['keep:executable-or-library'] += share
            elif kind == 'd':
                keep['keep:metadata'] += share
            elif kind not in RECLAIMABLE:
                keep['keep:unrecognised-name'] += share
            else:
                out_lines.append(json.dumps({'name': name, 'ino': st.st_ino, 'nlink': st.st_nlink, 'mtime': int(st.st_mtime),
                                             'blocks': st.st_blocks, 'root': next(iter(roots)), 'kind': kind}, sort_keys=True))
    return seen, keep, True


def classify(args, exists=os.path.lexists):
    deps = str(check_target(args.deps)[0] / 'deps')
    state = private_state(state_dir(args.state), create=True)
    progress = load_progress(state, deps)
    prefixes = tuple(args.prefix) if args.prefix else DEFAULT_PREFIXES
    deadline = time.monotonic() + args.max_seconds
    obsolete_file = plan_paths(state)[1]
    for digit in '0123456789abcdef':
        if digit in progress['done']:
            continue
        if time.monotonic() > deadline:
            break
        lines = []
        seen, keep, complete = classify_partition(deps, digit, prefixes, deadline, lines, exists)
        if not complete:
            print('time budget reached inside partition %s; it is redone on the next run' % digit)
            break
        fd = os.open(obsolete_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a') as handle:
            handle.write(''.join(line + '\n' for line in lines))
        progress['done'].append(digit)
        progress['seen'] += seen
        progress['obsolete'] += len(lines)
        for reason, nbytes in keep.items():
            progress['keep'][reason] = progress['keep'].get(reason, 0) + nbytes
        progress['updated'] = time.time()
        write_private(plan_paths(state)[0], json.dumps(progress, sort_keys=True))
        print('partition %s: %d files, %d obsolete' % (digit, seen, len(lines)))
    return report_progress(progress, state)


def read_plan(state):
    text = load_private(plan_paths(state)[1]) or ''
    entries = (json.loads(line) for line in text.splitlines() if line)
    return [e for e in entries if e['kind'] in RECLAIMABLE]


def eligible(entries, now, min_age_s):
    """Entries whose every hard link is in the set (the only ones whose removal frees space) and old enough."""
    links = collections.Counter(e['ino'] for e in entries)
    return [e for e in entries if links[e['ino']] == e['nlink'] and now - e['mtime'] >= min_age_s]


def dedup_bytes(entries):
    return sum({e['ino']: e['blocks'] * 512 for e in entries}.values())


def report_progress(progress, state):
    entries = read_plan(state)
    ready = eligible(entries, time.time(), 86400)
    print('partitions done: %d/16, files seen: %d' % (len(progress['done']), progress['seen']))
    print('obsolete files: %d; fully-removable (all hard links inside the set, >24h old): %d' % (len(entries), len(ready)))
    print('ESTIMATE, hard-link-deduplicated: %.2f GiB (not a reclaim figure; per-entry sums would be %.2f GiB)' % (
        dedup_bytes(ready) / GIB, sum(e['blocks'] * 512 for e in ready) / GIB))
    for reason, nbytes in sorted(progress['keep'].items(), key=lambda kv: -kv[1]):
        print('kept  %-28s %.2f GiB' % (reason, nbytes / GIB))
    return 0


def report(args):
    deps = str(check_target(args.deps)[0] / 'deps')
    state = private_state(state_dir(args.state))
    return report_progress(load_progress(state, deps), state)


def take_locks(profile):
    """Non-blocking exclusive flock on every Cargo build lock present. A lock that cannot be taken means a build is running."""
    held = []
    for name in LOCK_NAMES:
        path = profile / name
        if not path.exists():
            continue
        fd = os.open(path, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            release_locks(held)
            raise SystemExit('refusing: a Cargo build lock (%s) is held, so a build is active; nothing was deleted' % name)
        held.append(fd)
    if not held:
        raise SystemExit('refusing: no Cargo build lock found in %s, so an active build cannot be ruled out' % profile)
    return held


def release_locks(held):
    for fd in held:
        os.close(fd)


def free_bytes(path='/System/Volumes/Data'):
    st = os.statvfs(path if os.path.exists(path) else '/')
    return st.f_bavail * st.f_frsize


def apply(args, free_fn=free_bytes, exists=os.path.lexists):
    profile, _ = check_target(args.deps)
    deps = str(profile / 'deps')
    state = private_state(state_dir(args.state))
    progress = load_progress(state, deps)
    if len(progress['done']) < 16:
        print('note: classification covers %d/16 partitions; only classified files can be removed' % len(progress['done']))
    if time.time() - progress.get('updated', progress['started']) > PLAN_MAX_AGE_S:
        raise SystemExit('refusing: the classification is older than 7 days; run classify again')
    entries = read_plan(state)
    now = time.time()
    ready = eligible(entries, now, args.min_age_hours * 3600)
    planned = collections.Counter(e['ino'] for e in entries)
    print('dry run: %s' % (not args.execute))
    print('ESTIMATE, hard-link-deduplicated, before re-checks: %.2f GiB in %d files' % (dedup_bytes(ready) / GIB, len(ready)))
    held = take_locks(profile)
    audit = state / 'audit.jsonl'
    pfd = os.open(deps, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    removed = skipped = 0
    started, total_expected, total_measured = time.monotonic(), 0, 0
    batch, expect = [], 0
    try:
        def flush():
            nonlocal batch, expect, total_expected, total_measured
            if not batch:
                return True
            before = free_fn()
            for item in batch:
                os.unlink(item['name'], dir_fd=pfd)
            time.sleep(1)
            delta = free_fn() - before
            total_expected += expect
            total_measured += delta
            print('batch: %d files, estimate %.2f GiB, measured free-space change %+.2f GiB' % (
                len(batch), expect / GIB, delta / GIB))
            ok = delta >= STOP_RATIO * expect
            batch, expect = [], 0
            return ok
        previous = None
        # Links of one inode stay adjacent and in one batch: removing only some of them frees nothing yet.
        for entry in sorted(ready, key=lambda e: (e['ino'], e['name'])):
            if entry['ino'] != previous:
                if args.execute and expect >= args.batch_gib * GIB and not flush():
                    print('STOP: a batch freed under %d%% of its estimate; investigate before continuing' % (STOP_RATIO * 100))
                    break
                if time.monotonic() - started > args.max_seconds or total_expected / GIB >= args.max_gib:
                    break
                previous = entry['ino']
            try:
                st = os.lstat(entry['name'], dir_fd=pfd)
            except OSError:
                skipped += 1
                continue
            stale = (not stat.S_ISREG(st.st_mode) or st.st_ino != entry['ino'] or st.st_nlink != entry['nlink']
                     or planned[entry['ino']] != st.st_nlink or int(st.st_mtime) != entry['mtime']
                     or now - st.st_mtime < args.min_age_hours * 3600 or exists(entry['root']))
            if stale:
                skipped += 1
                continue
            share = entry['blocks'] * 512 // entry['nlink']
            if args.execute:
                batch.append(entry)
                expect += share
                removed += 1
            else:
                removed += 1
                total_expected += share
        if args.execute:
            flush()
    finally:
        os.close(pfd)
        release_locks(held)
        with os.fdopen(os.open(audit, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600), 'a') as handle:
            handle.write(json.dumps({'ts': time.time(), 'deps': deps, 'execute': args.execute, 'files': removed,
                                     'skipped': skipped, 'estimate_bytes': total_expected,
                                     'measured_free_delta_bytes': total_measured}) + '\n')
    label = 'removed' if args.execute else 'would remove'
    print('%s %d files, skipped %d after re-check; estimate %.2f GiB; measured free-space change %+.2f GiB' % (
        label, removed, skipped, total_expected / GIB, total_measured / GIB))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='cmd', required=True)
    for name in ('classify', 'report', 'apply'):
        p = sub.add_parser(name)
        p.add_argument('--deps', required=True)
        p.add_argument('--state')
        if name == 'classify':
            p.add_argument('--max-seconds', type=int, default=300)
            p.add_argument('--prefix', action='append')
        if name == 'apply':
            p.add_argument('--execute', action='store_true')
            p.add_argument('--batch-gib', type=float, default=2.0)
            p.add_argument('--max-gib', type=float, default=20.0)
            p.add_argument('--max-seconds', type=int, default=600)
            p.add_argument('--min-age-hours', type=float, default=24.0)
    args = parser.parse_args(argv)
    return {'classify': classify, 'report': report, 'apply': apply}[args.cmd](args)


if __name__ == '__main__':
    sys.exit(main())
