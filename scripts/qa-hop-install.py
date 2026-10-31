#!/usr/bin/python3 -I
"""One-time owner install of the unattended QA hop (SPE-153, ADR 0016).

  qa-hop-install.py plan     --qa-name N --qa-uid U --codex-source PATH --codex-sha256 HEX [--exchange DIR] [--producer-uid U]
  sudo /usr/bin/python3 -I qa-hop-install.py apply    (same arguments)    installs; root only, run through YOUR own sudo
  qa-hop-install.py verify   (same arguments)                              unprivileged re-check of the installed state
  sudo /usr/bin/python3 -I qa-hop-install.py rollback --qa-name N --qa-uid U   removes the drop-in first, then the files

`plan` (the default) changes nothing and needs no privilege. `apply` and `rollback` refuse to run unless the effective user is
root AND the caller's uid is known (the owner's own sudo sets it); the installer never asks for, reads or stores a password and
never calls sudo itself. Paths are fixed constants; there is deliberately no flag, environment variable or prefix that moves
them. Tests construct a Layout with scratch paths and injected operations instead.

What it installs, all root-owned with no links: /Library/cae-qa/{qa-side, qa-side.json, codex} and /etc/sudoers.d/cae-qa. The
drop-in grants exactly the seven fixed-argv commands of the ADR as the QA user and nothing else, and is validated with
`visudo -c -f` before it is moved into place (installed last), then the live configuration is checked again.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import pwd
import re
import stat
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HEX64 = re.compile(r'[0-9a-f]{64}')
MAX_CODEX_BYTES = 2 << 30
SAFE_ENV = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin'}


class Stop(Exception):
    """A refusal or failure; the message is for the owner and carries no secret."""


class Layout:
    """Where things go. Production uses the defaults; tests pass scratch paths. Nothing here is user-configurable."""

    def __init__(self, lib='/Library/cae-qa', sudoers='/etc/sudoers.d/cae-qa', root_uid=0, wheel_gid=0, anchor='/',
                 visudo='/usr/sbin/visudo'):
        self.lib, self.sudoers, self.root_uid, self.wheel_gid, self.anchor, self.visudo = (
            str(lib), str(sudoers), root_uid, wheel_gid, str(anchor), visudo)
        self.helper, self.config, self.codex = (os.path.join(self.lib, n) for n in ('qa-side', 'qa-side.json', 'codex'))


class Ops:
    """Everything that touches the system beyond plain file I/O, injectable so tests can prove what would happen."""

    def __init__(self, run=None, fchown=None, chown=None, geteuid=None, caller_uid=None):
        self.run = run or self._run
        self.fchown = fchown or os.fchown
        self.chown = chown or os.chown
        self.geteuid = geteuid or os.geteuid
        self.caller_uid = caller_uid or self._caller_uid

    @staticmethod
    def _run(argv):
        import subprocess
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60, cwd='/', env=SAFE_ENV)
        return done.returncode, (done.stdout + done.stderr)[-400:]

    @staticmethod
    def _caller_uid():
        """The user who invoked sudo (the producer), as the owner's own sudo records it. Not a secret."""
        text = os.environ.get('SUDO_UID', '')
        return int(text) if text.isascii() and text.isdigit() else None


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), HERE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------ inputs
class Inputs:
    def __init__(self, qa_name, qa_uid, producer_uid, exchange, codex_source, codex_sha256):
        self.qa_name, self.qa_uid, self.producer_uid, self.exchange = qa_name, qa_uid, producer_uid, str(exchange)
        self.codex_source, self.codex_sha256 = codex_source, codex_sha256
        self.qa_home = None
        self.producer_name = None


