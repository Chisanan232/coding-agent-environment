#!/usr/bin/env python3
"""PreToolUse(Bash) disk-admission gate, SOURCE ONLY (SPE-109).

Deny-only: it never rewrites a command (rtk rewrites separately) and never
emits updatedInput. It is not installed or wired by this repository; an
operator must add it to settings.json, and until then it protects nothing.
Real-engine behavior (the host actually invoking this hook) is NOT claimed.

A Bash command that looks like a heavy build is checked with
`scripts/disk-admission.py check --weight heavy`. Mode file
~/.cache/coding-agent-environment/admission/mode holds warn or enforce; an
absent or invalid file means warn. Only an operator edits it.

  warn     allow, and add a PreToolUse additionalContext warning when
           admission would refuse (or could not be evaluated).
  enforce  exit 2 with a message when admission refuses, and also when
           admission cannot be evaluated (fail closed). Only enforce fails closed.

Enforce is a guardrail, not a security boundary: the governed agent runs as the
same user as the operator and could edit the mode file. A Bash command that
appears to write, move or remove under the admission directory is flagged
tamper-suspect (denied in enforce, advisory in warn); that detection is
best-effort. Operator hardening: chmod 0444 and chflags uchg on the mode file.

A failure before the command is classified allows it: denying every Bash call
because of a hook bug would be worse than the gap.

Classification is token-aware: only the command word of each simple command
counts, so quoted text, echo, grep and `git commit -m 'cargo build'` are not
heavy. Env prefixes, wrappers (env, time, rtk, ...) and `cd x && cargo ...`
are handled. `bash -c "cargo build"` is treated as heavy but marked
'best-effort'.

UNSUPPORTED blind spots (not detected): make, just, `npm run <script>`,
direct binaries (a compiled tool that itself builds), a user's own `!` shell
commands, IDE background analysis (flycheck, rust-analyzer), and any command
hidden behind variable expansion, aliases, scripts or xargs.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path
import pwd
import shlex
import stat
import subprocess
import sys

OPERATORS = {';', '&&', '||', '|', '&', '(', ')', '|&', ';;', '\n'}
WRAPPERS = {'env', 'time', 'command', 'nohup', 'nice', 'exec', 'rtk', 'sudo', 'timeout', 'watch'}
# Wrapper options that consume the next token.
WRAPPER_VALUE_OPTIONS = {
    'nice': ('-n', '--adjustment'), 'env': ('-u', '-C'), 'exec': ('-a',), 'timeout': ('-s', '-k'),
    'watch': ('-n', '--interval'),
    'sudo': ('-u', '-g', '-h', '-p', '-C', '-D', '-R', '-T', '-U', '--user', '--group'),
}
SHELLS = {'bash', 'sh', 'zsh', 'dash'}
# Control-flow words that precede a command in a compound statement.
KEYWORDS = {'{', '!', 'if', 'then', 'else', 'elif', 'do', 'while', 'until'}
CARGO_HEAVY = {'build', 'b', 'test', 't', 'check', 'c', 'doc', 'clippy', 'nextest', 'bench', 'rustdoc',
               'run', 'r', 'install', 'rustc'}
CARGO_VALUE_OPTIONS = {'-C', '--config', '-Z', '--color', '--manifest-path', '--target-dir'}
NODE_HEAVY = {'install', 'i', 'ci', 'build', 'test', 't'}


def _heredoc_word(text, index):
    """Parse `<<[-]['"]WORD['"]` at index (just after `<<`); returns (word, strip_tabs, end)."""
    strip = text.startswith('-', index)
    index += strip
    while index < len(text) and text[index] in ' \t':
        index += 1
    quote = text[index] if index < len(text) and text[index] in '\'"' else ''
    index += bool(quote)
    start = index
    while index < len(text) and (text[index] not in ' \t\r\n;&|<>()' if not quote else text[index] != quote):
        index += 1
    word = text[start:index]
    return word, strip, index + bool(quote)


def _normalize(command):
    """Rewrite unquoted newlines as `;`, drop comments and heredoc bodies (data, not commands)."""
    out, pending, index, quote = [], [], 0, ''
    while index < len(command):
        char = command[index]
        if quote:
            out.append(char)
            if char == '\\' and quote == '"' and index + 1 < len(command):
                out.append(command[index + 1])
                index += 1
            elif char == quote:
                quote = ''
        elif char in '\'"':
            quote = char
            out.append(char)
        elif char == '\\' and index + 1 < len(command):
            following = command[index + 1]
            if following == '\n':
                out.append(' ')
            elif following == '\r' and command[index + 2:index + 3] == '\n':
                out.append(' ')
                index += 1
            else:
                out.append(char + following)
            index += 1
        elif char == '#' and (not out or out[-1] in ' \t\n;&|(){}'):
            while index < len(command) and command[index] != '\n':
                index += 1
            continue
        elif char == '`':
            out.append(' ; ')
        elif command.startswith('<<', index) and not command.startswith('<<<', index):
            word, strip, end = _heredoc_word(command, index + 2)
            if word:
                pending.append((word, strip))
            out.append(' << ' + shlex.quote(word) + ' ')
            index = end
            continue
        elif char == '\r' and command[index + 1:index + 2] == '\n':
            pass
        elif char == '\n':
            out.append(' ; ')
            index += 1
            for word, strip in pending:
                while index < len(command):
                    end = command.find('\n', index)
                    end = len(command) if end < 0 else end
                    line = command[index:end].rstrip('\r')
                    index = min(end + 1, len(command))
                    if (line.lstrip('\t') if strip else line) == word:
                        break
            pending = []
            continue
        else:
            out.append(char)
        index += 1
    return ''.join(out)


def split_commands(command):
    """Split a shell line into simple commands (token lists); ValueError if unparsable."""
    lexer = shlex.shlex(_normalize(command), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ''
    commands, current = [], []
    for token in lexer:
        if token in OPERATORS:
            if current:
                commands.append(current)
            current = []
        else:
            current.append(token)
    if current:
        commands.append(current)
    return commands


def _is_assignment(token):
    name, equals, _ = token.partition('=')
    return bool(equals) and name.replace('_', 'a').isalnum() and not name[0].isdigit()


def _command_words(tokens):
    """Env assignments, keywords and wrappers stripped; returns (assignments, words)."""
    assignments, index = [], 0
    while index < len(tokens):
        token = tokens[index]
        if token in KEYWORDS:
            index += 1
        elif _is_assignment(token):
            assignments.append(token)
            index += 1
        elif os.path.basename(token) in WRAPPERS:
            wrapper = os.path.basename(token)
            index += 1
            values = WRAPPER_VALUE_OPTIONS.get(wrapper, ())
            while index < len(tokens) and tokens[index].startswith('-'):
                if tokens[index] == '--':
                    index += 1
                    break
                index += 2 if tokens[index] in values else 1
            if wrapper == 'timeout':
                index += 1
            elif wrapper == 'rtk' and tokens[index:index + 1] == ['proxy']:
                index += 1
        else:
            break
    return assignments, tokens[index:]


def _cargo_subcommand(args):
    index = 0
    while index < len(args):
        token = args[index]
        if token.startswith('+'):
            index += 1
        elif token in CARGO_VALUE_OPTIONS:
            index += 2
        elif token.startswith('-'):
            index += 1
        else:
            return token
    return None


def _heavy_kind(words):
    """Name of the heavy tool a command word list runs, else None."""
    if not words:
        return None
    program, args = os.path.basename(words[0]), words[1:]
    if program == 'cargo':
        return 'cargo' if _cargo_subcommand(args) in CARGO_HEAVY else None
    if program == 'maturin':
        return 'maturin' if args[:1] and args[0] in ('build', 'develop') else None
    if program in ('npm', 'pnpm', 'yarn'):
        if program == 'yarn' and not args:
            return 'yarn'
        return program if args[:1] and args[0] in NODE_HEAVY else None
    if program == 'uv':
        if args[:1] == ['sync'] or args[:2] == ['pip', 'install']:
            return 'uv'
        return None
    if program in ('pip', 'pip3'):
        return 'pip' if args[:1] == ['install'] else None
    if program.startswith('python') and args[:3] == ['-m', 'pip', 'install']:
        return 'pip'
    if program == 'docker':
        if args[:1] == ['build'] or args[:2] in (['buildx', 'build'], ['buildx', 'bake'], ['compose', 'build']):
            return 'docker'
        return None
    if program == 'xcodebuild':
        return 'xcodebuild'
    return None


ADMISSION_SCRIPT = 'disk-admission.py'
INTERPRETER_VALUE_OPTIONS = {'-W', '-X', '-Q'}
# `uv run` options that take a separate value; an unlisted option leaves the wrapper unrecognised.
UV_VALUE_OPTIONS = {'--python', '-p', '--with', '--with-requirements', '--with-editable', '--project',
                    '--directory', '--env-file', '--package', '--extra', '--group', '--only-group'}


def _admission_run_inner(words):
    """Words of the job inside `[python|uv run [python]] disk-admission.py run ... -- JOB...`, else None.

    The wrapper reserves and execs the job, so it would otherwise hide cargo/uv/... from this hook. Only the
    shape is recognised: the file name is matched but its path and contents are never trusted, and the wrapper
    is never treated as pre-admitted, so the inner command is classified and admitted exactly as if it had
    been typed directly. A copy or symlink under another name is not recognised.
    """
    if not words:
        return None
    program = os.path.basename(words[0])
    index = 0
    if program != ADMISSION_SCRIPT:
        value_options = INTERPRETER_VALUE_OPTIONS
        if program == 'uv' and words[1:2] == ['run']:
            index = 2
            value_options = UV_VALUE_OPTIONS
        elif program.startswith('python'):
            index = 1
        else:
            return None
        while index < len(words) and os.path.basename(words[index]) != ADMISSION_SCRIPT:
            word = words[index]
            if word == '--':
                # `--` ends the interpreter's options, so only the script itself may follow.
                following = words[index + 1:index + 2]
                if not following or os.path.basename(following[0]) != ADMISSION_SCRIPT:
                    return None
                index += 1
                break
            if not (word.startswith('-') or os.path.basename(word).startswith('python')
                                    or (index > 0 and words[index - 1] in value_options)):
                return None
            index += 1
        if index >= len(words):
            return None
    after = index + 1
    if words[after:after + 1] == ['--']:  # argparse accepts a separator before the subcommand
        after += 1
    if words[after:after + 1] != ['run']:
        return None
    rest = words[after + 1:]
    if '--' not in rest:
        return None
    inner = rest[rest.index('--') + 1:]
    while inner[:1] == ['--']:
        inner = inner[1:]
    return inner or None


def _shell_script(words):
    """The string run by `bash -c` (flag clusters like -euo pipefail -c allowed) or `eval`, else None."""
    if not words:
        return None
    program = os.path.basename(words[0])
    if program == 'eval':
        return ' '.join(words[1:]) or None
    if program not in SHELLS:
        return None
    index = 1
    while index < len(words):
        token = words[index]
        if token in ('-o', '+o', '-O', '+O'):
            index += 2
        elif token.startswith('-') and not token.startswith('--') and len(token) > 1:
            if token.endswith('c'):
                return words[index + 1] if index + 1 < len(words) else None
            index += 2 if token.endswith('o') else 1
        else:
            return None
    return None


HEAVY_WORD_RE = re.compile(r'(?<![\w./-])(cargo|rustc|rustdoc|nextest|maturin|npm|pnpm|yarn|uv|docker|xcodebuild)(?![\w-])')
MAX_PARSE_CHARS = 256 * 1024
UNCERTAIN = ('unparsable', 'oversize')


def _uncertain(command, confidence):
    """A command that cannot be tokenized: heavy when it names a heavy tool anywhere."""
    found = HEAVY_WORD_RE.search(command)
    return {'heavy': bool(found), 'confidence': confidence, 'kind': found.group(1) if found else None,
            'assignments': [], 'words': []}


def classify(command, _depth=0):
    """Return {'heavy', 'confidence', 'kind', 'assignments', 'words'} for a Bash command line."""
    verdict = {'heavy': False, 'confidence': 'exact', 'kind': None, 'assignments': [], 'words': []}
    if len(command) > MAX_PARSE_CHARS:
        return _uncertain(command, 'oversize')
    try:
        simple_commands = split_commands(command)
    except ValueError:
        return _uncertain(command, 'unparsable')
    for tokens in simple_commands:
        assignments, words = _command_words(tokens)
        kind = _heavy_kind(words)
        if kind:
            return {'heavy': True, 'confidence': 'exact', 'kind': kind, 'assignments': assignments, 'words': words}
        wrapped = _admission_run_inner(words) if _depth < 3 else None
        if wrapped is not None:
            inner_assignments, inner_words = _command_words(wrapped)
            inner_kind = _heavy_kind(inner_words)
            if inner_kind:
                return {'heavy': True, 'confidence': 'exact', 'kind': inner_kind,
                        'assignments': assignments + inner_assignments, 'words': inner_words}
            inner = classify(' '.join(shlex.quote(w) for w in wrapped), _depth + 1)
            if inner['heavy']:
                return dict(inner, confidence='best-effort')
        script = _shell_script(words) if _depth < 3 else None
        if script is not None:
            inner = classify(script, _depth + 1)
            if inner['heavy']:
                return dict(inner, confidence='best-effort')
    return verdict


def account_home():
    # The account database, not $HOME: the environment is caller-controlled.
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


SCRATCH_ROOTS = ('/private/tmp', '/tmp', '/private/var/folders', '/var/folders')


def target_dir(verdict):
    """Explicit cargo target directory of a classified command, else None."""
    if verdict['kind'] != 'cargo':
        return None
    for assignment in verdict['assignments']:
        if assignment.startswith('CARGO_TARGET_DIR='):
            return assignment.partition('=')[2]
    words = verdict['words']
    for index, word in enumerate(words):
        if word == '--target-dir' and index + 1 < len(words):
            return words[index + 1]
        if word.startswith('--target-dir='):
            return word.partition('=')[2]
    return None


def target_dir_advice(verdict, exists=os.path.exists):
    """Advice (never a denial) against a fresh cold-build target in scratch space."""
    chosen = target_dir(verdict)
    if not chosen:
        return None
    path = os.path.expanduser(chosen)
    shared = str(account_home() / '.cargo/shared-target')
    if not os.path.isabs(path) or path == shared or path.startswith(shared + '/') or exists(path):
        return None
    if path.startswith(tuple(root + '/' for root in SCRATCH_ROOTS)) or 'scratchpad' in Path(path).parts:
        return ('cargo target dir %s is new scratch space, so this is a cold build that duplicates build output; '
                'prefer the shared target %s' % (chosen, shared))
    return None


ADMISSION_RELATIVE = '.cache/coding-agent-environment/admission'
DENY_EXIT = 2
ADAPTER_TIMEOUT_S = 30


def default_admission_dir():
    return account_home() / ADMISSION_RELATIVE


def parse_admission_dir(argv):
    """Value of --admission-dir from the hook command line, else None.

    The flag comes from the command registered in settings.json, which only the
    operator edits; the agent's Bash commands cannot change it.
    """
    for index, arg in enumerate(argv):
        value = arg.partition('=')[2] if arg.startswith('--admission-dir=') else (
            argv[index + 1] if arg == '--admission-dir' and index + 1 < len(argv) else None)
        if value is not None:
            return Path(value) if os.path.isabs(value) else None
    return None


def admission_dir_config(argv):
    """(directory or None, error or None) from the hook command line.

    Any argument other than a valid absolute --admission-dir is a configuration
    error: silently falling back to the real default directory would let a typo
    read the real mode file while an operator believes a sandbox is in use.
    """
    directory, error, index = None, None, 0
    while index < len(argv):
        arg = argv[index]
        value = None
        if arg.startswith('--admission-dir='):
            value = arg.partition('=')[2]
        elif arg == '--admission-dir':
            value = argv[index + 1] if index + 1 < len(argv) else ''
            index += 1
        else:
            error = 'unrecognised hook argument %r' % arg
        if value is not None:
            if os.path.isabs(value):
                directory = Path(value)
            else:
                error = '--admission-dir %r is not an absolute path' % value
        index += 1
    return directory, error


def read_mode(path=None):
    """'enforce' only when the operator's mode file says exactly so; anything else is warn."""
    path = path or default_admission_dir() / 'mode'
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, 'rb') as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return 'warn'
            value = handle.read(64).decode('ascii', 'replace').strip()
    except OSError:
        return 'warn'
    return 'enforce' if value == 'enforce' else 'warn'


