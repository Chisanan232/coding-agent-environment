#!/usr/bin/python3 -I
"""The QA side of the Codex QA hand-off: everything that runs as the dedicated QA OS user (SPE-153).

  qa-side.py qa-stage --inbox DIR --outbox DIR --codex-home DIR [--codex-bin PATH]
  qa-side.py qa-exec  --inbox DIR --outbox DIR --codex-home DIR [--codex-bin PATH] [--timeout S] --profile-sha256 HEX

Never run by hand: `qa-handoff.py run --qa-identity NAME` copies this one file into the producer-owned inbox and runs the
copy as the QA user with `/usr/bin/python3 -I`, recording its SHA-256 in RUNMETA.json. It is therefore self-contained and
standard-library only: it imports nothing from the repository, loads no sibling and runs no privileged command. The producer
also imports it for the pieces both sides share (the permission profile, the confinement controls, the isolated environment,
the verdict schema and a few file helpers), so the producer and the QA side cannot drift apart.
"""
import argparse
import hashlib
import json
import os
import pwd
import re
import resource
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import types
import unicodedata
from pathlib import Path

HOME = Path.home()
SEVERITIES = ('Critical', 'High', 'Medium', 'Low')
PROFILE = 'qa_confined'
# Directories under HOME that hold credentials. Directory rules only: the Codex permission profile does not take globs.
SECRET_DIRS = ('.codex', '.ssh', '.aws', '.config', '.claude', '.gnupg', '.netrc', '.npmrc', '.docker', '.kube',
               'Library/Keychains', '.cargo/credentials.toml', '.cargo/credentials')
SYSTEM_READ = ('/usr', '/bin', '/opt/homebrew', '/Library/Developer/CommandLineTools')
CANARY_ROOT = HOME / '.local/state/coding-agent-environment/qa-canary'
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


def sha256_file(path):
    """Streaming SHA-256 of a regular file opened without following links."""
    digest = hashlib.sha256()
    with open_regular(path) as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def write_private(path, data, mode=0o600):
    """Create or replace a wrapper-owned file without ever following a link: unlink first, then exclusive create."""
    text = data if isinstance(data, (str, bytes)) else json.dumps(data, indent=2, sort_keys=True)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb' if isinstance(text, bytes) else 'w') as handle:
        handle.write(text)


def open_regular(path):
    """Open a plain regular file for reading without following links or blocking on a FIFO or device. lstat first,
    then O_NOFOLLOW|O_NONBLOCK, then re-check the opened object is the same regular file (no swap in between)."""
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode):
        raise OSError('not a regular file')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    after = os.fstat(fd)
    if not stat.S_ISREG(after.st_mode) or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
        os.close(fd)
        raise OSError('file changed while opening')
    return os.fdopen(fd, 'rb')


def isolated_env(run_dir, codex_home=None):
    env = {k: os.environ[k] for k in ENV_ALLOW if k in os.environ}
    # A relative or run-directory PATH entry would resolve helper binaries from a directory the QA process can write.
    entries = [e for e in os.environ.get('PATH', '/usr/bin:/bin').split(':') if os.path.isabs(e) and not e.startswith(str(run_dir))]
    env['PATH'] = ':'.join(entries) or '/usr/bin:/bin'
    home = str(run_dir / 'home')
    env.update({'HOME': home, 'TMPDIR': str(run_dir / 'tmp'), 'XDG_CONFIG_HOME': home + '/.config',
                'XDG_DATA_HOME': home + '/.local/share', 'XDG_CACHE_HOME': home + '/.cache',
                'CARGO_TARGET_DIR': str(run_dir / 'cargo-target'), 'CARGO_NET_OFFLINE': 'true',
                # A private Codex home holding only the permission profile and a link to the login (codex_home()).
                'CODEX_HOME': str(codex_home or run_dir / 'codexhome')})
    if os.environ.get('CARGO_HOME'):
        env['CARGO_HOME'] = os.environ['CARGO_HOME']
    return env


def real_codex_home():
    return Path(os.environ.get('CODEX_HOME') or HOME / '.codex')


def profile_toml(run_dir, read_roots=()):
    """Allowlist-only reads, writes confined to the clone and scratch, no network. Measured by the AASM QA campaign with
    `codex sandbox -P` and a real `codex exec` on codex-cli 0.163.0; the wrapper re-proves it before every run (see
    confinement_probe). NOT enforced by the seatbelt: other processes' environments of the same OS user stay readable."""
    fs = {':minimal': 'read'}
    for root in SYSTEM_READ:
        fs[root] = 'read'
    for root in read_roots:
        fs[str(root)] = 'read'
    for sub in ('source', 'out', 'tmp', 'home', 'cargo-target'):
        fs[str(run_dir / sub)] = 'write'
    for rel in SECRET_DIRS:
        fs[str(HOME / rel)] = 'deny'
    fs[str(real_codex_home())] = 'deny'
    # No [projects] table on purpose: the clone is the commit under test, and a trusted project lets its own
    # .codex/config.toml widen this profile (measured: a planted allowlist entry made a canary read ALLOWED).
    lines = ['approval_policy = "never"', 'cli_auth_credentials_store = "file"', 'default_permissions = "%s"' % PROFILE, '',
             '[permissions.%s.filesystem]' % PROFILE]
    lines += ['%s = "%s"' % (json.dumps(k), v) for k, v in fs.items()]
    lines += ['', '[permissions.%s.network]' % PROFILE, 'enabled = false', '']
    return '\n'.join(lines)


def prepare_codex_home(run_dir, read_roots=(), home=None):
    """A private CODEX_HOME per run: the permission profile, and the login only as a link to the real auth file. The link
    is readable by the Codex client (not sandboxed) and denied to the tool commands, whose profile denies the real home.
    A private home also stops the user's own MCP servers and hooks from loading."""
    home = Path(home or run_dir / 'codexhome')
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private(home / 'config.toml', profile_toml(run_dir, read_roots))
    auth = real_codex_home() / 'auth.json'
    link = home / 'auth.json'
    if auth.exists() and not os.path.lexists(link):
        os.symlink(auth.resolve(), link)
    return home


CONTROLS = (
    # (name, python snippet printing ALLOWED or DENIED:<errno>; the name is the first line so a test double can tell them apart)
    ('pos_read_clone', "import sys\ntry:\n    open({clone!r} + '/.git/HEAD').read()\n    print('ALLOWED')\nexcept OSError as e:\n    print('DENIED:%s' % e.errno)", 'ALLOWED'),
    ('pos_write_out', "import sys\ntry:\n    open({out!r} + '/probe.txt', 'w').write('x')\n    print('ALLOWED')\nexcept OSError as e:\n    print('DENIED:%s' % e.errno)", 'ALLOWED'),
    ('neg_read_canary', "import sys\ntry:\n    open({canary!r}, 'rb').read()\n    print('ALLOWED')\nexcept OSError as e:\n    print('DENIED:%s' % e.errno)", 'DENIED'),
    ('neg_list_codex_home', "import os\ntry:\n    os.listdir({codex_home!r})\n    print('ALLOWED')\nexcept OSError as e:\n    print('DENIED:%s' % e.errno)", 'DENIED'),
    ('neg_project_config_ignored', "import sys\ntry:\n    open({canary!r}, 'rb').read()\n    print('ALLOWED')\nexcept OSError as e:\n    print('DENIED:%s' % e.errno)", 'DENIED'),
    ('neg_open_auth_file', "try:\n    open({auth!r}, 'rb').close()\n    print('ALLOWED')\nexcept OSError as e:\n    print('DENIED:%s' % e.errno)", 'DENIED'),
    ('neg_write_outside', "import sys\ntry:\n    open({canary_dir!r} + '/written.txt', 'w').write('x')\n    print('ALLOWED')\nexcept OSError as e:\n    print('DENIED:%s' % e.errno)", 'DENIED'),
    # Network: the profile disables it. A bind that works, or a connect that is not refused outright, means it is not. A denial counts
    # only as EPERM or EACCES (the check below), so a timeout, an unreachable network or 'path too long' (errno None) prove nothing.
    ('neg_loopback_bind', "import socket\ntry:\n    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n    s.bind(('127.0.0.1', 0))\n    s.listen(1)\n    print('ALLOWED')\nexcept OSError as e:\n    print('DENIED:%s' % e.errno)", 'DENIED'),
    ('neg_uds_bind', "import os, socket\nname = 'qa-uds.sock'\ntry:\n    os.chdir({out!r})\n    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n    s.bind(name)\n    s.listen(1)\n    print('ALLOWED')\nexcept OSError as e:\n    print('DENIED:%s' % e.errno)\nfinally:\n    try:\n        os.unlink(name)\n    except OSError:\n        pass", 'DENIED'),
    # 192.0.2.1 is TEST-NET-1 (RFC 5737): never routed. A sandbox refuses at once with EPERM; a timeout would mean nothing.
    ('neg_testnet_connect', "import socket\ntry:\n    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n    s.settimeout(3)\n    s.connect(('192.0.2.1', 9))\n    print('ALLOWED')\nexcept OSError as e:\n    print('DENIED:%s' % e.errno)", 'DENIED'),
)
# Only with a payload. pos_exec_payload runs inside the sandbox whatever exec_probe_argv() names: ALLOWED when the process started (any exit
# status, even a timeout), DENIED:<errno> when the spawn itself is refused. It executes producer-supplied code, so it is the one control
# that is held back until every other control (the base set and neg_write_payload) has run and passed; see confinement_probe.
PAYLOAD_EXEC_CONTROLS = ('pos_exec_payload',)
PAYLOAD_CONTROLS = (
    ('neg_write_payload', "import os\nname = {payload!r} + '/qa-write-probe'\ntry:\n    open(name, 'w').write('x')\n    print('ALLOWED')\n    os.unlink(name)\nexcept OSError as e:\n    print('DENIED:%s' % e.errno)", 'DENIED'),
    ('pos_exec_payload', "import subprocess\ntry:\n    subprocess.run({exec_argv!r}, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)\n    print('ALLOWED')\nexcept subprocess.TimeoutExpired:\n    print('ALLOWED')\nexcept OSError as e:\n    print('DENIED:%s' % e.errno)", 'ALLOWED'),
)


