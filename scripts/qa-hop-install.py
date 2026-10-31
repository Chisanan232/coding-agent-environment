#!/usr/bin/python3 -I
"""One-time owner install of the unattended QA hop (SPE-153, ADR 0016).

  qa-hop-install.py plan     --qa-name N --qa-uid U --codex-source PATH --codex-sha256 HEX [--exchange DIR] [--producer-uid U]
  sudo /usr/bin/python3 -I -B /Library/cae-qa-install/qa-hop-install.py apply (same arguments) --bundle-sha256 H --helper-sha256 H
                                                                         installs; root only, run through YOUR own sudo
  qa-hop-install.py verify   (same arguments)                              unprivileged re-check of the installed state
  sudo /usr/bin/python3 -I qa-hop-install.py rollback --qa-name N --qa-uid U   removes the drop-in first, then the files

Root must never run code a less-privileged user can change. `apply` and `rollback` therefore refuse unless this file, the three
scripts it loads (qa-identity-setup.py, qa-handoff.py, qa-side.py) and every directory above them are root-owned, not group or
world writable and contain no link (run it from a root-owned copy), and `apply` additionally requires the owner to pin the
combined digest of those four files and the helper's digest, recomputed here from the bytes it actually loads and copies.
`plan` prints those digests; compare them with the reviewed PR head before approving.

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
import json
import os
import pwd
import re
import stat
import sys
import types
from pathlib import Path

sys.dont_write_bytecode = True        # root must never write __pycache__ into a checkout (run with -I -B as well)

HERE = Path(__file__).resolve().parent
HEX64 = re.compile(r'[0-9a-f]{64}')
BUNDLE_NAMES = ('qa-hop-install.py', 'qa-identity-setup.py', 'qa-handoff.py', 'qa-side.py')
# 32- and 64-bit thin Mach-O in both byte orders, then the 32- and 64-bit fat (universal) headers in both byte orders.
MACHO_MAGIC = (b'\xfe\xed\xfa\xce', b'\xfe\xed\xfa\xcf', b'\xce\xfa\xed\xfe', b'\xcf\xfa\xed\xfe',
               b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca', b'\xca\xfe\xba\xbf', b'\xbf\xba\xfe\xca')
PRODUCER_RE = re.compile(r'[A-Za-z_][A-Za-z0-9._-]{0,31}')
INSTALL_COPY = '/Library/cae-qa-install'
MAX_CODEX_BYTES = 2 << 30
SAFE_ENV = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin'}


class Stop(Exception):
    """A refusal or failure; the message is for the owner and carries no secret."""


class Layout:
    """Where things go. Production uses the defaults; tests pass scratch paths. Nothing here is user-configurable."""

    def __init__(self, lib='/Library/cae-qa', sudoers='/etc/sudoers.d/cae-qa', root_uid=0, wheel_gid=0, anchor='/',
                 visudo='/usr/sbin/visudo', bundle_dir=None):
        self.lib, self.sudoers, self.root_uid, self.wheel_gid, self.anchor, self.visudo = (
            str(lib), str(sudoers), root_uid, wheel_gid, str(anchor), visudo)
        self.bundle_dir = str(bundle_dir or HERE)
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


def chain_problems(path, root_uid, anchor, directory=False):
    """Messages for every component from `anchor` down to `path` that is a link, not owned by root, or writable by group or
    other; the last must be a regular file (or a directory). Empty means trusted. Carries paths, never contents."""
    anchor = os.path.normpath(anchor)
    prefix = anchor if anchor.endswith('/') else anchor + '/'
    if not (os.path.isabs(path) and os.path.normpath(path) == path and (path == anchor or path.startswith(prefix))):
        return ['%s is not a normalized path under %s' % (path, anchor)]
    parts = [''] + [p for p in path[len(prefix):].split('/') if p]
    current, found = anchor, []
    for index, part in enumerate(parts):
        if part:
            current = os.path.join(current, part)
        try:
            st = os.lstat(current)
        except OSError:
            return found + ['%s cannot be examined' % current]
        if stat.S_ISLNK(st.st_mode):
            found.append('%s is a link' % current)
        if st.st_uid != root_uid:
            found.append('%s is not root-owned' % current)
        if st.st_mode & 0o022:
            found.append('%s is writable by group or other' % current)
        last = index == len(parts) - 1
        if (stat.S_ISREG(st.st_mode) if last and not directory else stat.S_ISDIR(st.st_mode)) is False:
            found.append('%s is not a %s' % (current, 'regular file' if last and not directory else 'directory'))
    return found


def read_regular(path, limit=16 << 20):
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
        raise Stop('%s is not a regular file of acceptable size' % path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        return handle.read(limit + 1)


class Bundle:
    """The four files root executes or copies, each read exactly once. Loaded modules are executed from these very bytes and
    the helper that is installed is the very same bytes, so a digest pin binds what runs and what is installed."""

    def __init__(self, directory):
        self.dir = str(directory)
        self.data = {name: read_regular(os.path.join(self.dir, name)) for name in BUNDLE_NAMES}
        self.sha = {name: hashlib.sha256(data).hexdigest() for name, data in self.data.items()}
        self.combined = hashlib.sha256(''.join('%s %s\n' % (n, self.sha[n]) for n in sorted(self.sha)).encode()).hexdigest()
        self.helper = self.data['qa-side.py']
        self.helper_sha = self.sha['qa-side.py']
        self._modules = {}

    def module(self, name):
        file = name + '.py'
        if file not in self._modules:
            path = os.path.join(self.dir, file)
            module = types.ModuleType(name.replace('-', '_'))
            module.__file__ = path
            exec(compile(self.data[file], path, 'exec'), module.__dict__)
            self._modules[file] = module
        return self._modules[file]

    def trust_problems(self, layout, only=None):
        found = []
        for name in (only or BUNDLE_NAMES):
            found += chain_problems(os.path.join(self.dir, name), layout.root_uid, layout.anchor)
        return found


# ------------------------------------------------------------------ inputs
class Inputs:
    def __init__(self, qa_name, qa_uid, producer_uid, exchange, codex_source, codex_sha256):
        self.qa_name, self.qa_uid, self.producer_uid, self.exchange = qa_name, qa_uid, producer_uid, str(exchange)
        self.codex_source, self.codex_sha256 = codex_source, codex_sha256
        self.qa_home = None
        self.producer_name = None


def validate_identity(bundle, qa_name, qa_uid, producer_uid, exchange):
    """The QA account must exist, carry the stated uid and the standard home, differ from root and the producer, and the
    exchange must be the direct child of /Users/Shared the setup script creates (its own rules are reused)."""
    qis = bundle.module('qa-identity-setup')
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
    if not PRODUCER_RE.fullmatch(producer.pw_name) or producer.pw_name in ('ALL', 'root'):
        raise Stop('the invoking account name is not one this installer will write into a sudoers rule')
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


def resolve_inputs(args, ops, bundle, need_codex=True):
    producer_uid = args.producer_uid if args.producer_uid is not None else (
        ops.caller_uid() if ops.geteuid() == 0 else os.getuid())
    inputs = Inputs(args.qa_name, args.qa_uid, producer_uid, args.exchange or '/Users/Shared/%s-qa' % args.qa_name,
                    args.codex_source, args.codex_sha256)
    inputs.qa_home, inputs.exchange, inputs.producer_name = validate_identity(
        bundle, args.qa_name, args.qa_uid, producer_uid, inputs.exchange)
    if need_codex:
        if not (isinstance(args.codex_sha256, str) and HEX64.fullmatch(args.codex_sha256)):
            raise Stop('--codex-sha256 must be 64 lower-case hex characters (the pin is explicit; nothing is downloaded)')
        if not args.codex_source or not os.path.isabs(args.codex_source) or os.path.normpath(args.codex_source) != args.codex_source:
            raise Stop('--codex-source must be an absolute, normalized path to the Codex binary to pin')
        if sha256_stream(args.codex_source, MAX_CODEX_BYTES) != args.codex_sha256:
            raise Stop('--codex-source does not have the digest given by --codex-sha256; nothing is installed')
        with open(args.codex_source, 'rb') as handle:
            if handle.read(4) not in MACHO_MAGIC:
                raise Stop('--codex-source is not a Mach-O binary; a launcher script needs sibling files the pin does not copy')
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


def sudoers_findings(text, expected):
    """Exact comparison with the rendered ADR lines: every line must be one of them, none may be missing, and the order and
    count must match. Anything else (an added grant, a widened command, a changed target) is a finding."""
    got, want = text.splitlines(), expected.splitlines()
    found = ['unexpected line: %r' % line for line in got if line not in want]
    found += ['missing line: %r' % line for line in want if line not in got]
    if not found and got != want:
        found.append('the lines differ in count or order')
    return found


# ------------------------------------------------------------------ plan
def config_text(inputs, layout):
    return json.dumps({'qa_name': inputs.qa_name, 'qa_uid': inputs.qa_uid, 'producer_uid': inputs.producer_uid,
                       'exchange': inputs.exchange, 'codex_path': layout.codex, 'codex_sha256': inputs.codex_sha256},
                      sort_keys=True, indent=2) + '\n'


def planned(inputs, layout, bundle):
    """(files, drop-in text, helper bytes, config bytes) the install would create: path, owner, mode, sha256, source."""
    helper = bundle.helper
    helper_sha = bundle.helper_sha
    config = config_text(inputs, layout).encode()
    dropin = render_sudoers(inputs.producer_name, inputs.qa_name, inputs.qa_uid, inputs.qa_home, helper_sha, layout.helper)
    files = [
        (layout.lib, 'root:wheel', '0755', 'directory', None),
        (layout.helper, 'root:wheel', '0555', helper_sha, 'qa-side.py (the bundle copy)'),
        (layout.config, 'root:wheel', '0444', hashlib.sha256(config).hexdigest(), 'generated'),
        (layout.codex, 'root:wheel', '0555', inputs.codex_sha256, inputs.codex_source),
        (layout.sudoers, 'root:wheel', '0440', hashlib.sha256(dropin.encode()).hexdigest(), 'rendered below'),
    ]
    return files, dropin, helper, config


def visudo_check(layout, ops, path):
    code, out = ops.run([layout.visudo, '-c', '-f', str(path)])
    return code == 0 and 'parsed OK' in out, out


def copy_flow(names=BUNDLE_NAMES):
    """The owner's copy-and-verify block, printed by `plan` and quoted verbatim in docs/QA-HANDOFF.md (a test keeps them equal).
    Variables, every expansion double-quoted (paths with spaces; zsh-safe), a new empty directory, git objects instead of the
    working tree, a refusal of links/directories/extras before chown or chmod touch anything, and hashing after the copy."""
    scripts = ' '.join('scripts/' + n for n in names)
    stems, ordered = ' '.join(n[:-3] for n in names), ' '.join(n[:-3] for n in sorted(names))
    return [
        '# Paste the whole block: it runs in a subshell and stops at the first failure or REFUSED. Set REPO (your checkout) and H',
        '# (the full commit id you reviewed) on the first line; D is fixed.',
        '(',
        'set -e',
        "REPO='<REPO>'; H='<REVIEWED_HEAD>'; D=%s" % INSTALL_COPY,
        '# 1. A new or empty directory only.',
        'sudo install -d -m 755 -o root -g wheel "${D}"',
        'test -z "$(sudo ls -A "${D}")" || { echo "REFUSED: ${D} already has content; move it aside"; exit 1; }',
        '# 2. Extract from git objects of the reviewed commit, never from the working tree.',
        'git -C "${REPO}" archive "${H}" %s | sudo tar -x -o -C "${D}" --strip-components=1' % scripts,
        '# 3. Refuse a link, directory or extra file BEFORE any chown or chmod could follow it.',
        'test -z "$(sudo find "${D}" -mindepth 1 ! -type f)" || { echo "REFUSED: the extract holds a link, a directory or an unexpected file"; exit 1; }',
        'test "$(sudo find "${D}" -type f | wc -l | tr -d \' \')" = %d || { echo "REFUSED: the extract is not exactly %d files"; exit 1; }' % (len(names), len(names)),
        '# 4. Root-own the copy (-h: never follow a link).',
        'sudo chown -R -h root:wheel "${D}"',
        'sudo chmod 755 "${D}"',
        'sudo chmod -h 644 "${D}"/*.py',
        '# 5. Hash the copy AFTER the copy; each pair of lines must be identical.',
        'for f in %s; do shasum -a 256 "${D}/${f}.py"; git -C "${REPO}" show "${H}:scripts/${f}.py" | shasum -a 256; done' % stems,
        '# 6. Rebuild the combined bundle digest from the git objects (sha256 of the sorted "name sha256" lines); it must equal the',
        '#    combined digest printed by plan. git rev-parse blob ids are SHA-1: informational only, never the pin.',
        'for f in %s; do printf \'%%s %%s\\n\' "${f}.py" "$(git -C "${REPO}" show "${H}:scripts/${f}.py" | shasum -a 256 | cut -d\' \' -f1)"; '
        'done | shasum -a 256' % ordered,
        ')']


def plan_text(inputs, layout, ops, args_line, bundle):
    files, dropin, _, config = planned(inputs, layout, bundle)
    import tempfile
    with tempfile.TemporaryDirectory(prefix='cae-qa-plan-') as scratch:
        probe = Path(scratch) / 'cae-qa'
        probe.write_text(dropin)
        os.chmod(probe, 0o440)
        ok, _ = visudo_check(layout, ops, probe)
    copy = INSTALL_COPY + '/qa-hop-install.py'
    pins = '--bundle-sha256 %s --helper-sha256 %s' % (bundle.combined, bundle.helper_sha)
    lines = ['PLAN (nothing has been changed; this was computed without privilege)', '',
             'Identity: QA user %s (uid %d, home %s), invoking user %s (uid %d), exchange %s' % (
                 inputs.qa_name, inputs.qa_uid, inputs.qa_home, inputs.producer_name, inputs.producer_uid, inputs.exchange), '',
             'Code that root would run or install (compare with the reviewed PR head before approving):']
    for name in sorted(bundle.sha):
        lines.append('  %-22s sha256 %s' % (name, bundle.sha[name]))
    lines += ['  combined bundle digest %s' % bundle.combined, '  helper digest          %s (qa-side.py, installed as %s)' % (
        bundle.helper_sha, layout.helper), '', 'Files (all root-owned, no links):']
    for path, owner, mode, digest, source in files:
        lines.append('  %-28s %s %s  sha256 %s%s' % (path, owner, mode, digest, '' if not source else '  <- ' + source))
    lines += ['', 'Pinned Codex: copy of %s (Mach-O binary, digest %s, about 300 MB, decision D1; nothing is downloaded)' % (
        inputs.codex_source, inputs.codex_sha256), '',
        'Config %s:' % layout.config] + ['  ' + l for l in config.decode().splitlines()] + [
        '', 'Sudoers drop-in %s (visudo -c -f on this exact text, unprivileged: %s):' % (layout.sudoers, 'parsed OK' if ok else 'NOT VERIFIED')]
    lines += ['  ' + l for l in dropin.splitlines()]
    lines += ['', 'Root must run a root-owned copy of the four scripts, never the checkout, and that copy must come from GIT OBJECTS of the',
              'commit you reviewed, not from a working tree (this plan was computed by the checkout\'s own code, which proves nothing about',
              'itself). Run this block line by line in your shell, with REPO and H set on its first line:'] + [
        '  ' + l for l in copy_flow()] + [
              '  (the ids from `git -C "${REPO}" rev-parse "${H}:scripts/<file>"` are git SHA-1 blob ids: informational only, never the pin)',
              'Only if every pair matches and the combined digest equals the one printed above, run the one command that then needs root',
              '(your own sudo; it never asks for or stores a password):',
              '  sudo /usr/bin/python3 -I -B %s apply %s %s' % (copy, args_line, pins), '',
              'What that command does, in order:',
              '  1. refuses unless it is root, the four scripts and every directory above them are root-owned, not group/world writable and',
              '     link-free, and the bundle and helper digests above equal what it reads from those very files',
              '  2. re-validates every input above; the Codex source must be a regular Mach-O file with the pinned digest',
              '  3. creates %s (root:wheel 0755) and writes each file to a temporary name, sets owner and mode, checks the digest, renames it' % layout.lib,
              '  4. resolves %s once, requires the real directory to be root-owned and link-free, writes the drop-in to a temporary name there,' % os.path.dirname(layout.sudoers),
              '     reads it back and compares it line by line, runs `visudo -c -f` on it, and only then renames it into place (last)',
              '  5. runs `visudo -c` on the live configuration; if that fails the drop-in is removed at once',
              '  6. re-runs the same producer-side checks `qa-handoff.py --unattended` uses (as root, read-only); if they fail the drop-in is removed',
              '     again (no helper verb is run as root)', '',
              'Afterwards, unprivileged: python3 -I -B %s verify %s' % (Path(__file__).resolve(), args_line),
              '  and, as you, `sudo -n -u %s %s version` (must print the digests above).' % (inputs.qa_name, layout.helper),
              '  Codex smoke test (the delegation refuses the pinned codex on purpose, so use your own password-gated sudo):',
              '    cd / && sudo -u %s %s --version' % (inputs.qa_name, layout.codex),
              '  Unverified until the owner pilot: regex arguments and digest pinning under a real sudo, setsid and signal relay.', '',
              'Rollback (root; removes the drop-in FIRST, then the files; safe to repeat):',
              '  sudo /usr/bin/python3 -I -B %s rollback --qa-name %s --qa-uid %d' % (copy, inputs.qa_name, inputs.qa_uid), '',
              'Eliminated by this install: typing `sudo -v` before every run, launching from that Terminal, the sudo keepalive.',
              'Still yours: the copy and the one root command; reinstalling when qa-side.py or the pinned Codex changes; the QA user\'s Codex',
              '  device login (qa-identity-setup.py prints it, followed by chmod 700 ~/.codex and a --check re-run); rechecking after a macOS',
              '  major upgrade.']
    return '\n'.join(lines) + '\n'


# ------------------------------------------------------------------ apply
def need_root(layout, ops):
    if ops.geteuid() != layout.root_uid:
        raise Stop('this mode must run as root through your own sudo, from the root-owned copy: sudo /usr/bin/python3 -I -B %s/qa-hop-install.py ...' % INSTALL_COPY)


def require_trusted_bundle(bundle, layout, only=None):
    problems = bundle.trust_problems(layout, only)
    if problems:
        raise Stop('refusing to run as root from code a less-privileged user could change (copy the four scripts to a root-owned '
                   'directory first): ' + '; '.join(problems[:4]))


def lib_dir(layout, ops, bundle, create):
    """/Library/cae-qa: every existing component root-owned, not writable by others, not a link."""
    side = bundle.module('qa-side')
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


def sudoers_real(layout, must_exist=True):
    """The drop-in's path with /etc resolved once (macOS links /etc to private/etc): the real directory must be root-owned,
    not writable by others and link-free. Nothing is written through a path that could be redirected."""
    directory = os.path.dirname(layout.sudoers)
    if not os.path.isdir(directory):
        if must_exist:
            raise Stop('%s does not exist' % directory)
        return None
    real = os.path.realpath(directory)
    if real != directory and not (directory.startswith('/etc/') and real == '/private' + directory):
        raise Stop('%s resolves to %s, which is not the expected location; refusing' % (directory, real))
    problems = chain_problems(real, layout.root_uid, layout.anchor, directory=True)
    if problems:
        raise Stop('the sudoers.d directory is not trustworthy: ' + '; '.join(problems[:3]))
    return os.path.join(real, os.path.basename(layout.sudoers))


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


def remove_dropin(path, why):
    """Remove the grant. If that itself fails the grant is still live: say so loudly with the exact manual command."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    except OSError as error:
        raise Stop('%s. THE DROP-IN COULD NOT BE REMOVED (%s) AND IS STILL ACTIVE: run `sudo rm -f %s` NOW' % (
            why, type(error).__name__, path))