def adapter_path():
    """The admission adapter next to this checkout, else the managed runtime copy."""
    candidates = (Path(__file__).resolve().parents[2] / 'scripts/disk-admission.py',
                  account_home() / '.local/lib/coding-agent-environment/scripts/disk-admission.py')
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _run_adapter_in_process(adapter, admission_dir):
    """Sandbox use only: the adapter has no state-path flag, so call its main() directly."""
    import contextlib
    import importlib.util
    import io
    spec = importlib.util.spec_from_file_location('disk_admission_adapter', adapter)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        code = module.run(['check', '--weight', 'heavy'], state_path=admission_dir / 'state.json')
    return code, captured.getvalue().strip()[:300]


def run_adapter(admission_dir=None):
    """(exit code, one-line text) of `disk-admission.py check --weight heavy`; code None on failure."""
    adapter = adapter_path()
    if adapter is None:
        return None, 'disk-admission.py not found'
    if admission_dir is not None and admission_dir != default_admission_dir():
        try:
            return _run_adapter_in_process(adapter, admission_dir)
        except Exception as exc:
            return None, str(exc)
    try:
        done = subprocess.run([sys.executable, str(adapter), 'check', '--weight', 'heavy'], capture_output=True,
                              text=True, timeout=ADAPTER_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    return done.returncode, (done.stdout.strip() or done.stderr.strip())[:300]


AUDIT_ENABLED = True
AUDIT_NAME = 'gate-audit.jsonl'
AUDIT_MAX_BYTES = 1024 * 1024
ADAPTER_LINE_RE = re.compile(r'tier=(\w+) free=\S+ GiB bytes=(\d+)')


def _event(events, classification, verdict, mode, decision, text=''):
    """Record one audit event; never holds the command text."""
    if events is None:
        return
    found = ADAPTER_LINE_RE.search(text or '')
    events.append({'classification': classification, 'tool': verdict['kind'] if verdict else None,
                   'confidence': verdict['confidence'] if verdict else None, 'mode': mode, 'decision': decision,
                   'tier': found.group(1) if found else None,
                   'free_bytes': int(found.group(2)) if found else None})


def _decide_heavy(command, mode, runner, exists, events=None):
    verdict = classify(command)
    if not verdict['heavy']:
        return 0, '', ''
    label = '%s (%s classification)' % (verdict['kind'], verdict['confidence'])
    if verdict['confidence'] in UNCERTAIN and mode != 'enforce':
        # Warn mode never blocks and cannot tell what this command runs.
        note = 'disk-admission-gate (mode %s, advice): command is %s and names %s; it may be a heavy build' % (
            mode, verdict['confidence'], verdict['kind'])
        _event(events, 'heavy', verdict, mode, 'warn')
        return 0, json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'additionalContext': note}}), ''
    notes = []
    text = ''
    try:
        code, text = runner()
        if code == 77:
            notes.append('disk admission refuses heavy command %s: %s' % (label, text))
        elif code != 0:
            notes.append('disk admission could not be evaluated for %s: %s' % (label, text))
        denied = bool(notes)
    except Exception as exc:
        notes.append('disk admission could not be evaluated for %s: %s' % (label, exc))
        denied = True
    advice = target_dir_advice(verdict, exists)
    if advice:
        notes.append(advice)
    if denied and mode == 'enforce':
        _event(events, 'heavy', verdict, mode, 'deny', text)
        return DENY_EXIT, '', 'BLOCKED by disk-admission-gate (mode enforce): ' + '; '.join(notes)
    _event(events, 'heavy', verdict, mode, 'warn' if notes else 'allow', text)
    if not notes:
        return 0, '', ''
    prefix = 'disk-admission-gate (mode %s, %s): ' % (mode, 'would block' if denied else 'advice')
    return 0, json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse',
                                                   'additionalContext': prefix + '; '.join(notes)}}), ''