def controls_for(manifest=None):
    """The controls a run must pass. The producer computes this from the manifest it holds, so a QA side that leaves a payload control
    out of its record cannot make it optional: write-denial needs a payload, exec needs a payload with an executable."""
    if manifest is None:
        return CONTROLS
    wanted = ['neg_write_payload'] + (['pos_exec_payload'] if exec_probe_argv(manifest, '/') else [])
    return CONTROLS + tuple(c for c in PAYLOAD_CONTROLS if c[0] in wanted)


SELFTEST_FSIZE_FLOOR = 16 << 20


def fsize_limit(cap):
    """Largest file any process of a probe or self-test may write (RLIMIT_FSIZE, inherited by everything it starts): its output cap plus
    slack, never below a floor for the sandbox's own bookkeeping. A flood is cut off by the kernel, not by a timeout."""
    return max(cap + 4096, SELFTEST_FSIZE_FLOOR)


def run_group(argv, env, timeout, cwd=None, stdin=None, fsize=None):
    """Run argv in a session of its own and always kill the whole process group afterwards (a payload that forks and sleeps leaves
    nothing behind). Returns (exit code or None on timeout, up to 64 KiB of stdout). The environment is exactly `env`."""
    with tempfile.TemporaryFile() as out:
        limit = fsize_limit(OUTPUT_CAP) if fsize is None else fsize
        proc = subprocess.Popen(argv, stdin=stdin if stdin is not None else subprocess.DEVNULL, stdout=out, stderr=subprocess.DEVNULL,
                                cwd=cwd, env=env, start_new_session=True,
                                preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit)))
        try:
            try:
                code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                code = None
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait()
        out.seek(0)
        return code, out.read(64 << 10)


def confinement_probe(codex_bin, run_dir, env, manifest=None):
    """Prove, with the exact private home and profile the QA run will use, that a tool command cannot read a planted
    synthetic canary under HOME or list the real Codex home, cannot write outside the roots, and still can read the clone
    and write the out directory. Uses `codex sandbox -P` (no model). Any failed or unrunnable control means the run does not
    start. Returns (record, problems); the canary is removed afterwards."""
    token = os.urandom(8).hex()
    canary_dir = CANARY_ROOT / token
    canary_dir.mkdir(parents=True, mode=0o700)
    canary = canary_dir / 'canary.txt'
    canary.write_text('synthetic-qa-canary-%s' % token)
    auth = real_codex_home() / 'auth.json'
    values = {'auth': str(auth), 'clone': str(run_dir / 'source'), 'out': str(run_dir / 'out'), 'canary': str(canary), 'canary_dir': str(canary_dir),
              'codex_home': str(real_codex_home())}
    if manifest is not None:
        values.update(payload=str(run_dir / 'payload'), exec_argv=exec_probe_argv(manifest, run_dir / 'payload'))
    results, problems = [], []
    try:
        # A scratch clone whose own .codex/config.toml tries to add the canary to the read allowlist: the profile must hold.
        hostile = run_dir / 'probe-hostile'
        shutil.rmtree(hostile, ignore_errors=True)       # a wrapper killed mid-probe may have left one behind
        (hostile / '.codex').mkdir(parents=True)
        (hostile / '.codex/config.toml').write_text('[permissions.%s.filesystem]\n%s = "read"\n' % (PROFILE, json.dumps(str(canary_dir))))
        for name, snippet, expect in controls_for(manifest):
            if name == 'neg_open_auth_file' and not auth.exists():
                continue          # no login file to protect; the open() is the denied operation, no content is read
            if name in PAYLOAD_EXEC_CONTROLS and problems:
                # Producer code runs only on a profile that has just been proved. Anything failing or not running before this point
                # means it never starts, and the record says so instead of leaving the control out.
                results.append({'control': name, 'expected': expect, 'observed': 'NOT RUN: an earlier control failed', 'ok': False,
                                'unrunnable': True, 'not_run': True})
                problems.append('%s not run: an earlier control failed' % name)
                continue
            code = '# CONTROL=%s\n%s' % (name, snippet.format(**values))
            cwd = hostile if name == 'neg_project_config_ignored' else run_dir / 'source'
            argv = [codex_bin, 'sandbox', '-P', PROFILE, '-C', str(cwd), '/usr/bin/python3', '-c', code]
            try:
                if name in PAYLOAD_EXEC_CONTROLS:
                    # allowlist environment, own session, whole group killed afterwards: the payload cannot leave a process behind
                    returncode, stdout = run_group(argv, selftest_env(run_dir, env.get('CODEX_HOME', '')), 60)
                    done = types.SimpleNamespace(returncode=returncode, stdout=stdout.decode('utf-8', 'replace'))
                else:
                    done = subprocess.run(argv, capture_output=True, text=True, timeout=60, env=env)
                got = done.stdout.strip().splitlines()[-1] if done.stdout.strip() else ''
                # A denial only counts as one when it is EPERM or EACCES: ENOENT (a wrong path), a missing errno or any
                # other text proves nothing about confinement.
                ok = done.returncode == 0 and (got == 'ALLOWED' if expect == 'ALLOWED' else got in ('DENIED:1', 'DENIED:13'))
                detail = got or 'exit %s' % done.returncode
                unrunnable = done.returncode != 0 or not got
            except (OSError, subprocess.SubprocessError) as error:
                ok, detail, unrunnable = False, type(error).__name__, True
            results.append({'control': name, 'expected': expect, 'observed': clean(detail, 40), 'ok': ok, 'unrunnable': unrunnable})
            if not ok:
                problems.append('%s expected %s, observed %s' % (name, expect, clean(detail, 40)))
    finally:
        shutil.rmtree(run_dir / 'probe-hostile', ignore_errors=True)
        for leftover in (canary_dir / 'written.txt', canary, canary_dir):
            try:
                leftover.unlink() if leftover.is_file() else leftover.rmdir()
            except OSError:
                pass
    return {'profile': PROFILE, 'established': not problems, 'controls': results, 'canary_dir': str(canary_dir),
            'could_not_check': bool(problems) and all(c['unrunnable'] for c in results if not c['ok'])}, problems


# ---------------------------------------------------------------------------------------------------------------------
# Payload (SPE-145 part 9). A producer-built directory of tools and vectors, described by payload/MANIFEST.json, is copied
# into the QA outbox, mounted into the permission profile as one more read root (read and exec, never write) and exercised
# by model-free self-tests under the real profile before Codex may start. The manifest rules live here, once, because the
# producer, the QA side and the producer's manifest helper must agree byte for byte. See docs/QA-PAYLOAD.md.
# ---------------------------------------------------------------------------------------------------------------------
PAYLOAD_SCHEMA = 'cae-qa-payload/1'
MANIFEST_NAME = 'MANIFEST.json'
MANIFEST_MAX = 64 << 10
PAYLOAD_MAX_FILES = 2000
PAYLOAD_MAX_BYTES = 256 << 20
PAYLOAD_MAX_DEPTH = 8
PAYLOAD_PATH_MAX = 200
PRODUCER_RUN_MAX = 200
SELFTEST_MAX = 32
SELFTEST_TOTAL_SECONDS = 600
SELFTEST_ARG_MAX = 256
SELFTEST_ARGC_MAX = 64
OUTPUT_CAP = 1 << 20
SCOPE_MAX = 2048
PAYLOAD_TOKEN = '@payload'
TEST_ID_RE = re.compile(r'[a-z0-9_-]{1,40}')
SELFTEST_PATH = '/usr/bin:/bin:/usr/sbin:/sbin'
# Modes of the copies: the QA outbox copy is what the sandbox executes (exec 0555, data 0444, directories 0555); the producer's
# inbox copy is owner-only (the QA user reads it through the inbox ACL, not through mode bits).
OUTBOX_MODES = {'exec': 0o555, 'data': 0o444, 'dir': 0o555}
INBOX_MODES = {'exec': 0o500, 'data': 0o400, 'dir': 0o700}
PROBE_SCOPE = ('The wrapper\'s probe and self-tests cover only offline, library-level checks under the confinement profile. '
               'Daemon-level and end-to-end results are producer-run and are not independently reproduced by this QA.')