def apply(inputs, layout, ops, say, bundle):
    need_root(layout, ops)
    if ops.caller_uid() != inputs.producer_uid:
        raise Stop('the invoking uid does not match the producer uid; run this through your own sudo')
    files, dropin, helper, config = planned(inputs, layout, bundle)
    dropin_path = sudoers_real(layout)
    lib_dir(layout, ops, bundle, create=True)
    install_file(layout, ops, layout.helper, 0o555, data=helper, expect_sha=bundle.helper_sha)
    install_file(layout, ops, layout.config, 0o444, data=config)
    install_file(layout, ops, layout.codex, 0o555, source=inputs.codex_source, expect_sha=inputs.codex_sha256)
    say('installed %s, %s, %s' % (layout.helper, layout.config, layout.codex))
    # Last: the grant itself. Candidate beside the destination under a name sudo ignores, compared, validated, then renamed.
    candidate = os.path.join(os.path.dirname(dropin_path), '.cae-qa.new')
    install_file(layout, ops, candidate, 0o440, data=dropin.encode())
    written_bytes = read_regular(candidate)
    findings = sudoers_findings(written_bytes.decode('utf-8', 'replace'), dropin)
    if written_bytes != dropin.encode():          # exact bytes, not only the lines: a stray byte or line ending is a finding
        findings.append('the written bytes differ from the rendered text')
    ok, out = visudo_check(layout, ops, candidate) if not findings else (False, 'the written text differs from the rendered one')
    if not ok:
        os.unlink(candidate)
        raise Stop('visudo rejected the drop-in or it differed from the plan; it was not installed (%s)' % (
            '; '.join(findings[:2]) or out.strip()[-200:]))
    os.rename(candidate, dropin_path)
    code, out = ops.run([layout.visudo, '-c'])
    if code != 0:
        remove_dropin(dropin_path, 'visudo -c failed on the live configuration')
        raise Stop('visudo -c failed on the live configuration; the drop-in was removed again (%s)' % out.strip()[-200:])
    say('installed %s (validated with visudo before and after)' % dropin_path)
    record, problems = verify_installed(inputs, layout, bundle, producer_uid=inputs.producer_uid)
    if problems:
        remove_dropin(dropin_path, 'post-install verification failed')
        raise Stop('post-install verification failed (%s); the drop-in was removed again (fail closed); run rollback to remove the rest' % '; '.join(problems[:3]))
    say('verified: helper %s, codex %s' % (record['helper_sha256'], record['codex_sha256']))
    say('Next, as yourself (not root): verify, then `sudo -n -u %s %s version`; Codex smoke test: `cd / && sudo -u %s %s --version`.' % (
        inputs.qa_name, layout.helper, inputs.qa_name, layout.codex))