STATE_NEEDLE = 'coding-agent-environment/admission'
REDIRECTS = {'>', '>>', '>|', '&>', '&>>'}
WRITERS = {'tee', 'mv', 'rm', 'cp', 'ln', 'truncate', 'dd', 'chmod', 'chflags', 'install', 'touch', 'rmdir',
           'unlink', 'rsync', 'perl', 'ruby', 'node', 'awk', 'gawk'}
TAMPER_NOTE = ('command appears to modify the disk-admission state directory (mode file, state, snapshot or '
               'reservations); only the operator should edit it')


# Default-suspect: a line that mentions the state directory is cleared only when every simple command on it
# is provably read-only. Unknown programs, interpreters, cwd changes, heredocs and process substitution
# stay suspect, so narrowing the old blanket rule cannot open a write route.
# Only programs with no file-writing or command-running option: sort -o, uniq IN OUT, xxd -r, date -s,
# less/rg --pre, sed w and awk system() are deliberately absent.
READ_ONLY = {'cat', 'ls', 'head', 'tail', 'grep', 'egrep', 'fgrep', 'wc', 'stat', 'jq', 'diff', 'cmp', 'shasum',
             'sha256sum', 'md5', 'md5sum', 'strings', 'cut', 'tr', 'du', 'df', 'realpath', 'readlink', 'basename',
             'dirname', 'echo', 'printf', 'true', 'false', ':', 'test', '[', 'pwd'}
