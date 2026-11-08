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

`apply` re-checks every file right before its batch is unlinked (same inode, link count, mtime, older than --min-age-hours,
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
# Recorded roots are real paths, so /tmp appears as /private/tmp.
DEFAULT_PREFIXES = tuple(str(p) for p in (HOME / 'Bryant-Developments', Path('/private/tmp'), Path('/private/var/folders')))


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
            raw = handle.read(D_READ_LIMIT + 1)
    except OSError:
        return set()
    if len(raw) > D_READ_LIMIT:
        return set()                      # a line cut at the limit could be a partial path that looks absent: no evidence
    text = raw.decode('utf-8', 'replace')
    return {line[len(ROOT_LINE):].strip() for line in text.splitlines() if line.startswith(ROOT_LINE)}


def valid_prefix(prefix):
    """An absolute, normalised prefix at least two components deep that is neither the home directory nor above it."""
    if not (isinstance(prefix, str) and os.path.isabs(prefix) and os.path.normpath(prefix) == prefix.rstrip('/')
            and len([c for c in prefix.split('/') if c]) >= 2):
        return False
    home = str(HOME)
    return not (prefix.rstrip('/') == home or home.startswith(prefix.rstrip('/') + '/'))


def prefix_of(root, prefixes):
    """The live prefix a recorded root sits under, or None. A path below an unmounted or renamed prefix proves nothing."""
    if not os.path.isabs(root) or '\0' in root or os.path.normpath(root) != root:
        return None
    for prefix in prefixes:
        base = prefix.rstrip('/')
        if valid_prefix(base) and root.startswith(base + '/') and os.path.isdir(base):
            return base
    return None


def probe_root(root, prefix):
    """'absent' only when the walk from the prefix down finds a component that is definitely not there (ENOENT/ENOTDIR) and no
    component before it is a symlink. 'present' when the root exists or the answer is unclear (permission error, other I/O
    error), 'unsafe' when a symlink sits on the way (it may point at an unmounted volume)."""
    current = prefix
    for part in [c for c in root[len(prefix):].split('/') if c]:
        current = os.path.join(current, part)
        try:
            info = os.lstat(current)
        except (FileNotFoundError, NotADirectoryError):
            return 'absent'
        except OSError:
            return 'present'
        if stat.S_ISLNK(info.st_mode):
            return 'unsafe'
    return 'present'


def verdict_for(roots, prefixes, probe=probe_root):
    if not roots:
        return 'keep:no-recorded-root'
    if len(roots) > 1:
        return 'keep:conflicting-roots'
    root = next(iter(roots))
    prefix = prefix_of(root, prefixes)
    if prefix is None:
        return 'keep:root-outside-prefixes'
    state = probe(root, prefix)
    return {'absent': 'obsolete', 'present': 'keep:root-present'}.get(state, 'keep:symlinked-ancestor')


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
    """Atomic: write a private temporary file next to the target, then rename it over."""
    tmp = '%s.tmp%d' % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(text)
    os.replace(tmp, path)


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
        return {'deps': str(deps), 'done': [], 'done_at': {}, 'started': time.time(), 'seen': 0, 'keep': {}, 'obsolete': 0,
                'prefixes': []}
    progress = json.loads(text)
    if progress.get('deps') != str(deps):
        raise SystemExit('refusing: the state directory holds a classification of a different deps directory')
    return progress


def classify_partition(deps, digit, prefixes, deadline, out_lines):
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
            root_cache[key] = verdict_for(roots, prefixes)
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


def classify(args):
    deps = str(check_target(args.deps)[0] / 'deps')
    state = private_state(state_dir(args.state), create=True)
    prefixes = tuple(args.prefix) if args.prefix else DEFAULT_PREFIXES
    bad = [p for p in prefixes if not valid_prefix(p.rstrip('/') if isinstance(p, str) else p)]
    if bad:
        raise SystemExit('refusing: prefix must be an absolute normalised path at least two levels deep: %s' % bad[0])
    if args.reset:
        for path in plan_paths(state):
            if path.exists():
                load_private(path)           # only removes files this tool wrote (private, regular, ours)
                path.unlink()
    progress = load_progress(state, deps)
    if progress['done'] and progress.get('prefixes') != sorted(prefixes):
        raise SystemExit('refusing: this classification used a different --prefix set; run classify --reset to redo it')
    progress['prefixes'] = sorted(prefixes)
    deadline = time.monotonic() + args.max_seconds
    obsolete_file = plan_paths(state)[1]
    for digit in '0123456789abcdef':
        if digit in progress['done']:
            continue
        if time.monotonic() > deadline:
            break
        lines = []
        seen, keep, complete = classify_partition(deps, digit, prefixes, deadline, lines)
        if not complete:
            print('time budget reached inside partition %s; it is redone on the next run' % digit)
            break
        fd = os.open(obsolete_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a') as handle:
            handle.write(''.join(line + '\n' for line in lines))
        progress['done'].append(digit)
        progress.setdefault('done_at', {})[digit] = time.time()
        progress['seen'] += seen
        progress['obsolete'] += len(lines)
        for reason, nbytes in keep.items():
            progress['keep'][reason] = progress['keep'].get(reason, 0) + nbytes
        write_private(plan_paths(state)[0], json.dumps(progress, sort_keys=True))
        print('partition %s: %d files, %d obsolete' % (digit, seen, len(lines)))
    return report_progress(progress, state)


def read_plan(state):
    text = load_private(plan_paths(state)[1]) or ''
    seen, out = set(), []
    for line in text.splitlines():
        if not line:
            continue
        try:
            e = json.loads(line)
            ok = (isinstance(e, dict) and isinstance(e.get('name'), str) and '\0' not in e['name'] and '/' not in e['name']
                  and isinstance(e.get('ino'), int) and isinstance(e.get('nlink'), int) and isinstance(e.get('mtime'), int)
                  and isinstance(e.get('blocks'), int) and isinstance(e.get('root'), str) and '\0' not in e['root'])
        except ValueError:
            ok = False
        match = GROUP.match(e['name']) if ok else None
        if (not match or kind_of(match.group('rest')) != e.get('kind') or e['kind'] not in RECLAIMABLE
                or (e['name'], e['ino']) in seen):
            continue                        # malformed, tampered, mismatched or duplicated (crash resume) entries are ignored
        seen.add((e['name'], e['ino']))
        out.append(e)
    return out


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


def free_bytes(path):
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def apply(args, free_fn=None):
    profile, _ = check_target(args.deps)
    deps = str(profile / 'deps')
    free_fn = free_fn or (lambda: free_bytes(deps))     # the filesystem that holds deps, not a fixed volume
    state = private_state(state_dir(args.state))
    progress = load_progress(state, deps)
    prefixes = progress.get('prefixes') or []
    if len(progress['done']) < 16:
        print('note: classification covers %d/16 partitions; only classified files can be removed' % len(progress['done']))
    oldest = min(progress.get('done_at', {}).values(), default=progress['started'])
    if time.time() - oldest > PLAN_MAX_AGE_S:
        raise SystemExit('refusing: part of the classification is older than 7 days; run classify --reset to redo it')
    entries = read_plan(state)
    now = time.time()
    ready = eligible(entries, now, args.min_age_hours * 3600)
    planned = collections.Counter(e['ino'] for e in entries)
    print('dry run: %s' % (not args.execute))
    print('ESTIMATE, hard-link-deduplicated, before re-checks: %.2f GiB in %d files' % (dedup_bytes(ready) / GIB, len(ready)))
    held = take_locks(profile)
    if not args.execute:
        release_locks(held)                      # a dry run only proves no build is active right now
        held = []
    audit = state / 'audit.jsonl'
    pfd = os.open(deps, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    removed = skipped = 0
    started, total_expected, total_measured = time.monotonic(), 0, 0
    cap = args.max_gib * GIB

    def fresh(entry):
        """Everything classify relied on, checked again: same regular file and inode, link count, mtime, age, and a recorded
        root that is still definitely absent under a prefix that still exists with no symlink on the way."""
        try:
            st = os.lstat(entry['name'], dir_fd=pfd)
        except OSError:
            return False
        if (not stat.S_ISREG(st.st_mode) or st.st_ino != entry['ino'] or st.st_nlink != entry['nlink']
                or planned[entry['ino']] != st.st_nlink or int(st.st_mtime) != entry['mtime']
                or time.time() - st.st_mtime < args.min_age_hours * 3600):
            return False
        prefix = prefix_of(entry['root'], prefixes)
        return prefix is not None and probe_root(entry['root'], prefix) == 'absent'

    batch, expect = [], 0

    def flush():
        """Unlink the batch (each inode's links together, only when every link is still fresh) and compare the estimate with the
        measured free-space change. False when the batch freed under STOP_RATIO of its estimate."""
        nonlocal batch, expect, total_expected, total_measured, removed, skipped
        groups = collections.defaultdict(list)
        for item in batch:
            groups[item['ino']].append(item)
        batch, expect = [], 0
        todo = [g for g in groups.values() if all(fresh(i) for i in g)]
        skipped += sum(len(g) for g in groups.values()) - sum(len(g) for g in todo)
        if not todo:
            return True
        estimate = sum(i['blocks'] * 512 // i['nlink'] for g in todo for i in g)
        before = free_fn()
        for group in todo:
            if not all(fresh(i) for i in group):      # once more, right before this inode's links go
                skipped += len(group)
                continue
            for item in group:
                try:
                    os.unlink(item['name'], dir_fd=pfd)
                    removed += 1
                except OSError:
                    skipped += 1
        time.sleep(1)
        delta = free_fn() - before
        total_expected += estimate
        total_measured += delta
        print('batch: %d files, estimate %.2f GiB, measured free-space change %+.2f GiB' % (
            sum(len(g) for g in todo), estimate / GIB, delta / GIB))
        return delta >= STOP_RATIO * estimate

    shares = collections.Counter()
    for e in ready:
        shares[e['ino']] += e['blocks'] * 512 // e['nlink']
    try:
        previous = None
        # Links of one inode stay adjacent and in one batch: removing only some of them frees nothing yet.
        for entry in sorted(ready, key=lambda e: (e['ino'], e['name'])):
            if entry['ino'] != previous:
                limit = min(args.batch_gib * GIB, cap - total_expected)
                if args.execute and batch and expect >= limit and not flush():
                    print('STOP: a batch freed under %d%% of its estimate; investigate before continuing' % (STOP_RATIO * 100))
                    break
                if (time.monotonic() - started > args.max_seconds or total_expected + expect >= cap
                        or total_expected + expect + shares[entry['ino']] > cap):
                    break                      # the cap is never overshot by the next inode group either
                previous = entry['ino']
            if args.execute:
                batch.append(entry)
                expect += entry['blocks'] * 512 // entry['nlink']
            elif fresh(entry):
                removed += 1
                total_expected += entry['blocks'] * 512 // entry['nlink']
            else:
                skipped += 1
        if args.execute and batch:
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
            p.add_argument('--reset', action='store_true', help='discard the stored classification and redo it')
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
