#!/usr/bin/env python3
"""Producer -> independent Codex QA hand-off, as local files and one headless `codex exec` (SPE-131).

  qa-handoff.py prepare --repo PATH --sha SHA --ticket T --product P --criteria FILE
                        [--producer-evidence FILE] [--qa-notes FILE] [--root DIR]
  qa-handoff.py run     --run-dir DIR [--timeout SECONDS]
  qa-handoff.py verdict --run-dir DIR

Automates only the missing hop. It follows the existing independent-QA campaign convention (a pinned private clone at
the exact commit, an isolated HOME/TMPDIR/CARGO_TARGET_DIR, offline, a packet, a resource admission check) and uses
AASM's worker result contract (STATUS / BASELINE / VERIFIED / SUSPECTED_FINDINGS / UNTESTED_OR_BLOCKED / CONFIDENCE)
as the single verdict schema. No daemon, database, broker or cloud service: the producer's own session runs `run`
(foreground, or in the background and read at completion) and acts on the exit code and RESULT.json.

What "independent" means here: a separate Codex process with a fresh `--ephemeral` model context that sees only the
packet and the repository at the exact commit, never the producer's transcript. It is NOT filesystem or configuration
isolation: the Codex sandbox can still read other files this user can read, and the run inherits the user's Codex
config (hooks, MCP servers). The wrapper therefore checks the facts itself, not the verdict text: the pinned clone's
HEAD equals the packet SHA before and after, no tracked file changed, the event stream shows commands were actually
executed, and the event stream and verdict are hashed at exit so a later edit is detected.

Only the headless `codex exec` interface is used. There is deliberately no option to target a tmux pane or an
interactive session: an interactive session that is not callable through a supported interface is not a worker.

Exit codes of `run` and `verdict` (the producer's continuation rule):
  0  QA_PASS            complete, every check passed, nothing untested, no Critical/High/Medium finding
  10 QA_FAIL            a check failed or a Critical/High/Medium finding exists: fix, new SHA, requalify
  11 QA_INCOMPLETE      partial, blocked, untested items, timeout, or no command was executed: not a pass
  12 QA_INVALID         integrity failure (SHA mismatch, modified source, tampered or malformed verdict)
  75 WAITING_RESOURCE   admission refused: retry later; this is not a QA failure and codex was not started
A Codex verdict is QA evidence only. It is never merge evidence and must not be written as an owner-merge review.
"""
import argparse
import datetime
import hashlib
import json
import os
import shlex
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
DEFAULT_ROOT = HOME / '.local/state/coding-agent-environment/qa'
SEVERITIES = ('Critical', 'High', 'Medium', 'Low')
FAILING = ('Critical', 'High', 'Medium')
EXIT = {'QA_PASS': 0, 'QA_FAIL': 10, 'QA_INCOMPLETE': 11, 'QA_INVALID': 12, 'WAITING_RESOURCE': 75}
ENV_ALLOW = ('PATH', 'LANG', 'LC_ALL', 'TERM', 'TZ', 'USER', 'LOGNAME', 'SHELL')

STR = {'type': 'string'}


def _obj(props, required=None):
    return {'type': 'object', 'additionalProperties': False, 'required': list(required or props), 'properties': props}


VERDICT_SCHEMA = _obj({
    'status': {'type': 'string', 'enum': ['COMPLETE', 'PARTIAL', 'BLOCKED']},
    'baseline': STR,
    'verified': {'type': 'array', 'items': _obj({'check': STR, 'result': {'type': 'string', 'enum': ['PASS', 'FAIL']},
                                                  'evidence': STR})},
    'suspected_findings': {'type': 'array', 'items': _obj({
        'id': STR, 'severity': {'type': 'string', 'enum': list(SEVERITIES)}, 'surface': STR, 'expected': STR,
        'actual': STR, 'evidence': STR, 'confidence': {'type': 'string', 'enum': ['HIGH', 'MEDIUM', 'LOW']}})},
    'untested_or_blocked': {'type': 'array', 'items': _obj({'surface': STR, 'reason': STR})},
    'confidence': {'type': 'string', 'enum': ['HIGH', 'MEDIUM', 'LOW']},
})