FIND_WRITES = ('-delete', '-exec', '-execdir', '-ok', '-okdir', '-fprint', '-fprint0', '-fprintf', '-fls')
# Programs that change what later commands act on (cwd) or run what they are fed: suspect anywhere on the line.
CWD_CHANGERS = {'cd', 'pushd', 'popd', 'chdir', 'xargs', 'parallel', 'source', '.', 'builtin'}
DYNAMIC = re.compile(r'[$`*?\[{]')
FD_DUP = re.compile(r'\A(\d+|-)\Z')


SAFE_SINKS = re.compile(r'\A(/dev/(null|stdout|stderr)|(/private)?/tmp/[^\s]*|/var/folders/[^\s]*)\Z')


def _redirects_ok(tokens, needles):
    """Every output redirect is a descriptor duplication or a normalized path in a scratch area."""
    for index, token in enumerate(tokens):
        if token.startswith(('>', '&>')) or token == '<>':
            target = tokens[index + 1] if index + 1 < len(tokens) else '$'
            if token in ('>&', '<&') and FD_DUP.match(target):
                continue
            if not SAFE_SINKS.match(target) or DYNAMIC.search(target) or os.path.normpath(target) != target \
                    or _mentions(target, needles):
                return False
    return True


def _read_only(tokens, words, needles):
    program = os.path.basename(words[0])
    arguments = words[1:]
    if '/' in words[0] and not TRUSTED_BIN.match(words[0]):
        return False  # a planted binary named like a read-only tool
    if not _redirects_ok(tokens, needles):
        return False
    if program == 'find':
        return not any(a.startswith(FIND_WRITES) for a in arguments)
    if program == 'sed':
        # Only a plain substitution: no -i, no -f, no w/e flags or commands that write or run something.
        flags = [a for a in arguments if a.startswith('-')]
        rest = [a for a in arguments if not a.startswith('-')]
        return bool(rest) and all(f in ('-n', '-E', '-r') for f in flags) and \
            re.fullmatch(r's/[^/\\]*/[^/\\]*/[gpiI0-9]*', rest[0]) is not None
    return program in READ_ONLY


