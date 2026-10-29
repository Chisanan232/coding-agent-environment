#!/usr/bin/env python3
"""Shared development resource lifecycle (SPE-130): evidence-driven inventory, classification and bounded GC.

  resource-lifecycle.py inventory --self-session ID[,ID...] [--out DIR] [--consumers FILE] [--sessions-dir DIR]
  resource-lifecycle.py verify    --manifest FILE
  resource-lifecycle.py apply     --manifest FILE --self-session ID [--ids a,b] [--dry-run]

Coordination layer only. It is NOT a cleanup engine of its own: Glomeris keeps discovery, policy and its typed
deletions; the SPE-109 adapter keeps admission and reservations. This tool adds what neither records: who owns a
directory, whether anything is using it right now, whether it holds unique evidence, and an exact-scope manifest.

`inventory` is read-only (it writes only its own report and manifests under --out, mode 0700/0600). Per resource it
records logical bytes, ALLOCATED bytes, the newest modification time, open handles and process working directories,
the owning session and product, the rebuild cost, and the protective markers found. Allocated bytes come from `du`
(block counts): APFS clones and shared blocks are counted once per path, so allocated is an UPPER BOUND on what a
deletion frees. The only reclaim number this tool reports is the measured change in filesystem free space.

Classes (age alone is never proof of inactivity):
  A  owned by this session (a `.ca-owner.json` receipt naming it), reproducible, no open handle, no protective
     marker, no escaping symlink. The only class `apply` deletes without an explicit id list.
  B  shared or foreign reproducible resources, and scratch of sessions that are no longer alive. Exact-scope manifest;
     `apply` deletes one only when named in --ids (the caller's host permission prompt is the ASK) and revalidation passes.
  C  unique evidence, credentials, source, active or unknown ownership, or anything that could not be fully checked.
     Never deleted.

Revalidation: `apply` re-measures and re-classifies each entry immediately before deleting it, compares the
recorded fingerprint, refuses a path whose real location moved or escaped its root, and deletes with the fd-based
symlink-safe remover. Any new handle, marker or fingerprint change skips the entry.
"""
import argparse
import datetime
import glob
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
OWNER_RECEIPT = '.ca-owner.json'
KEEP_MARKER = '.ca-keep'
CREDENTIAL = re.compile(r'(^\.env($|\.)|credential|secret|(^|[._-])token($|[._-])|auth\.json$|\.pem$|\.p12$|\.pfx$|'
                        r'id_(rsa|ed25519|ecdsa)|\.key$|\.keychain)', re.IGNORECASE)
EVIDENCE = re.compile(r'(^|[._-])(evidence|signoff|sign-off|attestation|receipt|qa-report|ledger|manifest)'
                      r'($|[._-])', re.IGNORECASE)
PRODUCTS = (('AASM', r'aaasm|aasm|agent-assembly'), ('Fornax', r'fornax|fornx'), ('NDA', r'(^|[-_./])nda([-_./]|$)'),
            ('Libra', r'libra|glomeris'), ('Circinus', r'circinus'), ('Octans', r'octans'), ('Ophiuchus', r'ophiuchus'),
            ('Eltanin', r'eltanin'), ('Eridanus', r'eridanus'), ('Horologium', r'horologium'), ('UAAP', r'uaap'))
# kind -> (reproducible, rebuild cost, shallow marker scan). Shallow kinds hold millions of generated files.
KINDS = {
    'cargo_target': (True, 'high: full cold rebuild', True),
    'package_cache': (True, 'medium: network redownload', True),
    'ide_cache': (True, 'low: indexes rebuild', True),
    'session_scratch': (False, 'not reproducible: may hold unique QA evidence', False),
    'tmp_tree': (False, 'not reproducible: may hold copies of private files', False),
    'worktree': (False, 'not reproducible: branch state', False),
    'unknown': (False, 'unknown', False),
}
# Never a deletion root, whatever a consumers file says.
PROTECTED_ROOTS = tuple(str(HOME / p) for p in ('', '.claude', '.codex', '.agents', '.ssh', '.gnupg', '.config', 'Documents',
                                                 'Desktop', 'Library', 'Bryant-Developments', '.local', '.cargo'))