def validate_identity(qa_name, qa_uid, producer_uid, exchange):
    """The QA account must exist, carry the stated uid and the standard home, differ from root and the producer, and the
    exchange must be the direct child of /Users/Shared the setup script creates (its own rules are reused)."""
    qis = load('qa-identity-setup')
    if not isinstance(qa_name, str) or not qis.NAME_RE.match(qa_name) or qa_name in qis.RESERVED:
        raise Stop('--qa-name is not a valid QA account name')
    try:
        record = pwd.getpwnam(qa_name)
    except KeyError:
        raise Stop('--qa-name %s is not a local account (create it with qa-identity-setup.py first)' % qa_name)
    if record.pw_uid != qa_uid or qa_uid in (0, producer_uid) or producer_uid in (None, 0):
        raise Stop('--qa-uid must be the account\'s uid, unprivileged, and different from the invoking user')
    try:
        home, xroot = qis.check_args(qa_name, qa_uid, exchange)
    except qis.Stop as error:
        raise Stop(str(error))
    if Path(record.pw_dir) != home:
        raise Stop('the QA account home %s is not the expected %s' % (record.pw_dir, home))
    try:
        producer = pwd.getpwuid(producer_uid)
    except KeyError:
        raise Stop('the invoking uid has no account')
    return str(home), str(xroot), producer.pw_name


def sha256_stream(path, limit):
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise Stop('%s is not a regular file (links are refused; pass the resolved path)' % path)
    if st.st_size > limit:
        raise Stop('%s is larger than the allowed size' % path)
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_inputs(args, ops, need_codex=True):
    producer_uid = args.producer_uid if args.producer_uid is not None else (
        ops.caller_uid() if ops.geteuid() == 0 else os.getuid())
    inputs = Inputs(args.qa_name, args.qa_uid, producer_uid, args.exchange or '/Users/Shared/%s-qa' % args.qa_name,
                    args.codex_source, args.codex_sha256)
    inputs.qa_home, inputs.exchange, inputs.producer_name = validate_identity(
        args.qa_name, args.qa_uid, producer_uid, inputs.exchange)
    if need_codex:
        if not (isinstance(args.codex_sha256, str) and HEX64.fullmatch(args.codex_sha256)):
            raise Stop('--codex-sha256 must be 64 lower-case hex characters (the pin is explicit; nothing is downloaded)')
        if not args.codex_source or not os.path.isabs(args.codex_source) or os.path.normpath(args.codex_source) != args.codex_source:
            raise Stop('--codex-source must be an absolute, normalized path to the Codex binary to pin')
        if sha256_stream(args.codex_source, MAX_CODEX_BYTES) != args.codex_sha256:
            raise Stop('--codex-source does not have the digest given by --codex-sha256; nothing is installed')
    return inputs


# ------------------------------------------------------------------ drop-in
HEAD = '# cae-qa (SPE-153, ADR 0016). Managed file: reinstall, never edit by hand.'
ATTEMPT_RULE = '"^(probe|stage|exec) [A-Za-z0-9][A-Za-z0-9._-]*$"'
LABEL_RULE = '"^user/%d/[A-Za-z0-9_][A-Za-z0-9_.-]*$"'
SECURE_PATH = '/usr/bin:/bin:/usr/sbin:/sbin'


def render_sudoers(producer, qa_name, qa_uid, qa_home, helper_sha, helper_path='/Library/cae-qa/qa-side'):
    """The exact text of ADR 0016, section 'Sudoers drop-in': seven fixed-argv commands as the QA user, plus the pinned
    helper's two fixed shapes. Every value is validated by the caller; nothing is free text."""
    pad = ' ' * 26
    return '\n'.join([
        HEAD,
        'Defaults>%s env_reset, !setenv, secure_path="%s", always_set_home' % (qa_name, SECURE_PATH),
        '',
        'Cmnd_Alias CAE_QA_SWEEP = /bin/kill -KILL -1',
        'Cmnd_Alias CAE_QA_SEE   = /usr/bin/crontab -l, \\',
        pad + '/usr/bin/atq "", \\',
        pad + '/bin/ls -A %s/Library/LaunchAgents, \\' % qa_home,
        pad + '/bin/launchctl print user/%d, \\' % qa_uid,
        pad + '/bin/launchctl print gui/%d, \\' % qa_uid,
        pad + '/bin/launchctl print ' + LABEL_RULE % qa_uid,
        'Cmnd_Alias CAE_QA_SIDE  = sha256:%s %s version, \\' % (helper_sha, helper_path),
        pad + 'sha256:%s %s %s' % (helper_sha, helper_path, ATTEMPT_RULE),
        '',
        '%s ALL = (%s) NOPASSWD: NOSETENV: CAE_QA_SWEEP, CAE_QA_SEE, CAE_QA_SIDE' % (producer, qa_name),
        ''])