def _mentions(command, needles):
    """Does the line name the state directory, its parent, or a spelling of either (/./, //, globs)?

    Path-shaped on purpose: a worktree called coding-agent-environment-SPE-1 or a file called disk-admission.py
    must not match, while rm -rf <parent>, <parent>/adm*, <parent>/./admission and coding-agent-env*/ must.
    """
    for needle in needles:
        if needle in command:
            return True
        parent, _, child = needle.rpartition('/')
        if not parent:
            continue
        glob = r'[*?\[]'
        if '/' not in parent:
            # A bare directory name only counts under a dot-directory (~/.cache, ~/.claude, ~/.config): the same
            # name is also an ordinary checkout, and `git -C .../coding-agent-environment status` is fine.
            rooted = (r'(?:\.cache|\.claude|\.config)["\']?/+(?:\.{1,2}/+|[^\s/]+/+\.\./+)*["\']?' + re.escape(parent))
            if re.search(rooted + r'/+\S', command) or re.search(rooted + r'(?![\w.+-])', command) or \
                    re.search(r'(?:\.cache|\.claude|\.config)/' + re.escape(parent[:12]) + r'[^\s/]*' + glob + r'[^\s/]*', command):
                return True
        else:
            # A custom (sandbox) directory: its own name (also via ../ or /./ or a glob) and, when it is at
            # least two levels deep, its parent too (deleting the parent deletes the state). A one-level
            # parent such as /tmp is never a mention, or every scratch file would be.
            parent_re = re.escape(parent)
            if re.search(parent_re + r'/+(?:\.{1,2}/+|[^\s/]+/+\.\./+)*(' + re.escape(child) + r'(?![\w.+-])|[^\s/]*' + glob + ')', command):
                return True
            if parent.strip('/').count('/') >= 1 and re.search(parent_re + r'(?![\w.+-])', command):
                return True
        if re.search(glob + r'[^\s/]*/+(\.{1,2}/+)*' + re.escape(child) + r'(?![\w.+-])', command):
            return True
    return False


