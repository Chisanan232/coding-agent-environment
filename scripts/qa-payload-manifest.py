#!/usr/bin/python3 -I
"""Producer-side helper for QA payloads (SPE-145 part 9): generate and check payload/MANIFEST.json (cae-qa-payload/1).

  qa-payload-manifest.py plan   --dir DIR --scope TEXT [--producer-run PATH ...] [--selftests FILE] [--write]
                                [--endpoint-daemon PATH --endpoint-contract ID]
  qa-payload-manifest.py verify --dir DIR

Unprivileged and offline. `plan` lists every regular file of DIR (except MANIFEST.json) with its SHA-256, size and kind (exec when
the file has an execute bit, data otherwise), adds the self-tests from FILE (a JSON list of entries with id, argv, stdin,
expect_exit, expect_stdout_sha256, timeout_s and max_output_bytes: this tool cannot run them, so their expected output digests come
from you), validates the whole document with the same rules the QA run applies, and prints the manifest on stdout and a summary on
stderr; `--write` also writes DIR/MANIFEST.json. `verify` re-checks DIR against its MANIFEST.json exactly as the QA run will: the
exact file set, sizes, SHA-256, single links, no links or special files, no extra directories. The rules are read from qa-side.py
next to this file (one copy of them), executed from the very bytes read once. See docs/QA-PAYLOAD.md.
"""
import argparse
import hashlib
import json
import os
import stat
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True


def load_rules():
    path = HERE / 'qa-side.py'
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o022:
        raise SystemExit('qa-side.py must sit next to this tool as a regular file that is not group or world writable')
    with open(path, 'rb') as handle:
        data = handle.read()
    module = types.ModuleType('qa_side')
    module.__file__ = str(path)
    exec(compile(data, str(path), 'exec'), module.__dict__)
    return module


def scan(side, directory):
    """[(relative path, kind, size, sha256)] for every regular file under directory, sorted; links, special files and the
    manifest itself are handled by the caller."""
    found, root = [], str(directory)
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs[:]:
            if os.path.islink(os.path.join(current, name)):
                raise SystemExit('%s is a link; the payload may contain regular files only' % os.path.join(current, name))
        for name in files:
            full = os.path.join(current, name)
            rel = os.path.relpath(full, root)
            if rel == side.MANIFEST_NAME:
                continue
            st = os.lstat(full)
            if not stat.S_ISREG(st.st_mode):
                raise SystemExit('%s is a link or not a regular file' % rel)
            digest = hashlib.sha256()
            with open(full, 'rb') as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b''):
                    digest.update(chunk)
            found.append((rel, 'exec' if st.st_mode & 0o111 else 'data', st.st_size, digest.hexdigest()))
    return sorted(found)


def build_manifest(side, directory, scope, producer_run, selftests, endpoint=None):
    """cae-qa-payload/1 unless an endpoint block is given, then /2 (each self-test then carries needs_endpoint, false when left out)."""
    entries = [{'path': p, 'sha256': sha, 'size': size, 'kind': kind} for p, kind, size, sha in scan(side, directory)]
    if endpoint is None:
        return {'schema': side.PAYLOAD_SCHEMA, 'files': entries, 'selftests': selftests, 'scope': scope, 'producer_run': producer_run}
    tests = [dict(t, needs_endpoint=t.get('needs_endpoint', False)) if isinstance(t, dict) else t for t in selftests]
    return {'schema': side.PAYLOAD_SCHEMA_2, 'files': entries, 'selftests': tests, 'scope': scope, 'producer_run': producer_run,
            'endpoint': endpoint}


def render(manifest):
    return (json.dumps(manifest, indent=2) + '\n').encode()


def plan(args, side):
    selftests = []
    if args.selftests:
        with open(args.selftests, 'rb') as handle:
            selftests = json.loads(handle.read(1 << 20).decode('utf-8'))
    if bool(args.endpoint_daemon) != bool(args.endpoint_contract):
        raise SystemExit('--endpoint-daemon and --endpoint-contract go together')
    endpoint = {'daemon': args.endpoint_daemon, 'contract': args.endpoint_contract} if args.endpoint_daemon else None
    manifest = build_manifest(side, args.dir, args.scope, list(args.producer_run or []), selftests, endpoint)
    raw = render(manifest)
    side.parse_manifest(raw)                                    # the QA run's own rules, before anything is written
    sys.stderr.write('files: %d  bytes: %d  self-tests: %d  producer_run: %d\n' % (
        len(manifest['files']), sum(f['size'] for f in manifest['files']), len(selftests), len(manifest['producer_run'])))
    for entry in manifest['files']:
        sys.stderr.write('  %-5s %10d  %s  %s\n' % (entry['kind'], entry['size'], entry['sha256'], entry['path']))
    sys.stderr.write('manifest sha256 %s\n' % hashlib.sha256(raw).hexdigest())
    if args.write:
        target = Path(args.dir) / side.MANIFEST_NAME
        if os.path.lexists(target):
            os.unlink(target)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw)
        sys.stderr.write('wrote %s\n' % target)
    sys.stdout.buffer.write(raw)
    return 0


def verify(args, side):
    fd = os.open(args.dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            raw = side.read_manifest_at(fd, os.getuid())
            manifest = side.parse_manifest(raw)
        except side.PayloadError as error:
            print('NOT OK: %s' % error)
            return 1
        problems = side.verify_tree(fd, manifest, raw, os.getuid())
        for entry in manifest['files']:
            mode = os.lstat(os.path.join(args.dir, entry['path'])).st_mode if os.path.lexists(os.path.join(args.dir, entry['path'])) else 0
            if entry['kind'] == 'exec' and not mode & 0o111:
                problems.append('%s is listed kind=exec but has no execute bit here' % entry['path'])
        if problems:
            print('NOT OK:\n  ' + '\n  '.join(problems))
            return 1
        print('OK manifest sha256 %s (%d files, %d self-tests)' % (side.manifest_sha256(raw), len(manifest['files']), len(manifest['selftests'])))
        return 0
    finally:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='mode', required=True)
    p = sub.add_parser('plan')
    p.add_argument('--dir', required=True)
    p.add_argument('--scope', required=True)
    p.add_argument('--producer-run', action='append')
    p.add_argument('--selftests')
    p.add_argument('--endpoint-daemon', help='payload-relative path of the candidate daemon (a kind=exec file); makes a cae-qa-payload/2 manifest')
    p.add_argument('--endpoint-contract', help='endpoint contract id, e.g. fornax-daemon/1')
    p.add_argument('--write', action='store_true')
    v = sub.add_parser('verify')
    v.add_argument('--dir', required=True)
    args = parser.parse_args(argv)
    side = load_rules()
    try:
        return {'plan': plan, 'verify': verify}[args.mode](args, side)
    except side.PayloadError as error:
        print('REFUSED: %s' % error)
        return 1


if __name__ == '__main__':
    sys.exit(main())
