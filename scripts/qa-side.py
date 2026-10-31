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
import shutil
import signal
import stat
import subprocess
import sys
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
)


def confinement_probe(codex_bin, run_dir, env):
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
    results, problems = [], []
    try:
        # A scratch clone whose own .codex/config.toml tries to add the canary to the read allowlist: the profile must hold.
        hostile = run_dir / 'probe-hostile'
        shutil.rmtree(hostile, ignore_errors=True)       # a wrapper killed mid-probe may have left one behind
        (hostile / '.codex').mkdir(parents=True)
        (hostile / '.codex/config.toml').write_text('[permissions.%s.filesystem]\n%s = "read"\n' % (PROFILE, json.dumps(str(canary_dir))))
        for name, snippet, expect in CONTROLS:
            if name == 'neg_open_auth_file' and not auth.exists():
                continue          # no login file to protect; the open() is the denied operation, no content is read
            code = '# CONTROL=%s\n%s' % (name, snippet.format(**values))
            cwd = hostile if name == 'neg_project_config_ignored' else run_dir / 'source'
            argv = [codex_bin, 'sandbox', '-P', PROFILE, '-C', str(cwd), '/usr/bin/python3', '-c', code]
            try:
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


def qa_stage(args):
    """QA side, hop 1 (runs as the QA user). Prints exactly one JSON record on stdout, before any model starts."""
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 2)           # the hop joins stderr to the pipe; only the record may reach the producer
    os.close(devnull)
    os.umask(0o077)
    try:
        record = stage_outbox(args)
    except Exception as error:
        record = {'error': type(error).__name__}
    sys.stdout.write(json.dumps(record) + '\n')
    sys.stdout.flush()
    return 0


def stage_outbox(args):
    inbox, outbox = Path(args.inbox), Path(args.outbox)
    outbox.mkdir(mode=0o700)      # fails if anything already exists under this attempt name
    for sub in ('out', 'tmp', 'home', 'cargo-target'):
        (outbox / sub).mkdir(mode=0o700)
    shutil.copytree(inbox / 'source', outbox / 'source', symlinks=True)
    write_private(outbox / 'verdict.schema.json', VERDICT_SCHEMA)
    if not shutil.which(args.codex_bin):
        return {'codex_missing': True}
    if not (real_codex_home() / 'auth.json').exists():
        return {'login_missing': True}     # otherwise the probe would skip the login control, which the producer requires
    home = prepare_codex_home(outbox, home=Path(args.codex_home))
    record, _ = confinement_probe(args.codex_bin, outbox, isolated_env(outbox, home))
    return {'confinement': record, 'profile_sha256': sha256_file(home / 'config.toml')}


class QAStop(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def qa_exec(args):
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
        with open_regular(inbox / 'prompt.txt') as prompt:
            try:
                proc = subprocess.Popen(codex_argv(args.codex_bin, outbox), cwd=str(outbox / 'source'),
                                        env=isolated_env(outbox, Path(args.codex_home)), stdin=prompt, start_new_session=True)
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
    args = build_parser().parse_args(argv)
    return {'qa-stage': qa_stage, 'qa-exec': qa_exec}[args.cmd](args)


if __name__ == '__main__':
    sys.exit(main())