TRUSTED_BIN = re.compile(r'\A(/bin|/usr/bin|/usr/local/bin|/opt/homebrew/bin)/[^/]+\Z')


UNSAFE_ASSIGNMENTS = re.compile(r'\A(PATH|BASH_ENV|ENV|IFS|LD_\w+|DYLD_\w+)=')


def tamper_suspect(command, needles=(STATE_NEEDLE,)):
    """Best-effort: does this command write, move or remove under the admission state directory?

    Default-suspect. A line that mentions the directory is cleared only when every simple command on it is
    on a read-only allowlist and its output redirects go to descriptors or normalized scratch paths. Any other
    command on the line, cd, xargs, heredocs, command substitution, process substitution, backticks, function
    definitions, interpreters and unparsable lines are suspect, so a decoy mention cannot clear a writer.
    """
    if not _mentions(command, needles):
        return False
    if any(mark in command for mark in ('<<', '>(', '<(', '`', '$(', '${')):
        return True
    try:
        simple_commands = split_commands(command)
    except ValueError:
        return True
    for tokens in simple_commands:
        assignments, words = _command_words(tokens)
        if any(UNSAFE_ASSIGNMENTS.match(a) or _mentions(a, needles) for a in assignments):
            return True
        if not words:
            if not _redirects_ok(tokens, needles):
                return True
            continue
        if any(t == '()' or t.endswith('()') for t in tokens) or os.path.basename(words[0]) == 'function':
            return True
        program = os.path.basename(words[0])
        if program in CWD_CHANGERS:
            return True
        script = _shell_script(words)
        if script is not None:
            if tamper_suspect(script, needles):
                return True
            continue
        if not _read_only(tokens, words, needles):
            return True
    return False