def default_consumers(tmp=None):
    d = HOME
    tmp = Path(tmp or os.environ.get('TMPDIR') or '/tmp')
    dev = d / 'Bryant-Developments'
    spec = [
        ('cargo_target', [str(d / '.cargo/shared-target'), str(d / '.cargo/nda-targets'), str(d / '.cargo/isolated-target*'),
                          str(d / '.cargo/isolated-targets/*'), str(d / 'aaasm-campaign-*'), str(d / '*-target-*'),
                          str(dev / '.cargo-target*'), str(dev / '.cargo-targets/*'), str(dev / '.*-target')]),
        ('package_cache', [str(dev / '.pnpm-store'), str(d / 'Library/pnpm'), str(d / '.npm'), str(d / '.cache/uv'),
                           str(d / 'Library/Caches/pypoetry'), str(d / '.cache/pre-commit'),
                           str(d / 'Library/Caches/ms-playwright'), str(d / 'Library/Caches/Homebrew'),
                           str(d / 'Library/Developer/Xcode/DerivedData')]),
        ('ide_cache', [str(d / 'Library/Caches/JetBrains')]),
        ('session_scratch', ['/private/tmp/claude-%d/*/*' % os.getuid()]),
        ('tmp_tree', [str(tmp / 'rehearse.*'), '/private/tmp/claude-%d/*/*/scratchpad/*' % os.getuid()]),
        ('worktree', [str(dev / '*')]),
    ]
    return spec


def du_kb(path, apparent=False):
    """Block (or apparent) kilobytes of one path via du; None on failure or timeout."""
    cmd = ['du', '-sk']
    if apparent:
        cmd.insert(1, '-A' if sys.platform == 'darwin' else '--apparent-size')
    try:
        out = subprocess.run(cmd + [str(path)], capture_output=True, text=True, timeout=300).stdout.split()
        return int(out[0]) if out else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def free_bytes(path='/System/Volumes/Data'):
    probe = path if os.path.exists(path) else '/'
    st = os.statvfs(probe)
    return st.f_bavail * st.f_frsize


def open_paths():
    """(paths with an open handle or used as a working directory, known). One lsof for everything."""
    try:
        out = subprocess.run(['lsof', '-nP', '-w', '-F', 'n'], capture_output=True, text=True, timeout=120).stdout
    except (OSError, subprocess.TimeoutExpired):
        return set(), False
    paths = {l[1:] for l in out.splitlines() if l.startswith('n/')}
    return paths, bool(paths)


def has_handle(root, paths):
    prefix = str(root).rstrip('/') + '/'
    return any(p == str(root) or p.startswith(prefix) for p in paths)