def sudoers_findings(text, qa_name):
    """Independent guard on the rendered text: anything beyond the ADR's fixed shapes is a finding. Run before writing and
    in the tests, on top of the rendered text being compared with the canonical one."""
    found = []
    stripped = text.replace(ATTEMPT_RULE, '<A>').replace(LABEL_RULE % 0, '<L>')
    stripped = re.sub(r'"\^user/\d+/\[A-Za-z0-9_\]\[A-Za-z0-9_\.-\]\*\$"', '<L>', stripped)
    for token in ('*', '?', 'NOPASSWD: ALL', 'NOPASSWD:ALL', '(ALL', '(root', '(#', '(%', 'SETENV:', '!authenticate', 'EXEC',
                  '/bin/sh', '/bin/bash', '/bin/zsh', 'python', 'env ', 'sudo', 'su ', '/usr/bin/id', '..'):
        if token in stripped.replace('NOSETENV:', '').replace('!setenv', ''):
            found.append('forbidden token %r' % token)
    grants = [line for line in text.splitlines() if ' ALL = (' in line]
    if len(grants) != 1 or not re.fullmatch(r'[a-z_][a-z0-9._-]* ALL = \(%s\) NOPASSWD: NOSETENV: CAE_QA_SWEEP, CAE_QA_SEE, CAE_QA_SIDE'
                                           % re.escape(qa_name), grants[0]):
        found.append('the grant line is not the single fixed one')
    if text.count('/bin/kill') != 1 or text.count('/bin/launchctl') != 3 or text.count('/usr/bin/crontab') != 1:
        found.append('the command set is not the ADR\'s')
    return found


# ------------------------------------------------------------------ plan
def helper_source():
    path = HERE / 'qa-side.py'
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o022:
        raise Stop('qa-side.py must be a regular file that is not group or world writable')
    return path, path.read_bytes()


def config_text(inputs, layout):
    return json.dumps({'qa_name': inputs.qa_name, 'qa_uid': inputs.qa_uid, 'producer_uid': inputs.producer_uid,
                       'exchange': inputs.exchange, 'codex_path': layout.codex, 'codex_sha256': inputs.codex_sha256},
                      sort_keys=True, indent=2) + '\n'


def planned(inputs, layout):
    """(files, drop-in text) the install would create: path, owner, mode, sha256, source."""
    _, helper = helper_source()
    helper_sha = hashlib.sha256(helper).hexdigest()
    config = config_text(inputs, layout).encode()
    dropin = render_sudoers(inputs.producer_name, inputs.qa_name, inputs.qa_uid, inputs.qa_home, helper_sha, layout.helper)
    findings = sudoers_findings(dropin, inputs.qa_name)
    if findings:
        raise Stop('the rendered drop-in failed its own check: ' + '; '.join(findings))
    files = [
        (layout.lib, 'root:wheel', '0755', 'directory', None),
        (layout.helper, 'root:wheel', '0555', helper_sha, 'scripts/qa-side.py'),
        (layout.config, 'root:wheel', '0444', hashlib.sha256(config).hexdigest(), 'generated'),
        (layout.codex, 'root:wheel', '0555', inputs.codex_sha256, inputs.codex_source),
        (layout.sudoers, 'root:wheel', '0440', hashlib.sha256(dropin.encode()).hexdigest(), 'rendered below'),
    ]
    return files, dropin, helper, config