def _directory_spellings(directory):
    """The configured directory as typed, resolved, and with macOS's /private alias added or removed."""
    spellings = {str(directory)}
    try:
        spellings.add(os.path.realpath(directory))
    except OSError:
        pass
    for text in list(spellings):
        if text.startswith('/private/'):
            spellings.add(text[len('/private'):])
        elif text.startswith(('/tmp/', '/var/', '/etc/')):
            spellings.add('/private' + text)
    return tuple(sorted(spellings))


def decide(command, mode, runner=run_adapter, exists=os.path.exists, needles=(STATE_NEEDLE,), events=None):
    """Return (exit code, stdout text, stderr text) for one Bash command."""
    if not tamper_suspect(command, needles):
        return _decide_heavy(command, mode, runner, exists, events)
    _event(events, 'tamper-suspect', None, mode, 'deny' if mode == 'enforce' else 'warn')
    if mode == 'enforce':
        return DENY_EXIT, '', 'BLOCKED by disk-admission-gate (mode enforce): tamper-suspect: ' + TAMPER_NOTE
    code, out, err = _decide_heavy(command, mode, runner, exists, events)
    note = 'disk-admission-gate (mode %s, advice): tamper-suspect: %s' % (mode, TAMPER_NOTE)
    if out:
        extra = json.loads(out)['hookSpecificOutput']['additionalContext']
        note += '; ' + extra
    return code, json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'additionalContext': note}}), err