PROMPT = """You are an independent QA reviewer. You have no connection to the producer of this change and you have not
seen their work: you receive only the packet below and a clean checkout of the repository at the exact commit.

Rules:
1. First run `git rev-parse HEAD`. Your BASELINE must read `<product> @ <that full sha>`.
2. Re-derive every verdict by running real commands in this repository and observing real behavior. The producer's
   evidence is a claim, not a result; do not copy it. Reading source is not evidence that the running product behaves.
3. A criterion you did not exercise goes in untested_or_blocked. Never record an inferred PASS.
4. For each security-critical claim run a negative control (an input that must be rejected) and a positive control
   (a similar input that must succeed), and cite both.
5. Do not modify any tracked file. Scratch work goes only under $TMPDIR, $HOME and the out directory given below.
   No network, no ticket-system or GitHub calls, no pushes.
6. Resource contention (busy lock, saturated lane, refused admission, a build the machine cannot afford) is BLOCKED,
   not a failed check. Do not start a heavy build unless the QA notes allow it, and then only through the product's
   resource wrapper.
7. Stop once you have enough evidence for PASS or for the defect.
8. Answer with ONLY a JSON object matching the supplied schema.

PACKET (product={product}, ticket={ticket}, commit={sha})

ACCEPTANCE CRITERIA (verbatim from the ticket):
{criteria}

PRODUCER-CLAIMED EVIDENCE (unverified; do not rely on it):
{producer}

QA NOTES (scope, allowed commands, how to run things):
{notes}

Out directory for any evidence files: {out}
"""


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')


def safe_digest(path):
    """Digest of a regular file, 'ABSENT' if missing, 'UNREADABLE' if it is a link or otherwise not a plain file."""
    if not os.path.lexists(path):
        return 'ABSENT'
    try:
        return sha256_file(path)
    except OSError:
        return 'UNREADABLE'


def sha256_file(path):
    return hashlib.sha256(read_nofollow(path)).hexdigest()


def write_private(path, data, mode=0o600):
    """Create or replace a wrapper-owned file without ever following a link: unlink first, then exclusive create."""
    text = data if isinstance(data, str) else json.dumps(data, indent=2, sort_keys=True)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'w') as handle:
        handle.write(text)