def visudo_check(layout, ops, path):
    code, out = ops.run([layout.visudo, '-c', '-f', str(path)])
    return code == 0 and 'parsed OK' in out, out


def plan_text(inputs, layout, ops, args_line):
    files, dropin, _, config = planned(inputs, layout)
    ok = None
    import tempfile
    with tempfile.TemporaryDirectory(prefix='cae-qa-plan-') as scratch:
        probe = Path(scratch) / 'cae-qa'
        probe.write_text(dropin)
        os.chmod(probe, 0o440)
        ok, _ = visudo_check(layout, ops, probe)
    lines = ['PLAN (nothing has been changed; this was computed without privilege)', '',
             'Identity: QA user %s (uid %d, home %s), invoking user %s (uid %d), exchange %s' % (
                 inputs.qa_name, inputs.qa_uid, inputs.qa_home, inputs.producer_name, inputs.producer_uid, inputs.exchange), '',
             'Files (all root-owned, no links):']
    for path, owner, mode, digest, source in files:
        lines.append('  %-28s %s %s  sha256 %s%s' % (path, owner, mode, digest, '' if not source else '  <- ' + source))
    lines += ['', 'Pinned Codex: copy of %s (digest %s, about 300 MB, decision D1; nothing is downloaded)' % (
        inputs.codex_source, inputs.codex_sha256), '',
        'Config %s:' % layout.config] + ['  ' + l for l in config.decode().splitlines()] + [
        '', 'Sudoers drop-in %s (visudo -c -f on this exact text, unprivileged: %s):' % (layout.sudoers, 'parsed OK' if ok else 'NOT VERIFIED')]
    lines += ['  ' + l for l in dropin.splitlines()]
    lines += ['', 'The one command that needs root (run it through your own sudo; it never asks for or stores a password):',
              '  sudo /usr/bin/python3 -I %s apply %s' % (Path(__file__).resolve(), args_line), '',
              'What that command does, in order:',
              '  1. refuses unless it runs as root with the invoking uid known, and re-validates every input above',
              '  2. creates %s (root:wheel 0755) and writes each file to a temporary name, sets owner and mode, checks the digest, renames it' % layout.lib,
              '  3. writes the drop-in to a temporary name beside %s, runs `visudo -c -f` on it, and only then renames it into place (last)' % layout.sudoers,
              '  4. runs `visudo -c` on the live configuration; if that fails the drop-in is removed at once',
              '  5. re-runs the same producer-side checks `qa-handoff.py --unattended` uses (as root, read-only); no helper verb is run as root', '',
              'Afterwards, unprivileged: python3 -I %s verify %s' % (Path(__file__).resolve(), args_line),
              '  and, as you, the install-time controls of ADR 0016 (`sudo -n -u %s %s version` must print the digests above).' % (
                  inputs.qa_name, layout.helper), '',
              'Rollback (root; removes the drop-in FIRST, then the files; safe to repeat):',
              '  sudo /usr/bin/python3 -I %s rollback --qa-name %s --qa-uid %d' % (Path(__file__).resolve(), inputs.qa_name, inputs.qa_uid), '',
              'Eliminated by this install: typing `sudo -v` before every run, launching from that Terminal, the sudo keepalive.',
              'Still yours: this one root command; reinstalling when qa-side.py or the pinned Codex changes; the QA user\'s Codex device',
              '  login (qa-identity-setup.py prints it, followed by chmod 700 ~/.codex and a --check re-run); rechecking after a macOS major upgrade.']
    return '\n'.join(lines) + '\n'


# ------------------------------------------------------------------ apply
def need_root(layout, ops):
    if ops.geteuid() != layout.root_uid:
        raise Stop('this mode must run as root through your own sudo: sudo /usr/bin/python3 -I %s ...' % Path(__file__).resolve())