class PayloadError(Exception):
    """A payload or manifest rule was broken. The message is for the owner and carries no file contents."""


def _check(condition, message):
    if not condition:
        raise PayloadError(message)


def _exact(value, keys, what):
    _check(isinstance(value, dict), '%s is not a JSON object' % what)
    unknown, missing = sorted(set(value) - set(keys)), sorted(set(keys) - set(value))
    _check(not unknown, '%s has unknown keys: %s' % (what, ', '.join(map(str, unknown[:3]))))
    _check(not missing, '%s is missing keys: %s' % (what, ', '.join(missing[:3])))


def _no_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError('duplicate key')
    return dict(pairs)


def unsafe_text(text):
    """True for control characters and for Unicode format, bidi, surrogate, private-use or unassigned code points: text that can hide
    or reorder what a reader sees. Newline and tab are the only control characters allowed (and only where the caller allows them)."""
    return any(unicodedata.category(c) in ('Cc', 'Cf', 'Cs', 'Co', 'Cn', 'Zl', 'Zp') and c not in '\n\t' for c in text)


def _int(value):
    return type(value) is int


def valid_payload_path(path):
    """Relative, normalized, no empty, '.' or '..' component, no control characters or backslash, at most 200 characters and
    8 components, and never the manifest's own name."""
    if not (isinstance(path, str) and 0 < len(path) <= PAYLOAD_PATH_MAX) or path == MANIFEST_NAME:
        return False
    if path.startswith('/') or '\\' in path or unsafe_text(path) or '\n' in path or '\t' in path:
        return False
    parts = path.split('/')
    return len(parts) <= PAYLOAD_MAX_DEPTH and all(part not in ('', '.', '..') for part in parts)


def manifest_sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def parse_manifest(raw):
    """Validate the raw bytes of MANIFEST.json against the cae-qa-payload/1 rules (exact key sets, caps, listed files only) and
    return the parsed document. Raises PayloadError naming the first rule broken."""
    _check(isinstance(raw, bytes) and 0 < len(raw) <= MANIFEST_MAX, 'MANIFEST.json is empty or larger than 64 KiB')
    try:
        doc = json.loads(raw.decode('utf-8'), object_pairs_hook=_no_duplicate_keys)
    except (ValueError, RecursionError):
        raise PayloadError('MANIFEST.json is not UTF-8 JSON without duplicate keys')
    _exact(doc, ('schema', 'files', 'selftests', 'scope', 'producer_run'), 'MANIFEST.json')
    _check(doc['schema'] == PAYLOAD_SCHEMA, 'MANIFEST.json schema is not %s' % PAYLOAD_SCHEMA)
    files, tests, scope, producer = doc['files'], doc['selftests'], doc['scope'], doc['producer_run']
    _check(isinstance(files, list) and len(files) <= PAYLOAD_MAX_FILES, 'files is not a list of at most %d entries' % PAYLOAD_MAX_FILES)
    paths, kinds, total = [], {}, 0
    for entry in files:
        _exact(entry, ('path', 'sha256', 'size', 'kind'), 'a files entry')
        _check(valid_payload_path(entry['path']), 'file path %r is not a normalized relative payload path' % str(entry['path'])[:60])
        _check(isinstance(entry['sha256'], str) and re.fullmatch(r'[0-9a-f]{64}', entry['sha256']) is not None,
               'sha256 of %s is not 64 lower-case hex characters' % entry['path'])
        _check(_int(entry['size']) and entry['size'] >= 0, 'size of %s is not a non-negative integer' % entry['path'])
        _check(entry['kind'] in ('exec', 'data'), 'kind of %s is not exec or data' % entry['path'])
        paths.append(entry['path'])
        kinds[entry['path']] = entry['kind']
        total += entry['size']
    _check(paths == sorted(paths) and len(set(paths)) == len(paths), 'files must be sorted by path and unique')
    _check(len({p.casefold() for p in paths}) == len(paths), 'two paths differ only by letter case (a case-insensitive volume would merge them)')
    _check(total <= PAYLOAD_MAX_BYTES, 'the payload is larger than %d bytes' % PAYLOAD_MAX_BYTES)
    directories = set()
    for path in paths:
        parts = path.split('/')
        directories.update('/'.join(parts[:i]) for i in range(1, len(parts)))
    _check(not (directories & set(paths)), 'a path is both a file and a directory')
    _check(isinstance(tests, list) and len(tests) <= SELFTEST_MAX, 'selftests is not a list of at most %d entries' % SELFTEST_MAX)
    seen_ids, wall = set(), 0
    for test in tests:
        _exact(test, ('id', 'argv', 'stdin', 'expect_exit', 'expect_stdout_sha256', 'timeout_s', 'max_output_bytes'), 'a selftests entry')
        ident = test['id']
        _check(isinstance(ident, str) and TEST_ID_RE.fullmatch(ident) is not None and ident not in seen_ids,
               'selftest id %r is invalid or repeated' % str(ident)[:40])
        seen_ids.add(ident)
        argv = test['argv']
        _check(isinstance(argv, list) and 1 <= len(argv) <= SELFTEST_ARGC_MAX and all(isinstance(a, str) for a in argv),
               'argv of %s is not a list of strings' % ident)
        _check(kinds.get(argv[0]) == 'exec', 'argv[0] of %s is not a listed kind=exec file' % ident)
        for arg in argv[1:]:
            _check(len(arg) <= SELFTEST_ARG_MAX and '\x00' not in arg, 'an argument of %s is too long or contains NUL' % ident)
            if arg == PAYLOAD_TOKEN or arg.startswith(PAYLOAD_TOKEN + '/'):
                rest = arg[len(PAYLOAD_TOKEN):]
                _check(all(part not in ('..', '.') for part in rest.split('/')[1:]), 'an @payload argument of %s contains . or ..' % ident)
        stdin = test['stdin']
        _check(stdin is None or kinds.get(stdin) == 'data', 'stdin of %s is not null or a listed kind=data file' % ident)
        _check(_int(test['expect_exit']) and 0 <= test['expect_exit'] <= 255, 'expect_exit of %s is not an integer 0..255' % ident)
        _check(isinstance(test['expect_stdout_sha256'], str) and re.fullmatch(r'[0-9a-f]{64}', test['expect_stdout_sha256']) is not None,
               'expect_stdout_sha256 of %s is not 64 lower-case hex characters' % ident)
        _check(_int(test['timeout_s']) and 1 <= test['timeout_s'] <= 120, 'timeout_s of %s is not 1..120' % ident)
        _check(_int(test['max_output_bytes']) and 1 <= test['max_output_bytes'] <= OUTPUT_CAP, 'max_output_bytes of %s is not 1..1048576' % ident)
        wall += test['timeout_s']
    _check(wall <= SELFTEST_TOTAL_SECONDS, 'the self-tests may take %d s in total, more than %d' % (wall, SELFTEST_TOTAL_SECONDS))
    _check(isinstance(scope, str) and 0 < len(scope) <= SCOPE_MAX and not unsafe_text(scope),
           'scope is not 1..2048 characters of text')
    _check(isinstance(producer, list) and len(producer) <= PRODUCER_RUN_MAX and len(set(map(str, producer))) == len(producer),
           'producer_run is not a list of unique paths')
    for path in producer:
        _check(isinstance(path, str) and kinds.get(path) == 'data', 'producer_run entry %r is not a listed kind=data file' % str(path)[:60])
    return doc


def expand_selftest_argv(test, payload_dir):
    """The absolute argv of one self-test: argv[0] is a payload-relative path; any other element that equals '@payload' or
    starts with '@payload/' gets the payload directory substituted; every other element is a literal ('@payloadX' stays)."""
    out = [os.path.join(str(payload_dir), test['argv'][0])]
    for arg in test['argv'][1:]:
        out.append(str(payload_dir) + arg[len(PAYLOAD_TOKEN):] if arg == PAYLOAD_TOKEN or arg.startswith(PAYLOAD_TOKEN + '/') else arg)
    return out