# ------------------------------------------------------------------ verify / rollback
def verify_installed(inputs, layout, bundle, producer_uid=None):
    """The producer-side check qa-handoff --unattended runs before every run, from the same code."""
    handoff = bundle.module('qa-handoff')
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


def grant_paths(layout):
    """([drop-in, candidate], warning). The grant must be removable even when sudoers.d is not trustworthy, so when the strict
    resolution refuses, the fixed constant paths are used best-effort (a link at the directory itself is never followed)."""
    try:
        real = sudoers_real(layout, must_exist=False)
    except Stop as error:
        directory = os.path.dirname(layout.sudoers)
        if os.path.islink(directory):
            return [], 'LINK'
        real = os.path.join(directory, os.path.basename(layout.sudoers))
        return [real, os.path.join(directory, '.cae-qa.new')], str(error)
    if real is None:
        return [], None
    return [real, os.path.join(os.path.dirname(real), '.cae-qa.new')], None


def is_ours_dropin(path):
    """True only for a regular file (never a link) whose first line is the marker our renderer writes."""
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_size > 64 << 10:
            return False
        return read_regular(path, 64 << 10).decode('utf-8', 'replace').split('\n', 1)[0] == HEAD
    except (OSError, Stop):
        return False


def rollback(layout, ops, say):
    """Remove the grant first, then the files. Never raises before trying everything: a failure to remove is collected, printed
    with the exact manual command, and makes the exit non-zero."""
    need_root(layout, ops)
    removed, failed = [], []
    grant, warning = grant_paths(layout)
    if warning == 'LINK':
        directory = os.path.dirname(layout.sudoers)
        target = os.path.join(os.path.realpath(directory), os.path.basename(layout.sudoers))
        say('THE PRODUCER MAY STILL HAVE THE GRANT: %s is a link, so nothing was touched there; remove %s manually with: sudo rm -f %s '
            '(inspect the link target first)' % (directory, target, target))
        failed.append(target)
    elif warning:
        say('WARNING: %s. The drop-in is being removed at its fixed paths anyway; inspect %s afterwards.' % (
            warning, os.path.dirname(layout.sudoers)))
    for path in (*grant, layout.helper, layout.config, layout.codex,
                 *(os.path.join(layout.lib, '.%s.new' % n) for n in ('qa-side', 'qa-side.json', 'codex'))):
        if not os.path.lexists(path):
            continue
        st = os.lstat(path)
        if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
            say('%s is a directory, not one of our files; refusing to remove it' % path)
            failed.append(path)
            continue
        if path in grant and not is_ours_dropin(path):
            say('WARNING: %s is not a regular file carrying this installer\'s first-line marker, so it was NOT removed (it may be '
                'someone else\'s, and if it grants anything it is still active). Inspect it, then remove it by hand if it is yours.' % path)
            failed.append(path)
            continue
        try:
            os.unlink(path)               # a link is removed itself, never followed
        except OSError as error:
            say('COULD NOT REMOVE %s (%s)%s' % (path, type(error).__name__, ' - THE PRODUCER STILL HAS THE GRANT' if path in grant else ''))
            failed.append(path)
            continue
        removed.append(path)
        if path == (grant[0] if grant else None):
            say('removed %s (the producer has no grant now)' % path)
    if os.path.isdir(layout.lib) and not os.path.islink(layout.lib):
        if not os.listdir(layout.lib):
            os.rmdir(layout.lib)
            removed.append(layout.lib)
        else:
            say('left %s in place: it holds files this installer did not create' % layout.lib)
    say('removed: ' + (', '.join(removed) if removed else 'nothing (already rolled back)'))
    if failed:
        raise Stop('rollback incomplete. Run by hand, in this order: %s' % '; '.join('sudo rm -f %s' % p for p in failed))
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
            p.add_argument('--codex-source', required=True, help='absolute path of the Codex Mach-O binary to pin (no default, no download)')
            p.add_argument('--codex-sha256', required=True, help='its SHA-256: the explicit pin')
        if name == 'apply':
            p.add_argument('--bundle-sha256', required=True, help='combined digest of the four scripts, from `plan`')
            p.add_argument('--helper-sha256', required=True, help='digest of qa-side.py, from `plan`')
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
        bundle = Bundle(layout.bundle_dir)
        if mode in ('apply', 'rollback'):
            need_root(layout, ops)
            require_trusted_bundle(bundle, layout, only=None if mode == 'apply' else ('qa-hop-install.py',))
        if mode == 'apply':
            if not (HEX64.fullmatch(args.bundle_sha256) and HEX64.fullmatch(args.helper_sha256)):
                raise Stop('--bundle-sha256 and --helper-sha256 must be 64 lower-case hex characters (copy them from `plan`)')
            if args.bundle_sha256 != bundle.combined or args.helper_sha256 != bundle.helper_sha:
                raise Stop('the scripts about to run or be installed do not match the digests you approved (bundle %s, helper %s); '
                           'run plan again and compare with the reviewed PR head' % (bundle.combined, bundle.helper_sha))
        if mode == 'rollback':
            rollback(layout, ops, say)
            return 0
        inputs = resolve_inputs(args, ops, bundle)
        if mode == 'plan':
            say(plan_text(inputs, layout, ops, args_line(args), bundle).rstrip('\n'))
        elif mode == 'apply':
            apply(inputs, layout, ops, say, bundle)
        elif mode == 'verify':
            record, problems = verify_installed(inputs, layout, bundle)
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
