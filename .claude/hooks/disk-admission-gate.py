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
import json
import os
from pathlib import Path
import pwd
import shlex
import stat
import subprocess
import sys

OPERATORS = {';', '&&', '||', '|', '&', '(', ')', '|&', ';;', '\n'}
WRAPPERS = {'env', 'time', 'command', 'nohup', 'nice', 'exec', 'rtk', 'sudo'}
SHELLS = {'bash', 'sh', 'zsh', 'dash'}
CARGO_HEAVY = {'build', 'b', 'test', 't', 'check', 'c', 'doc', 'clippy', 'nextest', 'bench', 'rustdoc'}
CARGO_VALUE_OPTIONS = {'-C', '--config', '-Z', '--color'}
NODE_HEAVY = {'install', 'i', 'ci', 'build'}


def _heredoc_word(text, index):
    """Parse `<<[-]['"]WORD['"]` at index (just after `<<`); returns (word, strip_tabs, end)."""
    strip = text.startswith('-', index)
    index += strip
    while index < len(text) and text[index] in ' \t':
        index += 1
    quote = text[index] if index < len(text) and text[index] in '\'"' else ''
    index += bool(quote)
    start = index
    while index < len(text) and (text[index] not in ' \t\n;&|<>()' if not quote else text[index] != quote):
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
    """Env assignments and wrappers stripped; returns (assignments, words)."""
    assignments, index = [], 0
    while index < len(tokens):
        token = tokens[index]
        if _is_assignment(token):
            assignments.append(token)
        elif os.path.basename(token) in WRAPPERS:
            pass
        elif token.startswith('-') and index and os.path.basename(tokens[index - 1]) in WRAPPERS:
            pass
        else:
            break
        index += 1
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


def _shell_script(words):
    """The string given to `bash -c` (also -lc and similar), else None."""
    if not words or os.path.basename(words[0]) not in SHELLS:
        return None
    for index, token in enumerate(words[1:3], 1):
        if token.startswith('-') and not token.startswith('--') and token.endswith('c') and index + 1 < len(words):
            return words[index + 1]
    return None


def classify(command, _depth=0):
    """Return {'heavy', 'confidence', 'kind', 'assignments', 'words'} for a Bash command line."""
    verdict = {'heavy': False, 'confidence': 'exact', 'kind': None, 'assignments': [], 'words': []}
    try:
        simple_commands = split_commands(command)
    except ValueError:
        return dict(verdict, confidence='unparsable')
    for tokens in simple_commands:
        assignments, words = _command_words(tokens)
        kind = _heavy_kind(words)
        if kind:
            return {'heavy': True, 'confidence': 'exact', 'kind': kind, 'assignments': assignments, 'words': words}
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


MODE_RELATIVE = '.cache/coding-agent-environment/admission/mode'
DENY_EXIT = 2
ADAPTER_TIMEOUT_S = 30


def read_mode(path=None):
    """'enforce' only when the operator's mode file says exactly so; anything else is warn."""
    path = path or account_home() / MODE_RELATIVE
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


def run_adapter():
    """(exit code, one-line text) of `disk-admission.py check --weight heavy`; code None on failure."""
    adapter = adapter_path()
    if adapter is None:
        return None, 'disk-admission.py not found'
    try:
        done = subprocess.run([sys.executable, str(adapter), 'check', '--weight', 'heavy'], capture_output=True,
                              text=True, timeout=ADAPTER_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    return done.returncode, (done.stdout.strip() or done.stderr.strip())[:300]


def decide(command, mode, runner=run_adapter, exists=os.path.exists):
    """Return (exit code, stdout text, stderr text) for one Bash command."""
    verdict = classify(command)
    if not verdict['heavy']:
        return 0, '', ''
    label = '%s (%s classification)' % (verdict['kind'], verdict['confidence'])
    notes = []
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
        return DENY_EXIT, '', 'BLOCKED by disk-admission-gate (mode enforce): ' + '; '.join(notes)
    if not notes:
        return 0, '', ''
    prefix = 'disk-admission-gate (mode %s, %s): ' % (mode, 'would block' if denied else 'advice')
    return 0, json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse',
                                                   'additionalContext': prefix + '; '.join(notes)}}), ''


def main(raw, stdout, stderr, mode_fn=read_mode, runner=run_adapter):
    try:
        payload = json.loads(raw)
        command = payload['tool_input']['command']
        if payload.get('tool_name') != 'Bash' or not isinstance(command, str):
            return 0
        mode = mode_fn()
    except Exception:
        return 0
    try:
        code, out, err = decide(command, mode, runner)
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
    raise SystemExit(main(sys.stdin.read(), sys.stdout, sys.stderr))
