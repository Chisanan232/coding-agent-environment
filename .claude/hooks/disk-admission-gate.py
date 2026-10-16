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


def split_commands(command):
    """Split a shell line into simple commands (token lists); ValueError if unparsable."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
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