def payload_stage_seconds(manifest):
    """Wall-clock budget the producer must give the stage hop for this payload: copy and verify, plus every self-test."""
    return 120 + sum(t['timeout_s'] + 10 for t in manifest['selftests'])


def _open_dir(parent_fd, name):
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)


def read_manifest_at(dir_fd, owner):
    """MANIFEST.json of an opened payload directory: a single-link regular file owned by `owner` (None: any), at most 64 KiB."""
    try:
        fd = os.open(MANIFEST_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError:
        raise PayloadError('MANIFEST.json is missing or is a link')
    with os.fdopen(fd, 'rb') as handle:
        st = os.fstat(handle.fileno())
        _check(stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and (owner is None or st.st_uid == owner), 'MANIFEST.json is not a plain file of the expected owner')
        raw = handle.read(MANIFEST_MAX + 1)
    _check(len(raw) <= MANIFEST_MAX, 'MANIFEST.json is larger than 64 KiB')
    return raw


def verify_tree(dir_fd, manifest, raw, owner, check_modes=False, modes=None):
    """Problems (empty list: the tree is exactly the manifest). The directory held open as dir_fd must contain exactly the listed
    files plus MANIFEST.json (whose bytes are `raw`), each a single-link regular file with the listed size and SHA-256, owned
    by `owner` when given, and no other entry of any kind (no link, no empty or extra directory). With check_modes the exact
    modes of OUTBOX_MODES are required. Everything is opened relative to descriptors with O_NOFOLLOW."""
    modes = modes or OUTBOX_MODES
    expected = {f['path']: f for f in manifest['files']}
    expected[MANIFEST_NAME] = {'sha256': manifest_sha256(raw), 'size': len(raw), 'kind': 'data'}
    wanted_dirs = set()
    for path in expected:
        parts = path.split('/')
        wanted_dirs.update('/'.join(parts[:i]) for i in range(1, len(parts)))
    problems, seen, count = [], set(), [0]
    limit = len(expected) + len(wanted_dirs) + 1

    def note(text):
        if len(problems) < 20:
            problems.append(text)

    def walk(fd, prefix):
        try:
            with os.scandir(fd) as scan:
                names = sorted(entry.name for entry in scan)
        except OSError:
            note('%s cannot be listed' % (prefix or 'the payload directory'))
            return
        for name in names:
            count[0] += 1
            if count[0] > limit:
                note('the payload directory has more entries than the manifest lists')
                return
            rel = prefix + name
            try:
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError:
                note('%s cannot be examined' % rel)
                continue
            if stat.S_ISDIR(st.st_mode):
                if rel not in wanted_dirs:
                    note('unexpected directory %s' % rel)
                    continue
                if check_modes and stat.S_IMODE(st.st_mode) != modes['dir']:
                    note('directory %s has mode %o' % (rel, stat.S_IMODE(st.st_mode)))
                if owner is not None and st.st_uid != owner:
                    note('directory %s has the wrong owner' % rel)
                try:
                    child = _open_dir(fd, name)
                except OSError:
                    note('directory %s cannot be opened' % rel)
                    continue
                try:
                    walk(child, rel + '/')
                finally:
                    os.close(child)
            elif stat.S_ISREG(st.st_mode):
                entry = expected.get(rel)
                if entry is None:
                    note('unexpected file %s' % rel)
                    continue
                try:
                    handle = os.fdopen(os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd), 'rb')
                except OSError:
                    note('%s cannot be opened' % rel)
                    continue
                with handle:
                    held = os.fstat(handle.fileno())
                    if not stat.S_ISREG(held.st_mode) or (held.st_dev, held.st_ino) != (st.st_dev, st.st_ino):
                        note('%s changed while it was being checked' % rel)
                        continue
                    if held.st_nlink != 1:
                        note('%s has more than one link' % rel)
                    if owner is not None and held.st_uid != owner:
                        note('%s has the wrong owner' % rel)
                    if check_modes and stat.S_IMODE(held.st_mode) != modes[entry['kind']]:
                        note('%s has mode %o, expected %o' % (rel, stat.S_IMODE(held.st_mode), modes[entry['kind']]))
                    if held.st_size != entry['size']:
                        note('%s has size %d, listed %d' % (rel, held.st_size, entry['size']))
                    else:
                        digest = hashlib.sha256()
                        for chunk in iter(lambda: handle.read(1 << 20), b''):
                            digest.update(chunk)
                        if digest.hexdigest() != entry['sha256']:
                            note('%s does not match its listed SHA-256' % rel)
                seen.add(rel)
            else:
                note('%s is a link or not a regular file' % rel)

    top = os.fstat(dir_fd)
    if owner is not None and top.st_uid != owner:
        note('the payload directory has the wrong owner')
    if check_modes and stat.S_IMODE(top.st_mode) != modes['dir']:
        note('the payload directory has mode %o' % stat.S_IMODE(top.st_mode))
    walk(dir_fd, '')
    for path in sorted(set(expected) - seen):
        note('missing %s' % path)
    return problems


def copy_payload(src_fd, dst, manifest, raw, modes):
    """Copy the verified payload held open as src_fd to the new directory dst, file by file as listed (never by scanning), each
    opened relative to its parent descriptor with O_NOFOLLOW, hashed while it is copied and created exclusively with the mode
    its kind asks for. Directories get their final mode last. Raises PayloadError on any mismatch."""
    os.mkdir(dst, 0o700)
    created = ['']
    entries = [(f['path'], f['sha256'], f['size'], modes[f['kind']]) for f in manifest['files']]
    for path, sha, size, mode in entries:
        parts = path.split('/')
        fd = os.dup(src_fd)
        try:
            for i, part in enumerate(parts[:-1]):
                rel = '/'.join(parts[:i + 1])
                if rel not in created:
                    os.mkdir(os.path.join(dst, rel), 0o700)
                    created.append(rel)
                nxt = _open_dir(fd, part)
                os.close(fd)
                fd = nxt
            with os.fdopen(os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd), 'rb') as source:
                held = os.fstat(source.fileno())
                _check(stat.S_ISREG(held.st_mode) and held.st_nlink == 1 and held.st_size == size, '%s is not the listed regular file' % path)
                digest = hashlib.sha256()
                out = os.open(os.path.join(dst, path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(out, 'wb') as sink:
                    for chunk in iter(lambda: source.read(1 << 20), b''):
                        digest.update(chunk)
                        sink.write(chunk)
                _check(digest.hexdigest() == sha, '%s does not match its listed SHA-256 while being copied' % path)
                os.chmod(os.path.join(dst, path), mode)
        finally:
            os.close(fd)
    target = os.path.join(dst, MANIFEST_NAME)
    out = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(out, 'wb') as sink:
        sink.write(raw)
    os.chmod(target, modes['data'])
    for rel in sorted(created, key=lambda r: -r.count('/')):
        os.chmod(os.path.join(dst, rel) if rel else dst, modes['dir'])


def open_payload_path(path):
    """Attended mode: (fd, producer uid) of inbox/payload, or None if there is none. The producer is whoever owns the inbox."""
    parent = os.path.dirname(str(path))
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    return fd, os.stat(parent).st_uid


def open_payload_at(attempt_fd, producer):
    """Installed mode: (fd, producer) of payload relative to the validated attempt descriptor, or None if absent."""
    try:
        return _open_dir(attempt_fd, 'payload'), producer
    except FileNotFoundError:
        return None


def selftest_env(outbox, codex_home):
    """Allowlist for self-tests: a fixed PATH, the run's own home and tmp, and CODEX_HOME because `codex sandbox -P` cannot
    find the profile without it. Nothing is inherited."""
    outbox = str(outbox)
    return {'PATH': SELFTEST_PATH, 'HOME': outbox + '/home', 'TMPDIR': outbox + '/tmp', 'CODEX_HOME': str(codex_home)}


def _digest_of(handle, cap):
    handle.seek(0, 2)
    size = handle.tell()
    handle.seek(0)
    digest = hashlib.sha256()
    remaining = min(size, cap)
    while remaining:
        chunk = handle.read(min(remaining, 1 << 20))
        if not chunk:
            break
        digest.update(chunk)
        remaining -= len(chunk)
    return size, digest.hexdigest()


def run_selftests(codex_bin, outbox, manifest, env):
    """Run each listed self-test as `codex sandbox -P qa_confined -C <clone> <absolute argv>` with the allowlist environment,
    cwd = the run's tmp, stdin from the listed file or /dev/null, stdout and stderr to temporary files (raw bytes, capped).
    Returns one observation per test: exit code, timeout, sizes and SHA-256 of stdout and of the truncated stderr. Nothing is
    judged here: the producer re-derives pass or fail from the manifest it holds."""
    outbox = Path(outbox)
    payload_dir = outbox / 'payload'
    records = []
    for test in manifest['selftests']:
        record = {'id': test['id'], 'exit': None, 'timed_out': False, 'unrunnable': False, 'stdout_bytes': None,
                  'stdout_sha256': None, 'stderr_bytes': None, 'stderr_sha256': None, 'elapsed': None}
        records.append(record)
        argv = [codex_bin, 'sandbox', '-P', PROFILE, '-C', str(outbox / 'source')] + expand_selftest_argv(test, payload_dir)
        stdin = None
        started = time.monotonic()
        try:
            with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                stdin = open_regular(payload_dir / test['stdin']) if test['stdin'] is not None else open(os.devnull, 'rb')
                try:
                    limit = fsize_limit(test['max_output_bytes'])
                    proc = subprocess.Popen(argv, stdin=stdin, stdout=out, stderr=err, cwd=str(outbox / 'tmp'), env=env, start_new_session=True,
                                            preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit)))
                except OSError:
                    record['unrunnable'] = True
                    continue
                try:
                    record['exit'] = proc.wait(timeout=test['timeout_s'])
                except subprocess.TimeoutExpired:
                    kill_group(proc)
                    record['timed_out'] = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)       # stragglers in the test's own group
                except OSError:
                    pass
                cap = test['max_output_bytes']
                record['stdout_bytes'], record['stdout_sha256'] = _digest_of(out, cap)
                record['stderr_bytes'], record['stderr_sha256'] = _digest_of(err, cap)
                record['stdout_capped'] = record['stdout_bytes'] > cap
        except OSError:
            record['unrunnable'] = True
        finally:
            if stdin is not None:
                stdin.close()
            record['elapsed'] = round(time.monotonic() - started, 3)
    return records