def lib_dir(layout, ops, create):
    """/Library/cae-qa: every existing component root-owned, not writable by others, not a link."""
    side = load('qa-side')
    side.ROOT_UID = layout.root_uid
    parent = os.path.dirname(layout.lib)
    try:
        side.trusted_chain(parent, layout.anchor, final_regular=False)
    except (side.Refuse, OSError):
        raise Stop('%s or a directory above it is not root-owned and non-writable (or is a link); refusing' % parent)
    if os.path.lexists(layout.lib):
        st = os.lstat(layout.lib)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != layout.root_uid or st.st_mode & 0o022:
            raise Stop('%s exists but is not a root-owned, non-writable directory; refusing' % layout.lib)
    elif create:
        os.mkdir(layout.lib, 0o755)
        os.chmod(layout.lib, 0o755)
        ops.chown(layout.lib, layout.root_uid, layout.wheel_gid)


def install_file(layout, ops, dest, mode, source=None, data=None, expect_sha=None):
    """Write to a temporary name in the same root-owned directory, set owner and mode on the descriptor, verify the digest,
    then rename over the destination: a reader never sees a partial or foreign file."""
    tmp = os.path.join(os.path.dirname(dest), '.' + os.path.basename(dest) + '.new')
    if os.path.lexists(tmp):
        os.unlink(tmp)
    digest = hashlib.sha256()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        ops.fchown(fd, layout.root_uid, layout.wheel_gid)
        with os.fdopen(fd, 'wb', closefd=False) as sink:
            if source is not None:
                src = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(src, 'rb') as handle:
                    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                        raise Stop('%s is not a regular file' % source)
                    for chunk in iter(lambda: handle.read(1 << 20), b''):
                        digest.update(chunk)
                        sink.write(chunk)
            else:
                digest.update(data)
                sink.write(data)
        if expect_sha and digest.hexdigest() != expect_sha:
            raise Stop('digest mismatch while writing %s; nothing was moved into place' % dest)
        os.fchmod(fd, mode)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.close(fd)
    os.rename(tmp, dest)
    return digest.hexdigest()


def apply(inputs, layout, ops, say):
    need_root(layout, ops)
    if ops.caller_uid() != inputs.producer_uid:
        raise Stop('the invoking uid does not match the producer uid; run this through your own sudo')
    files, dropin, helper, config = planned(inputs, layout)
    lib_dir(layout, ops, create=True)
    helper_sha = files[1][3]
    install_file(layout, ops, layout.helper, 0o555, data=helper, expect_sha=helper_sha)
    install_file(layout, ops, layout.config, 0o444, data=config)
    install_file(layout, ops, layout.codex, 0o555, source=inputs.codex_source, expect_sha=inputs.codex_sha256)
    say('installed %s, %s, %s' % (layout.helper, layout.config, layout.codex))
    # Last: the grant itself. Candidate beside the destination under a name sudo ignores, validated, then renamed.
    candidate = os.path.join(os.path.dirname(layout.sudoers), '.cae-qa.new')
    install_file(layout, ops, candidate, 0o440, data=dropin.encode())
    ok, out = visudo_check(layout, ops, candidate)
    if not ok:
        os.unlink(candidate)
        raise Stop('visudo rejected the drop-in; it was not installed (%s)' % out.strip()[-200:])
    os.rename(candidate, layout.sudoers)
    ok, out = ops.run([layout.visudo, '-c'])
    if ok != 0:
        os.unlink(layout.sudoers)
        raise Stop('visudo -c failed on the live configuration; the drop-in was removed again (%s)' % out.strip()[-200:])
    say('installed %s (validated with visudo before and after)' % layout.sudoers)
    record, problems = verify_installed(inputs, layout, producer_uid=inputs.producer_uid)
    if problems:
        raise Stop('post-install verification failed: ' + '; '.join(problems) + ' (run rollback to remove the install)')
    say('verified: helper %s, codex %s' % (record['helper_sha256'], record['codex_sha256']))
    say('Next, as yourself (not root): verify, then `sudo -n -u %s %s version`.' % (inputs.qa_name, layout.helper))