def etime_seconds(pid):
    out = subprocess.run(['ps', '-o', 'etime=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()
    m = re.fullmatch(r'(?:(?:(\d+)-)?(\d+):)?(\d+):(\d+)', out)
    if not m:
        return None
    d, h, mi, s = (int(g or 0) for g in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


def live_sessions(sessions_dir):
    """{sessionId: pid} for session records whose pid is alive and whose start time still matches (pid reuse safe)."""
    live = {}
    for record in Path(sessions_dir).glob('*.json'):
        try:
            d = json.loads(record.read_text())
            pid, sid, started = int(d['pid']), str(d['sessionId']), int(d['startedAt'])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        elapsed = etime_seconds(pid)
        if elapsed is not None and abs((time.time() - elapsed) - started / 1000) <= 120:
            live[sid] = pid
    return live


def scan_markers(root, shallow, limit=200000):
    """Protective markers and symlink escapes. Shallow kinds look two levels deep only. Returns (markers, complete)."""
    markers, count = set(), 0
    root = str(root)
    real_root = os.path.realpath(root)
    stack = [(root, 0)]
    while stack:
        cur, depth = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except OSError:
            markers.add('unreadable')
            continue
        for e in entries:
            count += 1
            if count > limit:
                return markers, False
            name = e.name
            if name == KEEP_MARKER:
                markers.add('keep-marker')
            if CREDENTIAL.search(name):
                markers.add('credential-name')
            if EVIDENCE.search(name):
                markers.add('evidence-name')
            if e.is_symlink():
                target = os.path.realpath(e.path)
                if not (target == real_root or target.startswith(real_root + '/')):
                    markers.add('escaping-symlink')
                continue
            if e.is_dir(follow_symlinks=False):
                if name == '.git':
                    markers.add('git-metadata')
                if not shallow or depth < 1:
                    stack.append((e.path, depth + 1))
    return markers, True


def session_from_path(path):
    """Session id for anything under /private/tmp/claude-UID/<project>/<sessionId>/..., else None."""
    m = re.match(r'^/private/tmp/claude-\d+/[^/]+/([0-9a-f-]{36})(/|$)', str(path))
    return m.group(1) if m else None


def newest_mtime(path, depth=2):
    """Newest modification time among the root and its entries to a bounded depth (cheap activity signal)."""
    newest = int(os.lstat(path).st_mtime)
    stack = [(str(path), 0)]
    while stack:
        cur, d = stack.pop()
        try:
            for e in os.scandir(cur):
                newest = max(newest, int(e.stat(follow_symlinks=False).st_mtime))
                if d + 1 < depth and e.is_dir(follow_symlinks=False):
                    stack.append((e.path, d + 1))
        except OSError:
            continue
    return newest


def read_receipt(root):
    try:
        d = json.loads((Path(root) / OWNER_RECEIPT).read_text())
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def product_of(path):
    for name, rx in PRODUCTS:
        if re.search(rx, str(path), re.IGNORECASE):
            return name
    return 'unattributed'


def worktree_state(root):
    """('clean-reconciled' | 'dirty' | 'unpushed' | 'unknown') for a linked git worktree."""
    def git(*a):
        return subprocess.run(['git', '-C', str(root)] + list(a), capture_output=True, text=True, timeout=60)
    try:
        if git('status', '--porcelain').stdout.strip():
            return 'dirty'
        up = git('rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}')
        if up.returncode != 0:
            return 'unpushed'
        if git('rev-list', '--count', '@{u}..HEAD').stdout.strip() not in ('0', ''):
            return 'unpushed'
        return 'clean-reconciled'
    except (OSError, subprocess.TimeoutExpired):
        return 'unknown'


def fingerprint(root):
    st = os.lstat(root)
    return {'dev': st.st_dev, 'ino': st.st_ino, 'mtime': int(st.st_mtime)}


def classify(rec, ctx):
    """Deterministic class and reasons from a measured record. Anything uncertain lands in C."""
    root, kind = rec['path'], rec['kind']
    why = []
    receipt = rec.get('receipt')
    owned = bool(receipt and receipt.get('session_id') in ctx['self_sessions'] and receipt.get('reproducible') is True)
    reproducible = KINDS.get(kind, KINDS['unknown'])[0]
    if os.path.islink(root):
        why.append('root-is-symlink')
    if any(root == p or root == p + '/' for p in PROTECTED_ROOTS):
        why.append('protected-root')
    for m in sorted(rec['markers']):
        # An owner may assert, in its receipt, that name-based evidence guesses (a copied ledger file) are false
        # positives. Credentials, keep markers and escaping symlinks can never be waived this way.
        if m == 'evidence-name' and owned and receipt.get('evidence_free') is True:
            continue
        why.append('marker:' + m)
    if not rec['scan_complete']:
        why.append('scan-incomplete')
    if not ctx['handles_known']:
        why.append('open-handles-unknown')
    elif has_handle(root, ctx['handles']):
        why.append('active:open-handle-or-cwd')
    if rec.get('session_alive') and not (owned and rec.get('session') in ctx['self_sessions']):
        why.append('active:live-session')
    if kind == 'worktree' and rec.get('worktree_state') != 'clean-reconciled':
        why.append('worktree-' + str(rec.get('worktree_state')))
    if kind == 'unknown':
        why.append('unknown-kind')
    if why:
        return 'C', why
    # SPE-109/ADR-0001: shared or redirected Cargo targets are never preauthorizable. A target is class A only when its
    # receipt claims it is private to the owner, and the machine-wide shared target never is.
    shared_target = os.path.realpath(root) == os.path.realpath(str(HOME / '.cargo/shared-target'))
    private_ok = kind != 'cargo_target' or (receipt.get('private') is True and not shared_target) if owned else False
    if owned and private_ok and kind in ('cargo_target', 'package_cache', 'ide_cache', 'tmp_tree', 'session_scratch'):
        return 'A', ['owned-reproducible-idle']
    if reproducible or kind in ('session_scratch', 'tmp_tree', 'worktree'):
        reason = ['shared-or-foreign'] if not owned else ['owned-but-not-claimed-private-needs-ask']
        if kind in ('session_scratch', 'tmp_tree'):
            reason = ['owner-session-not-alive', 'may-hold-unique-evidence']
        elif kind == 'worktree':
            reason = ['worktree-clean-reconciled-needs-ask']
        return 'B', reason
    return 'C', ['not-classifiable']


def measure(path, kind, ctx):
    shallow = KINDS.get(kind, KINDS['unknown'])[2]
    markers, complete = scan_markers(path, shallow)
    logical, allocated = du_kb(path, apparent=True), du_kb(path)
    session_id = session_from_path(path)
    session_alive = bool(session_id) and (session_id in ctx['live_sessions'] or session_id in ctx['self_sessions'])
    try:
        newest = newest_mtime(path)
    except OSError:
        newest = 0
    rec = {'path': str(path), 'kind': kind, 'product': product_of(path), 'session': session_id,
           'session_alive': session_alive, 'logical_bytes': None if logical is None else logical * 1024,
           'allocated_bytes': None if allocated is None else allocated * 1024, 'newest_mtime': newest,
           'rebuild_cost': KINDS.get(kind, KINDS['unknown'])[1], 'markers': sorted(markers), 'scan_complete': complete,
           'receipt': read_receipt(path), 'fingerprint': fingerprint(path)}
    if kind == 'worktree':
        rec['worktree_state'] = worktree_state(path)
    return rec


def make_ctx(self_session, sessions_dir=None):
    handles, known = open_paths()
    return {'self_session': self_session, 'self_sessions': set(filter(None, (self_session or '').split(','))),
            'handles': handles, 'handles_known': known,
            'live_sessions': live_sessions(sessions_dir or HOME / '.claude/sessions')}


def discover(consumers):
    seen, out = set(), []
    for kind, patterns in consumers:
        for pattern in patterns:
            for hit in sorted(glob.glob(pattern)):
                real = os.path.realpath(hit)
                if hit in seen or not os.path.isdir(hit):
                    continue
                if kind == 'worktree' and not (Path(hit) / '.git').is_file():
                    continue  # a main checkout (.git directory) or a plain directory is not a linked worktree
                seen.add(hit)
                out.append((kind, hit, real))
    return out


def inventory(args):
    consumers = json.loads(Path(args.consumers).read_text()) if args.consumers else default_consumers()
    ctx = make_ctx(args.self_session, args.sessions_dir)
    before = free_bytes()
    records = []
    for kind, hit, real in discover(consumers):
        rec = measure(hit, kind, ctx)
        rec['class'], rec['reasons'] = classify(rec, ctx)
        records.append(rec)
    records.sort(key=lambda r: -(r['allocated_bytes'] or 0))
    out = Path(args.out or HOME / '.local/state/coding-agent-environment/gc')
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    report = {'schema': 1, 'generated': stamp, 'free_bytes': before, 'self_session': args.self_session,
              'handles_known': ctx['handles_known'], 'resources': records,
              'note': 'allocated_bytes is an upper bound (APFS clones/shared blocks); only measured free-space change counts as reclaimed'}
    path = out / ('inventory-%s.json' % stamp)
    write_private(path, report)
    for cls in 'ABC':
        manifest = {'schema': 1, 'class': cls, 'generated': stamp, 'free_bytes_at_generation': before,
                    'self_session': args.self_session,
                    'entries': [dict(id=entry_id(r), **r) for r in records if r['class'] == cls]}
        write_private(out / ('manifest-%s-%s.json' % (cls, stamp)), manifest)
    summary(records, before)
    print('report: %s' % path)
    return 0


def entry_id(rec):
    return hashlib.sha256(rec['path'].encode()).hexdigest()[:12]


def write_private(path, obj):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(obj, handle, indent=2, sort_keys=True)


def gib(n):
    return 'n/a' if n is None else '%.1f GiB' % (n / 2 ** 30)


def summary(records, free):
    print('free space now: %s (ground truth; allocated sizes below are upper bounds)' % gib(free))
    totals = {}
    for r in records:
        totals.setdefault(r['class'], [0, 0])
        totals[r['class']][0] += 1
        totals[r['class']][1] += r['allocated_bytes'] or 0
    for cls in 'ABC':
        n, b = totals.get(cls, [0, 0])
        print('class %s: %d resources, <= %s allocated' % (cls, n, gib(b)))
    for r in records[:15]:
        print('  %s %-9s %-10s %s  [%s]' % (r['class'], r['product'], r['kind'], gib(r['allocated_bytes']),
                                            ','.join(r['reasons'])[:60]))


def load_manifest(path):
    d = json.loads(Path(path).read_text())
    if d.get('schema') != 1 or not isinstance(d.get('entries'), list):
        raise SystemExit('refusing: unrecognised manifest')
    return d


def revalidate(entry, ctx):
    """(still_class, reasons): fresh measurement of the same path, compared to what the manifest recorded."""
    path = entry['path']
    if not os.path.isdir(path) or os.path.islink(path):
        return 'GONE', ['path-missing-or-now-a-symlink']
    now = measure(path, entry['kind'], ctx)
    if now['fingerprint']['dev'] != entry['fingerprint']['dev'] or now['fingerprint']['ino'] != entry['fingerprint']['ino']:
        return 'C', ['identity-changed']
    cls, why = classify(now, ctx)
    if now['newest_mtime'] != entry['newest_mtime']:
        why = list(why) + ['modified-since-manifest']
        cls = 'C' if cls == 'A' else cls
    return cls, why


def verify(args):
    manifest = load_manifest(args.manifest)
    ctx = make_ctx(manifest.get('self_session'))
    bad = 0
    for entry in manifest['entries']:
        cls, why = revalidate(entry, ctx)
        ok = cls == manifest['class']
        bad += not ok
        print('%s %s %s %s' % ('OK  ' if ok else 'STOP', entry['id'], cls, ','.join(why)))
    return 1 if bad else 0


def safe_remove(path):
    """Remove one directory tree with the fd-based symlink-safe remover; never follows a link."""
    if not shutil.rmtree.avoids_symlink_attacks:
        raise SystemExit('refusing: this platform has no symlink-safe recursive remover')
    shutil.rmtree(path)


def apply(args):
    manifest = load_manifest(args.manifest)
    if set(filter(None, (manifest.get('self_session') or '').split(','))) != set(filter(None, args.self_session.split(','))):
        raise SystemExit('refusing: the manifest was generated for a different owner session')
    ids = set(filter(None, (args.ids or '').split(',')))
    cls = manifest['class']
    if cls == 'C':
        raise SystemExit('refusing: class C is never deleted')
    if cls == 'B' and not ids:
        raise SystemExit('refusing: class B needs explicit --ids (the host ASK covers exactly these entries)')
    state = Path(args.out or HOME / '.local/state/coding-agent-environment/gc')
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    audit = state / 'gc-audit.jsonl'
    removed = 0
    free_start = free_bytes()
    for entry in manifest['entries']:
        if cls == 'B' and entry['id'] not in ids:
            continue
        if ids and entry['id'] not in ids:
            continue
        ctx = make_ctx(args.self_session)
        now_cls, why = revalidate(entry, ctx)
        record = {'ts': time.time(), 'id': entry['id'], 'path': entry['path'], 'class': cls, 'now': now_cls, 'why': why}
        if now_cls != cls:
            record['action'] = 'skipped'
        elif args.dry_run:
            record['action'] = 'would-remove'
        else:
            before = free_bytes()
            try:
                safe_remove(entry['path'])
                record['action'] = 'removed'
                removed += 1
            except OSError as error:
                record['action'] = 'failed:%s' % type(error).__name__
            record['free_delta_bytes'] = free_bytes() - before
        with open(audit, 'a') as handle:
            handle.write(json.dumps(record, sort_keys=True) + '\n')
        os.chmod(audit, 0o600)
        print('%-12s %s %s' % (record['action'], entry['id'], ','.join(why)))
    print('removed %d; measured free-space change over the batch: %+d bytes (%s)' %
          (removed, free_bytes() - free_start, 'ground truth, not the logical sizes'))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='cmd', required=True)
    inv = sub.add_parser('inventory')
    inv.add_argument('--self-session', required=True)
    inv.add_argument('--out')
    inv.add_argument('--consumers')
    inv.add_argument('--sessions-dir')
    ver = sub.add_parser('verify')
    ver.add_argument('--manifest', required=True)
    app = sub.add_parser('apply')
    app.add_argument('--manifest', required=True)
    app.add_argument('--self-session', required=True)
    app.add_argument('--ids')
    app.add_argument('--out')
    app.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    return {'inventory': inventory, 'verify': verify, 'apply': apply}[args.cmd](args)


if __name__ == '__main__':
    sys.exit(main())