def derive_selftest_verdicts(manifest, records):
    """(verdicts, first_problem). Pass or fail is recomputed from the manifest the producer holds and the raw observations,
    never from anything the QA side calls ok: the right exit status, the stdout SHA-256 equal to the listed one, no timeout, no
    output over the cap, and exactly one observation per listed test."""
    by_id, verdicts, first = {}, [], None
    for record in records if isinstance(records, list) else []:
        if isinstance(record, dict) and isinstance(record.get('id'), str):
            by_id.setdefault(record['id'], []).append(record)
    for test in manifest['selftests']:
        seen = by_id.get(test['id'], [])
        reasons = []
        record = seen[0] if len(seen) == 1 else {}
        if len(seen) != 1:
            reasons.append('not run' if not seen else 'reported more than once')
        elif record.get('unrunnable') is True:
            reasons.append('could not be run')
        elif record.get('timed_out') is True:
            reasons.append('timed out after %d s' % test['timeout_s'])
        elif record.get('stdout_capped') is True or not _int(record.get('stdout_bytes')) or record['stdout_bytes'] > test['max_output_bytes']:
            reasons.append('produced more than %d bytes of output' % test['max_output_bytes'])
        else:
            if record.get('exit') != test['expect_exit'] or not _int(record.get('exit')):
                reasons.append('exited %s, expected %d' % (clean(record.get('exit'), 12), test['expect_exit']))
            if record.get('stdout_sha256') != test['expect_stdout_sha256']:
                reasons.append('stdout SHA-256 %s differs from the listed one' % clean(str(record.get('stdout_sha256'))[:12], 14))
        verdict = {'id': test['id'], 'argv': test['argv'], 'expect_exit': test['expect_exit'], 'ok': not reasons, 'reasons': reasons}
        for key in ('exit', 'timed_out', 'stdout_bytes', 'stdout_sha256', 'stderr_bytes', 'stderr_sha256', 'elapsed'):
            verdict[key] = record.get(key)
        verdicts.append(verdict)
        if reasons and first is None:
            first = 'self-test %s %s' % (test['id'], reasons[0])
    return verdicts, first


def exec_probe_argv(manifest, payload_dir):
    """What the pos_exec_payload control runs inside the sandbox: the first self-test's own expanded argv (the producer's definition
    of a safe invocation), else the first kind=exec file with no arguments. None when the payload has no executable."""
    if manifest['selftests']:
        return expand_selftest_argv(manifest['selftests'][0], payload_dir)
    for entry in manifest['files']:
        if entry['kind'] == 'exec':
            return [os.path.join(str(payload_dir), entry['path'])]
    return None


def codex_argv(codex_bin, run_dir):
    """The permission profile in the private CODEX_HOME applies; there is deliberately no -s, --add-dir or sandbox -c
    override (the legacy write-only sandbox). No -m: model routing is not touched."""
    # -o is written by the codex client itself, which is NOT sandboxed (only its tool commands are). It therefore points
    # into the run directory, which the QA process cannot write, so a link planted by QA can never redirect that write.
    return [codex_bin, 'exec', '--skip-git-repo-check', '--json', '--ephemeral', '-c', 'default_permissions="%s"' % PROFILE,
            '-C', str(run_dir / 'source'),
            '--output-schema', str(run_dir / 'verdict.schema.json'), '-o', str(run_dir / 'verdict.json'), '-']


def clean(text, limit=120):
    """QA-controlled text is data for a human or agent to read, never instructions: strip control characters, cap it."""
    return ''.join(c if c.isprintable() else ' ' for c in str(text))[:limit]


def kill_group(proc):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)   # our own child's process group only
        except OSError:
            return
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


PROFILE_CHANGED = 125                          # qa-exec: the profile is not the one hop 1 proved; codex was not started


def qa_stage(args, build_env=None, copy_source=None, open_payload=None):
    """QA side, hop 1 (runs as the QA user). Prints exactly one JSON record on stdout, before any model starts.
    build_env is the installed mode's allowlist environment; the attended mode passes none and uses isolated_env."""
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 2)           # the hop joins stderr to the pipe; only the record may reach the producer
    os.close(devnull)
    os.umask(0o077)
    try:
        record = stage_outbox(args, build_env, copy_source, open_payload)
    except Exception as error:
        record = {'error': type(error).__name__}
    sys.stdout.write(json.dumps(record) + '\n')
    sys.stdout.flush()
    return 0


def stage_outbox(args, build_env=None, copy_source=None, open_payload=None):
    inbox, outbox = Path(args.inbox), Path(args.outbox)
    outbox.mkdir(mode=0o700)      # fails if anything already exists under this attempt name
    for sub in ('out', 'tmp', 'home', 'cargo-target'):
        (outbox / sub).mkdir(mode=0o700)
    if copy_source:
        copy_source(outbox / 'source')       # installed mode: copied from a validated descriptor, never from a path
    else:
        shutil.copytree(inbox / 'source', outbox / 'source', symlinks=True)
    write_private(outbox / 'verdict.schema.json', VERDICT_SCHEMA)
    if not shutil.which(args.codex_bin):
        return {'codex_missing': True}
    if not (real_codex_home() / 'auth.json').exists():
        return {'login_missing': True}     # otherwise the probe would skip the login control, which the producer requires
    manifest, payload_record = None, None
    opened = (open_payload or (lambda: open_payload_path(inbox / 'payload')))()
    if opened is not None:
        manifest, payload_record = stage_payload(opened, outbox)
        if manifest is None:
            return {'payload': payload_record}                     # nothing is proved on top of a payload that failed its manifest
    home = prepare_codex_home(outbox, [outbox / 'payload'] if manifest is not None else (), home=Path(args.codex_home))
    env = build_env() if build_env else isolated_env(outbox, home)
    record, problems = confinement_probe(args.codex_bin, outbox, env, manifest)
    result = {'confinement': record, 'profile_sha256': sha256_file(home / 'config.toml')}
    if manifest is not None:
        result['payload'] = payload_record
        # The gate runs only on a proved profile; the producer refuses to start Codex without both.
        if not problems:
            result['selftests'] = run_selftests(args.codex_bin, outbox, manifest, selftest_env(outbox, home))
    return result