# ------------------------------------------------------------------ verify / rollback
def verify_installed(inputs, layout, producer_uid=None):
    """The producer-side check qa-handoff --unattended runs before every run, from the same code."""
    handoff = load('qa-handoff')
    handoff._SIDE.ROOT_UID = layout.root_uid
    ident = handoff.Identity(inputs.qa_name, inputs.qa_uid, Path(inputs.qa_home), Path(inputs.exchange), layout.root_uid)
    record, problems = handoff.unattended_install(ident, layout.config, layout.helper, layout.anchor, producer_uid)
    if os.path.lexists(layout.sudoers):
        st = os.lstat(layout.sudoers)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_uid != layout.root_uid or st.st_mode & 0o7777 != 0o440:
            problems = list(problems) + ['the sudoers drop-in is not a root-owned 0440 regular file']
    else:
        problems = list(problems) + ['the sudoers drop-in is not present']
    return record, problems


def rollback(layout, ops, say):
    need_root(layout, ops)
    removed = []
    for path in (layout.sudoers, os.path.join(os.path.dirname(layout.sudoers), '.cae-qa.new'), layout.helper, layout.config,
                 layout.codex, *(os.path.join(layout.lib, '.%s.new' % n) for n in ('qa-side', 'qa-side.json', 'codex'))):
        if os.path.lexists(path):
            st = os.lstat(path)
            if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
                raise Stop('%s is a directory, not one of our files; refusing to remove it' % path)
            os.unlink(path)           # a link is removed itself, never followed
            removed.append(path)
            if path == layout.sudoers:
                say('removed %s (the producer has no grant now)' % path)
    if os.path.isdir(layout.lib) and not os.path.islink(layout.lib):
        if not os.listdir(layout.lib):
            os.rmdir(layout.lib)
            removed.append(layout.lib)
        else:
            say('left %s in place: it holds files this installer did not create' % layout.lib)
    say('removed: ' + (', '.join(removed) if removed else 'nothing (already rolled back)'))
    return removed


# ------------------------------------------------------------------ cli
def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='mode', required=True)
    for name in ('plan', 'apply', 'verify', 'rollback'):
        p = sub.add_parser(name)
        p.add_argument('--qa-name', required=True)
        p.add_argument('--qa-uid', required=True, type=int)
        p.add_argument('--exchange')
        p.add_argument('--producer-uid', type=int)
        if name != 'rollback':
            p.add_argument('--codex-source', required=True, help='absolute path of the Codex binary to pin (no default, no download)')
            p.add_argument('--codex-sha256', required=True, help='its SHA-256: the explicit pin')
    return parser


def args_line(args):
    parts = ['--qa-name', args.qa_name, '--qa-uid', str(args.qa_uid), '--codex-source', args.codex_source, '--codex-sha256', args.codex_sha256]
    if args.exchange:
        parts += ['--exchange', args.exchange]
    return ' '.join(parts)


def main(argv=None, layout=None, ops=None, out=None):
    layout, ops = layout or Layout(), ops or Ops()
    say = (out or (lambda text: print(text)))
    args = build_parser().parse_args(argv)
    mode = args.mode
    try:
        if mode == 'rollback':
            rollback(layout, ops, say)
            return 0
        inputs = resolve_inputs(args, ops)
        if mode == 'plan':
            say(plan_text(inputs, layout, ops, args_line(args)).rstrip('\n'))
        elif mode == 'apply':
            apply(inputs, layout, ops, say)
        elif mode == 'verify':
            record, problems = verify_installed(inputs, layout)
            if problems:
                say('NOT VERIFIED: ' + '; '.join(problems))
                return 1
            say('VERIFIED: helper %s codex %s' % (record['helper_sha256'], record['codex_sha256']))
        return 0
    except Stop as error:
        say('REFUSED: %s' % error)
        return 1
    except OSError as error:
        say('FAILED: %s (%s)' % (type(error).__name__, error.strerror or ''))
        return 1


if __name__ == '__main__':
    sys.exit(main())