def audit_append(directory, event, command, now=None):
    """Append one JSONL line; never raises and never records the command itself."""
    record = dict(event, ts=(now or datetime.now(timezone.utc)).strftime('%Y-%m-%dT%H:%M:%SZ'),
                  cmd_sha12=hashlib.sha256(command.encode('utf-8', 'replace')).hexdigest()[:12])
    line = (json.dumps(record, sort_keys=True) + '\n').encode()
    path = Path(directory) / AUDIT_NAME
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        try:
            if os.lstat(path).st_size + len(line) > AUDIT_MAX_BYTES:
                os.replace(path, str(path) + '.1')
        except FileNotFoundError:
            pass
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.write(descriptor, line)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def main(raw, stdout, stderr, mode_fn=None, runner=None, admission_dir=None, config_error=None):
    directory = admission_dir or default_admission_dir()
    custom = admission_dir is not None
    mode_fn = mode_fn or (lambda: read_mode(directory / 'mode'))
    runner = runner or (lambda: run_adapter(directory if custom else None))
    needles = (STATE_NEEDLE,) + (_directory_spellings(directory) if custom else ())
    try:
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8', 'replace')
        payload = json.loads(raw)
        command = payload['tool_input']['command']
        if payload.get('tool_name') != 'Bash' or not isinstance(command, str):
            return 0
        mode = mode_fn()
    except Exception:
        return 0
    try:
        events = []
        note = None
        if config_error:
            heavy = classify(command)['heavy']
            note = 'disk-admission-gate configuration error: %s; using the default admission directory' % config_error
            refuse = heavy and mode == 'enforce'
            events.append({'classification': 'config-error', 'tool': None, 'confidence': None, 'mode': mode,
                           'decision': 'deny' if refuse else 'warn', 'tier': None, 'free_bytes': None})
            if refuse:
                if AUDIT_ENABLED:
                    audit_append(directory, events[0], command)
                stderr.write('BLOCKED by disk-admission-gate (mode enforce): ' + note + '\n')
                return DENY_EXIT
        code, out, err = decide(command, mode, runner, needles=needles, events=events)
        if note:
            extra = json.loads(out)['hookSpecificOutput']['additionalContext'] if out else None
            out = json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse',
                                                       'additionalContext': note + ('; ' + extra if extra else '')}})
        if AUDIT_ENABLED:
            for event in events:
                audit_append(directory, event, command)
    except Exception as exc:
        if mode != 'enforce':
            return 0
        code, out, err = DENY_EXIT, '', 'BLOCKED by disk-admission-gate (mode enforce): internal error: %s' % exc
    if out:
        stdout.write(out + '\n')
    if err:
        stderr.write(err + '\n')
    return code


if __name__ == '__main__':
    _directory, _error = admission_dir_config(sys.argv[1:])
    raise SystemExit(main(sys.stdin.buffer.read(), sys.stdout, sys.stderr, admission_dir=_directory, config_error=_error))