def stage_payload(opened, outbox):
    """Verify the producer's payload (held open as `opened`), copy it into outbox/payload and verify the copy. Returns
    (manifest, record) or (None, record with the problems)."""
    fd, producer = opened
    record = {'manifest_sha256': None, 'staged': False, 'problems': []}
    try:
        raw = read_manifest_at(fd, producer)
        record['manifest_sha256'] = manifest_sha256(raw)
        manifest = parse_manifest(raw)
        record['problems'] = verify_tree(fd, manifest, raw, producer)
        if record['problems']:
            return None, record
        copy_payload(fd, str(outbox / 'payload'), manifest, raw, OUTBOX_MODES)
        dst = os.open(str(outbox / 'payload'), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            record['problems'] = verify_tree(dst, manifest, raw, os.getuid(), check_modes=True)
        finally:
            os.close(dst)
        if record['problems']:
            return None, record
        record['staged'] = True
        return manifest, record
    except PayloadError as error:
        record['problems'] = record['problems'] or [str(error)]
        return None, record
    finally:
        os.close(fd)


class QAStop(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def qa_exec(args, build_env=None, open_prompt=None):
    """QA side, hop 2 (runs as the QA user). Only codex writes fd 1 (the producer's pipe); stderr goes to the outbox; the
    prompt comes from the producer-owned inbox file. Exit: codex's code, 124 on timeout or alarm, 130 on a signal."""
    inbox, outbox = Path(args.inbox), Path(args.outbox)
    err = os.open(outbox / 'stderr.log', os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.dup2(err, 2)
    os.close(err)
    os.umask(0o077)
    proc = None

    def stop(signum, frame):
        raise QAStop(124 if signum == signal.SIGALRM else 130)
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT, signal.SIGALRM):
        signal.signal(sig, stop)
    try:
        try:
            profile = sha256_file(Path(args.codex_home) / 'config.toml')
        except OSError:
            profile = None
        if profile != args.profile_sha256:
            sys.stderr.write('qa-exec: the permission profile changed since it was proved; codex not started\n')
            return PROFILE_CHANGED
        with (open_prompt() if open_prompt else open_regular(inbox / 'prompt.txt')) as prompt:
            try:
                proc = subprocess.Popen(codex_argv(args.codex_bin, outbox), cwd=str(outbox / 'source'),
                                        env=build_env() if build_env else isolated_env(outbox, Path(args.codex_home)), stdin=prompt, start_new_session=True)
            except OSError:
                return 127
        try:
            code = proc.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            kill_group(proc)
            return 124
        try:
            os.killpg(proc.pid, signal.SIGKILL)   # stragglers in codex's own group
        except OSError:
            pass
        return code if code >= 0 else 128 - code
    except QAStop as stopped:
        if proc is not None:
            kill_group(proc)
        return stopped.code


# Copied from qa-identity-setup.py (the attended isolation probe); a test pins the two together. This file imports
# nothing from the repository, so the installed probe verb carries its own text.
PROCENV_SRC = r"""
import ctypes, ctypes.util
def procenv(p):
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    libc.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    libc.sysctl.restype = ctypes.c_int
    mib = (ctypes.c_int * 3)(1, 49, int(p))
    size = ctypes.c_size_t(0)
    if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or size.value == 0:
        return None
    buf = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
        return None
    return buf.raw[: size.value]
"""

PROBE = PROCENV_SRC + r"""
import errno, json, os, stat, subprocess, sys
inbox, outbox, xroot, prodhome, pid, sentinel = sys.argv[1:7]
def tryit(f):
    try: f(); return "ok"
    except OSError as e: return errno.errorcode.get(e.errno, str(e.errno))
def wr(d):
    p = os.path.join(d, ".probe-" + str(os.getpid()))
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600); os.close(fd); os.unlink(p)
def codex_dir():
    # /Users/NAME is 0700, so only the QA user can see its own ~/.codex; Codex leaves it 0755 after login
    try: st = os.lstat(os.path.join(os.environ.get("HOME", "/nonexistent"), ".codex"))
    except FileNotFoundError: return "absent"
    except OSError as e: return "ERR:" + errno.errorcode.get(e.errno, str(e.errno))
    return {"dir": stat.S_ISDIR(st.st_mode), "uid": st.st_uid, "mode": st.st_mode & 0o777}
mine = "mine-" + os.urandom(6).hex()
child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(30)"], env={"PATH": "/usr/bin:/bin", "QA_OWN_MARK": mine})
own = procenv(child.pid)
own_ok = "ok" if own is not None and mine.encode() in own else "unreadable"
child.kill(); child.wait()
foreign = procenv(pid)
out = {"uid": os.getuid(), "gid": os.getgid(), "groups": sorted(os.getgroups()),
  "ctty": tryit(lambda: os.close(os.open("/dev/tty", os.O_RDWR))),
  "std_isatty": [os.isatty(0), os.isatty(1), os.isatty(2)],
  "list_inbox": tryit(lambda: os.listdir(inbox)), "write_inbox": tryit(lambda: wr(inbox)),
  "write_outbox": tryit(lambda: wr(outbox)), "write_exchange": tryit(lambda: wr(xroot)),
  "list_producer_home": tryit(lambda: os.listdir(prodhome)),
  "stat_producer_auth": tryit(lambda: os.stat(os.path.join(prodhome, ".codex", "auth.json"))),
  "open_producer_ssh": tryit(lambda: os.listdir(os.path.join(prodhome, ".ssh"))),
  "codex_dir": codex_dir(), "own_env_read": own_ok, "foreign_env_leaked": (foreign is not None and sentinel.encode() in foreign)}
print(json.dumps(out))
"""

# ---------------------------------------------------------------------------------------------------------------------
# Installed mode (ADR 0016, PR 2). Selected only by the first argument being one of the fixed verbs below; the attended
# subcommands (qa-stage, qa-exec) above are untouched. Installed at /Library/cae-qa/qa-side and run as the QA user through
# an exact-argv privilege-delegation rule, so every input is distrusted: the verb and attempt id come from argv, everything else from a
# root-owned config or from a request file that is validated before a single path is derived. Any failure prints nothing
# and exits non-zero; the producer maps that to QA_INCOMPLETE. This mode is not installed anywhere yet.
# ---------------------------------------------------------------------------------------------------------------------
PROTOCOL = 1
ROOT_UID = 0                          # tests substitute the current uid to stand in for root; never read from argv or env
CONFIG_PATH = '/Library/cae-qa/qa-side.json'
CHILD_PATH = '/usr/bin:/bin:/usr/sbin:/sbin'
PYTHON = '/usr/bin/python3'
ATTEMPT_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,150}')
SENTINEL_RE = re.compile(r'qa-sentinel-[0-9a-f]{32}')
SHA256_RE = re.compile(r'[0-9a-f]{64}')
NAME_RE = re.compile(r'[a-z][a-z0-9_]{1,31}')
# The stray-call guard compares the uid the privilege tool records for its caller. Built from parts because the attended
# module test forbids that tool's name anywhere in this file.
PRODUCER_UID_ENV = 'SU' + 'DO_UID'
ATTEMPT_SLOT = '<attempt>'
# The ONE table of producer-side argv shapes (after the helper path) that the delegation drop-in of a later PR will allow.
ALLOWED_ARGV = {
    'version': ('version',),
    'probe': ('probe', ATTEMPT_SLOT),
    'stage': ('stage', ATTEMPT_SLOT),
    'exec': ('exec', ATTEMPT_SLOT),
}
CONFIG_KEYS = {'qa_name': str, 'qa_uid': int, 'producer_uid': int, 'exchange': str, 'codex_path': str, 'codex_sha256': str}
REQUEST_KEYS = {
    'probe': {'verb': str, 'attempt': str, 'pid': int, 'sentinel': str},
    'stage': {'verb': str, 'attempt': str},
    'exec': {'verb': str, 'attempt': str, 'timeout': int, 'profile_sha256': str},
}
REQUEST_MAX = 4096
CONFIG_MAX = 65536
ALARM_SLACK = 300


class Refuse(Exception):
    """Any validation miss. Carries no message on purpose: installed mode prints nothing on failure."""


def _need(condition):
    if not condition:
        raise Refuse()


def _typed(value, kind):
    return type(value) is kind      # bool is an int subclass; a JSON true must not pass as a number


def parse_installed_argv(argv):
    """argv (without the program name) -> (verb, attempt or None, dry_run). Only the fixed shapes of ALLOWED_ARGV pass.
    --dry-run (last) is for tests and the owner; --codex-bin VALUE is accepted and ignored, the pinned path always wins."""
    words, dry = [], False
    items = list(argv)
    while items:
        item = items.pop(0)
        if item == '--dry-run':
            _need(not dry)
            dry = True
        elif item == '--codex-bin':
            _need(items)
            items.pop(0)
        else:
            words.append(item)
    _need(words and words[0] in ALLOWED_ARGV and len(words) == len(ALLOWED_ARGV[words[0]]))
    verb = words[0]
    attempt = words[1] if len(words) == 2 else None
    if attempt is not None:
        _need(ATTEMPT_RE.fullmatch(attempt) is not None and '..' not in attempt)
    return verb, attempt, dry


def trusted_chain(path, anchor, final_regular=True):
    """Tripwire: every component from anchor down to path is root-owned, not group or world writable and not a link;
    the last is a regular file. It cannot protect a helper against its own replacement (root-only location and the
    delegated digest pin do), it catches an installation that is visibly wrong."""
    _need(isinstance(path, str) and os.path.isabs(path) and os.path.normpath(path) == path)
    anchor = os.path.normpath(anchor)
    prefix = anchor if anchor.endswith('/') else anchor + '/'
    _need(path.startswith(prefix))
    parts = [p for p in path[len(prefix):].split('/') if p]
    current = anchor
    for index, part in enumerate([''] + parts):
        if part:
            current = os.path.join(current, part)
        st = os.lstat(current)
        _need(not stat.S_ISLNK(st.st_mode) and st.st_uid == ROOT_UID and not st.st_mode & 0o022)
        last = index == len(parts)
        _need(stat.S_ISREG(st.st_mode) if last and final_regular else stat.S_ISDIR(st.st_mode) if not last else True)


def read_config(config_path, chain_anchor):
    trusted_chain(config_path, chain_anchor)
    with open_regular(config_path) as handle:
        raw = handle.read(CONFIG_MAX + 1)
    _need(len(raw) <= CONFIG_MAX)
    config = json.loads(raw.decode('utf-8'))
    _need(isinstance(config, dict) and set(config) == set(CONFIG_KEYS))
    for key, kind in CONFIG_KEYS.items():
        _need(_typed(config[key], kind))
    _need(NAME_RE.fullmatch(config['qa_name']) is not None and SHA256_RE.fullmatch(config['codex_sha256']) is not None)
    _need(config['qa_uid'] > 0 and config['producer_uid'] > 0 and config['qa_uid'] != config['producer_uid'])
    for key in ('exchange', 'codex_path'):
        _need(os.path.isabs(config[key]) and os.path.normpath(config[key]) == config[key])
    return config


# The exchange root is root:wheel 0711: the QA user may only SEARCH it. Directories that are only traversed (exchange, inbox,
# attempt) are opened with O_SEARCH (macOS: O_EXEC|O_DIRECTORY = 0x40100000); there is no fallback to a read-mode open.
def _search_flag():
    """O_SEARCH where it exists (macOS: O_EXEC|O_DIRECTORY, 0x40100000 if Python does not expose it). Linux has no O_SEARCH;
    O_PATH is its search-only stand-in. Any other platform: None, and the open is refused, never made read-mode."""
    flag = getattr(os, 'O_SEARCH', None)
    if flag:
        return flag
    if sys.platform == 'darwin':
        return 0x40100000
    return getattr(os, 'O_PATH', None) if sys.platform.startswith('linux') else None


_O_SEARCH = _search_flag()


def search_flags():
    _need(_O_SEARCH is not None)
    return _O_SEARCH | os.O_DIRECTORY | os.O_NOFOLLOW


def open_dir_at(parent_fd, name, fds):
    fd = os.open(name, search_flags(), dir_fd=parent_fd)
    fds.append(fd)
    return fd


def read_request(config, verb, attempt, fds):
    """X/inbox/<attempt>/request.json, opened relative to parent descriptors without following a link at any step."""
    producer = config['producer_uid']
    xfd = os.open(config['exchange'], search_flags())
    fds.append(xfd)
    st = os.fstat(xfd)
    _need(st.st_uid == ROOT_UID and not st.st_mode & 0o022)
    ifd = open_dir_at(xfd, 'inbox', fds)
    st = os.fstat(ifd)
    _need(st.st_uid == producer and not st.st_mode & 0o022)
    lock = os.stat('RUN.lock', dir_fd=ifd, follow_symlinks=False)
    _need(stat.S_ISREG(lock.st_mode))
    afd = open_dir_at(ifd, attempt, fds)
    st = os.fstat(afd)
    _need(st.st_uid == producer and not st.st_mode & 0o022)
    rfd = os.open('request.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=afd)
    fds.append(rfd)
    st = os.fstat(rfd)
    _need(stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_uid == producer and not st.st_mode & 0o022
          and st.st_size <= REQUEST_MAX)
    raw = os.read(rfd, REQUEST_MAX + 1)
    _need(len(raw) <= REQUEST_MAX)
    request = json.loads(raw.decode('utf-8'))
    shape = REQUEST_KEYS[verb]
    _need(isinstance(request, dict) and set(request) == set(shape))
    for key, kind in shape.items():
        _need(_typed(request[key], kind))
    _need(request['verb'] == verb and request['attempt'] == attempt)
    if verb == 'probe':
        _need(request['pid'] > 1 and SENTINEL_RE.fullmatch(request['sentinel']) is not None)
    if verb == 'exec':
        _need(60 <= request['timeout'] <= 14400 and SHA256_RE.fullmatch(request['profile_sha256']) is not None)
    return request, afd


PROMPT_MAX = 1 << 20
# Clone copy budget (generous for a source checkout without build output; a bigger tree is refused, not truncated).
MAX_COPY_ENTRIES = 500000
MAX_COPY_BYTES = 8 << 30
MAX_COPY_DEPTH = 100
HELPER_PATH = '/Library/cae-qa/qa-side'


def open_source_dir(afd, producer):
    """The clone directory of a validated attempt, opened relative to the attempt descriptor without following a link."""
    fd = os.open('source', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=afd)
    st = os.fstat(fd)
    if st.st_uid != producer or st.st_mode & 0o022:
        os.close(fd)
        raise Refuse()
    return fd


def open_prompt_at(afd, producer):
    """prompt.txt of a validated attempt as a binary file object: no link followed, regular, producer-owned, size-capped."""
    fd = os.open('prompt.txt', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=afd)
    st = os.fstat(fd)
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != producer or st.st_mode & 0o022 or st.st_nlink != 1
            or st.st_size > PROMPT_MAX):
        os.close(fd)
        raise Refuse()
    return os.fdopen(fd, 'rb')


class CopyLimits:
    """Budget for one clone copy, so a hostile or runaway tree cannot fill the QA user's disk or hang the stage hop."""

    def __init__(self):
        self.entries = 0
        self.bytes = 0

    def entry(self):
        self.entries += 1
        _need(self.entries <= MAX_COPY_ENTRIES)

    def data(self, count):
        self.bytes += count
        _need(self.bytes <= MAX_COPY_BYTES)


def copy_tree_at(src_fd, dst, producer, limits=None, depth=0):
    """Copy the directory held open as src_fd to the new directory dst. Every entry is examined and opened relative to its
    parent descriptor with O_NOFOLLOW, so a swap of any path after validation cannot redirect the copy. Links are recreated
    as links (like copytree symlinks=True) and never followed; anything that is not a directory, regular file or link is
    refused. Every directory and file must be owned by `producer`, and a file must have a single link: a hard link could
    expose an inode the producer does not own. Entry count, total bytes and depth are capped, and permission bits are masked
    to 0o777 (no set-id or sticky bit is carried over). Any violation raises and the copy is abandoned."""
    limits = limits or CopyLimits()
    _need(depth <= MAX_COPY_DEPTH and os.fstat(src_fd).st_uid == producer)
    os.mkdir(dst, 0o700)
    with os.scandir(src_fd) as entries:
        names = [entry.name for entry in entries]
    for name in names:
        limits.entry()
        st = os.stat(name, dir_fd=src_fd, follow_symlinks=False)
        target = os.path.join(dst, name)
        mode = stat.S_IMODE(st.st_mode) & 0o777
        if stat.S_ISLNK(st.st_mode):
            os.symlink(os.readlink(name, dir_fd=src_fd), target)
        elif stat.S_ISDIR(st.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=src_fd)
            try:
                copy_tree_at(child, target, producer, limits, depth + 1)
                os.chmod(target, mode | 0o700)
            finally:
                os.close(child)
        elif stat.S_ISREG(st.st_mode):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=src_fd)
            with os.fdopen(fd, 'rb') as source:
                opened = os.fstat(source.fileno())
                _need(stat.S_ISREG(opened.st_mode) and opened.st_uid == producer and opened.st_nlink == 1)
                out = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode | 0o600)
                with os.fdopen(out, 'wb') as sink:
                    for chunk in iter(lambda: source.read(1 << 20), b''):
                        limits.data(len(chunk))
                        sink.write(chunk)
        else:
            raise Refuse()