def read_nofollow(path):
    """Bytes of a regular file, refusing a symlink (the QA process can plant one where it can write)."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError('not a regular file')
        return handle.read()


# Git run by the wrapper never trusts the clone's own configuration: a QA process that can write .git/config could
# otherwise make `git status` execute a command (core.fsmonitor, filter drivers) outside the sandbox. After the QA
# run the wrapper does not run git against the clone at all; see check_tree.
SAFE_GIT = ['-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null', '-c', 'core.pager=cat',
            '-c', 'protocol.ext.allow=never', '-c', 'core.untrackedCache=false']


def git(repo, *args, check=True):
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
           'GIT_OPTIONAL_LOCKS': '0', 'GIT_TERMINAL_PROMPT': '0', 'HOME': '/nonexistent'}
    done = subprocess.run(['git', '-C', str(repo)] + SAFE_GIT + list(args), capture_output=True, text=True, timeout=300,
                          env=env)
    if check and done.returncode != 0:
        raise SystemExit('git %s failed: %s' % (args[0], done.stderr.strip()[:200]))
    return done


def tree_manifest(source):
    """{path: [kind, digest]} for every tracked path at HEAD, hashed from the working tree without executing git
    config. Taken at prepare time from the fresh clone, which is trusted."""
    listing = git(source, 'ls-tree', '-r', '-z', 'HEAD').stdout
    manifest = {}
    for entry in filter(None, listing.split('\0')):
        meta, path = entry.split('\t', 1)
        mode = meta.split()[0]
        if mode == '160000':
            continue
        manifest[path] = hash_path(Path(source) / path)
    return manifest


def hash_path(full):
    try:
        if os.path.islink(full):
            return ['link', os.readlink(full)]
        return ['file', hashlib.sha256(read_nofollow(full)).hexdigest()]
    except OSError:
        return ['missing', '']


def check_tree(source, manifest):
    """Problems found comparing the clone to the manifest; reads files and .git/HEAD only, executes nothing."""
    problems = []
    try:
        head = read_nofollow(Path(source) / '.git/HEAD').decode().strip()
    except OSError:
        head = ''
    if head != manifest['__head__']:
        problems.append('HEAD of the pinned clone moved off the packet SHA')
    changed = [p for p, want in manifest['files'].items() if hash_path(Path(source) / p) != want]
    if changed:
        problems.append('tracked files changed in the pinned clone: %s' % ', '.join(sorted(changed)[:5]))
    return problems


def packet_digest(packet):
    body = {k: v for k, v in packet.items() if k != 'digest'}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def load_packet(run_dir):
    packet = json.loads((Path(run_dir) / 'packet.json').read_text())
    if packet.get('digest') != packet_digest(packet):
        raise SystemExit('refusing: packet digest does not match (edited after prepare)')
    return packet


def read_optional(path):
    return Path(path).read_text() if path else '(none supplied)'


def prepare(args):
    repo = Path(args.repo).resolve()
    sha = git(repo, 'rev-parse', '--verify', args.sha + '^{commit}').stdout.strip()
    parent = Path(args.root or DEFAULT_ROOT) / args.product / args.ticket
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    n = 1
    while (parent / ('%s-%d' % (sha[:12], n))).exists():
        n += 1
    run_dir = parent / ('%s-%d' % (sha[:12], n))
    run_dir.mkdir(mode=0o700)
    for sub in ('home', 'tmp', 'out', 'cargo-target'):
        (run_dir / sub).mkdir(mode=0o700)
    source = run_dir / 'source'
    subprocess.run(['git', 'clone', '--local', '--no-checkout', '-q', str(repo), str(source)], check=True,
                   capture_output=True, timeout=600)
    git(source, 'checkout', '-q', '--detach', sha)
    write_private(run_dir / 'tree-manifest.json', {'__head__': sha, 'files': tree_manifest(source)})
    packet = {'schema': 1, 'product': args.product, 'ticket': args.ticket, 'repo': str(repo), 'sha': sha,
              'created': now(), 'criteria': Path(args.criteria).read_text(),
              'producer_evidence': read_optional(args.producer_evidence), 'qa_notes': read_optional(args.qa_notes)}
    packet['digest'] = packet_digest(packet)
    write_private(run_dir / 'packet.json', packet)
    print(run_dir)
    return 0


def isolated_env(run_dir):
    env = {k: os.environ[k] for k in ENV_ALLOW if k in os.environ}
    home = str(run_dir / 'home')
    env.update({'HOME': home, 'TMPDIR': str(run_dir / 'tmp'), 'XDG_CONFIG_HOME': home + '/.config',
                'XDG_DATA_HOME': home + '/.local/share', 'XDG_CACHE_HOME': home + '/.cache',
                'CARGO_TARGET_DIR': str(run_dir / 'cargo-target'), 'CARGO_NET_OFFLINE': 'true',
                # Codex finds its own login here; no credential is copied into the isolated HOME.
                'CODEX_HOME': os.environ.get('CODEX_HOME') or str(HOME / '.codex')})
    if os.environ.get('CARGO_HOME'):
        env['CARGO_HOME'] = os.environ['CARGO_HOME']
    return env


def codex_argv(codex_bin, run_dir):
    """Explicit sandbox flags on every call: the user's Codex config is full-access with approvals off, so a missing
    flag would hand the QA run the real home. No -m: model routing is not touched."""
    out, source = run_dir / 'out', run_dir / 'source'
    argv = [codex_bin, 'exec', '--skip-git-repo-check', '--json', '--ephemeral', '-s', 'workspace-write',
            '-C', str(source), '--output-schema', str(run_dir / 'verdict.schema.json'), '-o', str(out / 'verdict.json')]
    for sub in ('out', 'tmp', 'home', 'cargo-target'):
        argv += ['--add-dir', str(run_dir / sub)]
    return argv + ['-']


def count_commands(events_path):
    """Completed command_execution items in the codex JSON event stream."""
    completed = 0
    with open(events_path, errors='replace') as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            item = event.get('item') if isinstance(event, dict) else None
            if event.get('type') == 'item.completed' and isinstance(item, dict) and item.get('type') == 'command_execution':
                completed += 1
    return completed


def admitted(args):
    cmd = args.admission_cmd or [sys.executable, str(Path(__file__).resolve().parent / 'disk-admission.py'), 'check',
                                 '--weight', 'light']
    try:
        return subprocess.run(cmd, capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def write_result(run_dir, packet, state, reasons, extra=None):
    result = {'state': state, 'reasons': reasons, 'sha': packet['sha'], 'ticket': packet['ticket'],
              'product': packet['product'], 'generated': now(),
              'note': 'QA evidence only; never merge evidence and never an owner-merge review record'}
    result.update(extra or {})
    write_private(Path(run_dir) / 'RESULT.json', result)
    print('%s %s' % (state, '; '.join(reasons) if reasons else ''))
    return EXIT[state]


def run(args):
    run_dir = Path(args.run_dir).resolve()
    packet = load_packet(run_dir)
    source = run_dir / 'source'
    if (run_dir / 'RUNMETA.json').exists():
        raise SystemExit('refusing: this run directory has already been run; prepare a new attempt')
    manifest = json.loads((run_dir / 'tree-manifest.json').read_text())
    problems = check_tree(source, manifest)
    if problems or manifest['__head__'] != packet['sha']:
        return write_result(run_dir, packet, 'QA_INVALID', problems or ['manifest does not match the packet'])
    if not admitted(args):
        return write_result(run_dir, packet, 'WAITING_RESOURCE', ['disk admission refused; codex was not started'])
    out = run_dir / 'out'
    # Wrapper-owned inputs sit in the run directory itself, which the QA sandbox cannot write (only source, out, tmp,
    # home and cargo-target are writable roots).
    write_private(run_dir / 'verdict.schema.json', VERDICT_SCHEMA)
    prompt = PROMPT.format(product=packet['product'], ticket=packet['ticket'], sha=packet['sha'],
                           criteria=packet['criteria'], producer=packet['producer_evidence'], notes=packet['qa_notes'],
                           out=out)
    write_private(run_dir / 'prompt.txt', prompt)
    argv = codex_argv(args.codex_bin, run_dir)
    events = run_dir / 'events.jsonl'
    t0 = time.time()
    timed_out = False
    with open(events, 'wb') as ev, open(run_dir / 'stderr.log', 'wb') as err:
        os.chmod(events, 0o600)
        proc = subprocess.Popen(argv, cwd=str(source), env=isolated_env(run_dir), stdin=subprocess.PIPE, stdout=ev,
                                stderr=err, start_new_session=True)
        try:
            proc.communicate(prompt.encode(), timeout=args.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)   # our own child's process group only
                except OSError:
                    break
                try:
                    proc.wait(timeout=5)
                    break
                except subprocess.TimeoutExpired:
                    continue
    os.chmod(run_dir / 'stderr.log', 0o600)
    verdict = out / 'verdict.json'
    meta = {'argv': argv[:-1], 'exit_code': proc.returncode, 'seconds': round(time.time() - t0, 1), 'timed_out': timed_out,
            'events_sha256': sha256_file(events), 'verdict_sha256': safe_digest(verdict),
            'commands_completed': count_commands(events)}
    write_private(run_dir / 'RUNMETA.json', meta)
    return evaluate(run_dir)


def validate(value, schema, path='verdict'):
    """Minimal JSON-schema check for the subset VERDICT_SCHEMA uses; returns a list of problems."""
    problems = []
    kind = schema.get('type')
    if kind == 'object':
        if not isinstance(value, dict):
            return ['%s: not an object' % path]
        for key in schema['required']:
            if key not in value:
                problems.append('%s: missing %s' % (path, key))
        for key, sub in value.items():
            if key not in schema['properties']:
                problems.append('%s: unexpected key %s' % (path, key))
            else:
                problems += validate(sub, schema['properties'][key], '%s.%s' % (path, key))
    elif kind == 'array':
        if not isinstance(value, list):
            return ['%s: not an array' % path]
        for i, item in enumerate(value):
            problems += validate(item, schema['items'], '%s[%d]' % (path, i))
    elif kind == 'string':
        if not isinstance(value, str):
            return ['%s: not a string' % path]
        if 'enum' in schema and value not in schema['enum']:
            problems.append('%s: %r not allowed' % (path, value))
    return problems


def evaluate(run_dir):
    run_dir = Path(run_dir).resolve()
    packet = load_packet(run_dir)
    meta_path, verdict_path, events = run_dir / 'RUNMETA.json', run_dir / 'out/verdict.json', run_dir / 'events.jsonl'
    if not meta_path.exists():
        return write_result(run_dir, packet, 'QA_INVALID', ['no run record'])
    meta = json.loads(meta_path.read_text())
    bad = []
    if safe_digest(events) != meta['events_sha256']:
        bad.append('event stream changed after the run')
    if meta['verdict_sha256'] == 'UNREADABLE' or safe_digest(verdict_path) == 'UNREADABLE':
        return write_result(run_dir, packet, 'QA_INVALID', bad + ['verdict is not a regular file (link planted?)'])
    if not os.path.lexists(verdict_path):
        if meta.get('timed_out'):
            return write_result(run_dir, packet, 'QA_INCOMPLETE', ['timed out before a verdict'])
        return write_result(run_dir, packet, 'QA_INVALID', ['no verdict file'])
    if safe_digest(verdict_path) != meta['verdict_sha256']:
        bad.append('verdict changed after the run (tampered)')
    bad += check_tree(run_dir / 'source', json.loads((run_dir / 'tree-manifest.json').read_text()))
    try:
        verdict = json.loads(read_nofollow(verdict_path))
    except ValueError:
        return write_result(run_dir, packet, 'QA_INVALID', bad + ['verdict is not JSON'])
    problems = validate(verdict, VERDICT_SCHEMA)
    if problems:
        return write_result(run_dir, packet, 'QA_INVALID', bad + problems[:5])
    if packet['sha'] not in verdict['baseline'] and packet['sha'][:12] not in verdict['baseline']:
        bad.append('verdict baseline does not name the packet SHA')
    if bad:
        return write_result(run_dir, packet, 'QA_INVALID', bad)
    extra = {'verdict_sha256': meta['verdict_sha256'], 'events_sha256': meta['events_sha256'],
             'commands_completed': meta['commands_completed'], 'findings': [
                 {'id': f['id'], 'severity': f['severity'], 'surface': f['surface']} for f in verdict['suspected_findings']]}
    failing = [c['check'] for c in verdict['verified'] if c['result'] == 'FAIL']
    failing += ['%s %s' % (f['severity'], f['id']) for f in verdict['suspected_findings'] if f['severity'] in FAILING]
    if failing:
        return write_result(run_dir, packet, 'QA_FAIL', failing[:10], extra)
    incomplete = []
    if meta['commands_completed'] < 1:
        incomplete.append('no command was executed (the run did not exercise the change)')
    if meta.get('timed_out'):
        incomplete.append('timed out')
    if verdict['status'] != 'COMPLETE':
        incomplete.append('status %s' % verdict['status'])
    if not verdict['verified']:
        incomplete.append('nothing verified')
    incomplete += ['untested: %s' % u['surface'] for u in verdict['untested_or_blocked']]
    if incomplete:
        return write_result(run_dir, packet, 'QA_INCOMPLETE', incomplete[:10], extra)
    return write_result(run_dir, packet, 'QA_PASS', [], extra)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('prepare')
    p.add_argument('--repo', required=True)
    p.add_argument('--sha', required=True)
    p.add_argument('--ticket', required=True)
    p.add_argument('--product', required=True)
    p.add_argument('--criteria', required=True)
    p.add_argument('--producer-evidence')
    p.add_argument('--qa-notes')
    p.add_argument('--root')
    r = sub.add_parser('run')
    r.add_argument('--run-dir', required=True)
    r.add_argument('--timeout', type=int, default=1800)
    r.add_argument('--codex-bin', default='codex', help=argparse.SUPPRESS)       # test hook
    r.add_argument('--admission-cmd', type=shlex.split, help=argparse.SUPPRESS)   # test hook (one quoted string)
    v = sub.add_parser('verdict')
    v.add_argument('--run-dir', required=True)
    args = parser.parse_args(argv)
    if args.cmd == 'verdict':
        return evaluate(args.run_dir)
    return {'prepare': prepare, 'run': run}[args.cmd](args)


if __name__ == '__main__':
    sys.exit(main())