def base_env(qa_home):
    """What a bare probe needs and nothing else."""
    return {'PATH': CHILD_PATH, 'HOME': qa_home}


def installed_env(qa_name, qa_home, outbox, codex_home):
    """ALLOWLIST environment for the QA child, built from the configured identity and the validated attempt. It reads no
    caller variable, so PATH, HOME, CODEX_HOME, LANG, TERM and SHELL of whoever started the helper cannot reach Codex. The
    attended isolated_env copies several of those from os.environ and is deliberately left alone."""
    home = os.path.join(outbox, 'home')
    return {'PATH': CHILD_PATH, 'HOME': qa_home, 'USER': qa_name, 'LOGNAME': qa_name,
            'TMPDIR': os.path.join(outbox, 'tmp'), 'XDG_CONFIG_HOME': home + '/.config',
            'XDG_DATA_HOME': home + '/.local/share', 'XDG_CACHE_HOME': home + '/.cache',
            'CARGO_TARGET_DIR': os.path.join(outbox, 'cargo-target'), 'CARGO_NET_OFFLINE': 'true', 'CODEX_HOME': codex_home}


def validate_installed(argv, config_path, chain_anchor, fds):
    verb, attempt, dry = parse_installed_argv(argv)
    config = read_config(config_path, chain_anchor)
    _need(os.getuid() == config['qa_uid'] and os.geteuid() == config['qa_uid'])
    caller = os.environ.get(PRODUCER_UID_ENV, '')
    _need(caller.isascii() and caller.isdigit() and int(caller) == config['producer_uid'])
    record = pwd.getpwuid(config['qa_uid'])
    _need(record.pw_name == config['qa_name'] and os.path.isabs(record.pw_dir))
    trusted_chain(config['codex_path'], chain_anchor)
    _need(sha256_file(config['codex_path']) == config['codex_sha256'])
    plan = {'verb': verb, 'dry_run': dry, 'qa_name': config['qa_name'], 'qa_home': record.pw_dir, 'producer_uid': config['producer_uid'], 'codex': config['codex_path'],
            'codex_sha256': config['codex_sha256'], 'exchange': config['exchange']}
    if verb == 'version':
        return plan
    request, afd = read_request(config, verb, attempt, fds)
    exchange = config['exchange']
    outbox = os.path.join(exchange, 'outbox', attempt)
    plan.update({'attempt': attempt, 'request': request, 'afd': afd, 'inbox_root': os.path.join(exchange, 'inbox'),
                 'outbox_root': os.path.join(exchange, 'outbox'), 'inbox': os.path.join(exchange, 'inbox', attempt),
                 'outbox': outbox, 'producer_home': pwd.getpwuid(config['producer_uid']).pw_dir,
                 'codex_home': os.path.join(record.pw_dir, '.local/state/coding-agent-environment/qa', attempt, 'codexhome')})
    return plan


def describe(plan):
    """Resolved paths and the argv that would run, as JSON-able data (--dry-run). Executes nothing."""
    verb = plan['verb']
    if verb == 'version':
        return {'verb': verb, 'argv': None, 'env': None, 'paths': {'helper': os.path.realpath(__file__), 'codex': plan['codex']}}
    paths = {key: plan[key] for key in ('exchange', 'inbox', 'outbox', 'codex_home', 'codex', 'qa_home')}
    if verb == 'probe':
        request = plan['request']
        argv = [PYTHON, '-I', '-c', '<probe source>', plan['inbox_root'], plan['outbox_root'], plan['exchange'],
                plan['producer_home'], str(request['pid']), request['sentinel']]
        env = base_env(plan['qa_home'])
    else:
        env = installed_env(plan['qa_name'], plan['qa_home'], plan['outbox'], plan['codex_home'])
        if verb == 'stage':
            argv = [plan['codex'], 'sandbox', '-P', PROFILE, '<per-control arguments>']
        else:
            argv = codex_argv(plan['codex'], Path(plan['outbox']))
    return {'verb': verb, 'attempt': plan['attempt'], 'argv': argv, 'env': env, 'paths': paths}


def pin_process(qa_home):
    """Drop the caller's environment from this process and aim the shared HOME-relative helpers at the QA home."""
    global HOME, CANARY_ROOT
    os.environ.clear()
    os.environ.update(base_env(qa_home))
    HOME = Path(qa_home)
    CANARY_ROOT = HOME / '.local/state/coding-agent-environment/qa-canary'


RELAYED = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


def relay_and_exit(child):
    """The parent half of detach(): forward TERM, INT and HUP to the detached child, wait for it and exit with its status
    (128+n when a signal ended it). It never returns. It holds no descriptor above 2, so the prompt, the exchange and the
    request files live only in the child."""
    os.closerange(3, 1024)

    def forward(signum, frame):
        try:
            os.kill(child, signum)
        except OSError:
            pass
    for sig in RELAYED:
        signal.signal(sig, forward)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, RELAYED)
    while True:
        try:
            _, status = os.waitpid(child, 0)
            break
        except InterruptedError:
            continue
        except ChildProcessError:
            os._exit(2)
    code = os.waitstatus_to_exitcode(status)
    os._exit(code if code >= 0 else 128 - code)


def detach(alarm_seconds, keep=()):
    """Fork, and in the child: new session (no controlling terminal), cwd /, stdin /dev/null, nothing inherited but 0-2 and
    the descriptors in keep, SIGALRM armed. Returns only in the child. The parent (the process the privilege tool started,
    and the one the producer signals) relays signals and passes the exit status on. The fork is what makes setsid safe: a
    process that leads its process group (the tool may start the helper that way) cannot call setsid, its child can."""
    signal.pthread_sigmask(signal.SIG_BLOCK, RELAYED)      # no signal is lost between the fork and the parent's relay
    pid = os.fork()
    if pid:
        relay_and_exit(pid)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, RELAYED)
    os.setsid()
    os.chdir('/')
    zero = os.open(os.devnull, os.O_RDWR)
    os.dup2(zero, 0)
    os.close(zero)
    start = 3
    for fd in sorted(keep):
        os.closerange(start, fd)
        start = fd + 1
    os.closerange(start, 1024)
    signal.alarm(alarm_seconds)


def run_installed(argv, config_path=None, chain_anchor='/'):
    """Entry for the installed verbs. config_path and chain_anchor exist only so tests can point at a scratch tree; main()
    never passes them, so in a real run the path is the constant and nothing from argv or the environment moves it."""
    fds = []
    try:
        return _act(validate_installed(argv, config_path or CONFIG_PATH, chain_anchor, fds))
    except Exception:
        return 2
    finally:
        # The validated descriptors stay open until the action is over: every later inbox access goes through them.
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass


def _act(plan):
    try:
        if plan['dry_run']:
            sys.stdout.write(json.dumps(describe(plan), sort_keys=True) + '\n')
            return 0
        if plan['verb'] == 'version':
            sys.stdout.write(json.dumps({'protocol': PROTOCOL, 'helper_sha256': sha256_file(os.path.realpath(__file__)),
                                         'codex_sha256': plan['codex_sha256']}, sort_keys=True) + '\n')
            return 0
        pin_process(plan['qa_home'])
        outbox, codex_home = plan['outbox'], plan['codex_home']
        env = installed_env(plan['qa_name'], plan['qa_home'], outbox, codex_home)
        ns = argparse.Namespace(inbox=plan['inbox'], outbox=outbox, codex_home=codex_home, codex_bin=plan['codex'])
        if plan['verb'] == 'stage':
            source_fd = open_source_dir(plan['afd'], plan['producer_uid'])
            return qa_stage(ns, lambda: env, lambda dst: copy_tree_at(source_fd, dst, plan['producer_uid']),
                            lambda: open_payload_at(plan['afd'], plan['producer_uid']))
        if plan['verb'] == 'exec':
            ns.timeout, ns.profile_sha256 = plan['request']['timeout'], plan['request']['profile_sha256']
            prompt = open_prompt_at(plan['afd'], plan['producer_uid'])
            detach(ns.timeout + ALARM_SLACK, keep=(prompt.fileno(),))
            return qa_exec(ns, lambda: env, lambda: prompt)
        detach(120)
        os.dup2(os.open(os.devnull, os.O_WRONLY), 2)
        request = plan['request']
        os.execve(PYTHON, [PYTHON, '-I', '-c', PROBE, plan['inbox_root'], plan['outbox_root'], plan['exchange'],
                           plan['producer_home'], str(request['pid']), request['sentinel']], base_env(plan['qa_home']))
    except Exception:
        return 2
    return 2


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    sub = parser.add_subparsers(dest='cmd', required=True, metavar='{qa-stage,qa-exec}')
    for name in ('qa-stage', 'qa-exec'):
        q = sub.add_parser(name)
        q.add_argument('--inbox', required=True)
        q.add_argument('--outbox', required=True)
        q.add_argument('--codex-home', required=True)
        q.add_argument('--codex-bin', default='codex')
        if name == 'qa-exec':
            q.add_argument('--timeout', type=int, default=1800)
            q.add_argument('--profile-sha256', required=True)
    return parser


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] in ALLOWED_ARGV:
        return run_installed(argv)
    args = build_parser().parse_args(argv)
    return {'qa-stage': qa_stage, 'qa-exec': qa_exec}[args.cmd](args)


if __name__ == '__main__':
    sys.exit(main())
