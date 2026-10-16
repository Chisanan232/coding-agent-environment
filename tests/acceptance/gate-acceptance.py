#!/usr/bin/env python3
"""Real-engine acceptance for the disk-admission gate hook. NOT run in CI.

Drives real `claude -p` sessions against a disposable fixture repo and a sandbox
admission directory (the hook is registered through --settings with
--admission-dir, so no live settings file and no real state file is touched).
The user's live hooks (for example `rtk hook claude`) stay active, which is the
point of case R1. Evidence per case: the model's one-word answer, a marker file
the command creates only if it really ran, and the sandbox audit lines.

Usage: gate-acceptance.py WORKDIR [--cases H1,H2,...] [--include-production-path]

WORKDIR must be new or a directory this script created earlier (it holds a sentinel file);
nothing else is ever deleted. P1 (--include-production-path) runs the hook WITHOUT
--admission-dir, through the production subprocess adapter. The adapter resolves the home
directory from the account database, not $HOME, so P1 cannot be sandboxed: it writes the real
state file and one audit line in the real default admission directory (read-only otherwise).
The admitted path (free space >= 100 GiB) is reported NOT_YET_VERIFIABLE while
the host is below 100 GiB; it is never faked.
"""
import hashlib
import json
import os
import pwd
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
GIB = 1024 ** 3


def load(name, relative):
    spec = spec_from_file_location(name, ROOT / relative)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = load('gate', '.claude/hooks/disk-admission-gate.py')
adapter = load('adapter', 'scripts/disk-admission.py')


SENTINEL = '.gate-acceptance-workdir'
OWNED_CHILDREN = ('sandbox-admission', 'fx', 'extra-settings.json', 'extra-settings-prod.json', 'result.json')


class RefusedWorkdir(Exception):
    pass


def claim_workdir(path):
    """A work directory this script created (it holds the sentinel), or a fresh one; never anything else.

    Refuses `/`, the home directory or any ancestor of it, any git working tree
    (including this repository), and an existing directory without the sentinel.
    """
    work = Path(path).expanduser().resolve()
    home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    if work == Path('/') or work == home or home.is_relative_to(work):
        raise RefusedWorkdir('refusing %s: it is, or contains, the home directory' % work)
    if ROOT.is_relative_to(work) or work.is_relative_to(ROOT):
        raise RefusedWorkdir('refusing %s: it overlaps this repository' % work)
    probe = work
    while not probe.exists():
        probe = probe.parent
    if subprocess.run(['git', '-C', str(probe), 'rev-parse', '--show-toplevel'], capture_output=True).returncode == 0:
        raise RefusedWorkdir('refusing %s: it is inside a git working tree' % work)
    if work.exists():
        if not (work / SENTINEL).is_file():
            raise RefusedWorkdir('refusing %s: it exists and was not created by this script (no %s)' % (work, SENTINEL))
        for name in OWNED_CHILDREN:
            child = work / name
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            elif child.exists() or child.is_symlink():
                child.unlink()
    else:
        work.mkdir(parents=True)
        (work / SENTINEL).write_text('created by gate-acceptance.py\n')
    return work


def prompt(command):
    return ('Run exactly this one Bash command and nothing else: %s . Reply with exactly one word first: EXECUTED if '
            'the command ran (even with no output or an error), BLOCKED if it was refused or denied by a hook. Do not '
            'retry or use any other tool.' % command)


class Harness:
    def __init__(self, work):
        self.work = claim_workdir(work)
        self.sandbox = self.work / 'sandbox-admission'
        self.fixture = self.work / 'fx/disposable'
        self.settings = self.work / 'extra-settings.json'
        self.results = []

    def prepare(self):
        self.sandbox.mkdir(parents=True, mode=0o700)
        self.fixture.mkdir(parents=True)
        subprocess.run(['git', 'init', '-q'], cwd=self.fixture, check=True)
        (self.fixture / 'README.md').write_text('disposable fixture\n')
        command = 'python3 %s --admission-dir %s' % (ROOT / '.claude/hooks/disk-admission-gate.py', self.sandbox)
        self.settings.write_text(json.dumps({'hooks': {'PreToolUse': [
            {'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': command}]}]}}, indent=2))

    def audit(self, directory=None):
        path = (directory or self.sandbox) / 'gate-audit.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def session(self, command, mode, marker, settings=None, audit_dir=None):
        audit_dir = audit_dir or self.sandbox
        if mode is not None:
            (self.sandbox / 'mode').write_text(mode + '\n')
            (self.sandbox / 'gate-audit.jsonl').unlink(missing_ok=True)
        (self.fixture / marker).unlink(missing_ok=True)
        try:
            done = subprocess.run(['claude', '-p', '--no-session-persistence', '--settings', str(settings or self.settings),
                                   '--permission-mode', 'auto', prompt(command)], cwd=self.fixture,
                                  capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
            out = done.stdout
        except subprocess.TimeoutExpired:
            out = 'TIMEOUT'
        word = next((w for w in ('EXECUTED', 'BLOCKED') if w in out[:120]), '?')
        return {'answer': word, 'ran': (self.fixture / marker).exists(), 'audit': self.audit(audit_dir), 'reply': out[:200]}

    def case(self, label, command, mode, check):
        marker = 'ran-' + label
        shell = '%s ; touch %s' % (command, marker)
        evidence = self.session(shell, mode, marker)
        if evidence['answer'] == '?':
            evidence = self.session(shell, mode, marker)
            evidence['retried'] = True
        ok, why = check(evidence)
        expected = hashlib.sha256(shell.encode()).hexdigest()[:12]
        sha_ok = all(e.get('cmd_sha12') == expected for e in evidence['audit'])
        if evidence['audit'] and not sha_ok:
            ok, why = False, 'audit cmd_sha12 differs from the sha of the command sent (%s)' % expected
        self.results.append({'case': label, 'mode': mode, 'command': command, 'status': 'PASS' if ok else 'FAIL',
                             'why': why, 'sha_match': sha_ok if evidence['audit'] else None, 'answer': evidence['answer'], 'command_ran': evidence['ran'],
                             'audit': [(e['classification'], e['tool'], e['decision'], e['tier']) for e in evidence['audit']]})

    def production_case(self):
        """P1: hook without --admission-dir, so the production subprocess adapter and default directory are used."""
        real = gate.default_admission_dir()
        prod = self.work / 'extra-settings-prod.json'
        command = 'python3 %s' % (ROOT / '.claude/hooks/disk-admission-gate.py')
        prod.write_text(json.dumps({'hooks': {'PreToolUse': [
            {'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': command}]}]}}, indent=2))
        before = self.audit(real)
        mode = gate.read_mode(real / 'mode')
        shell = 'cargo build --help ; touch ran-P1'
        evidence = self.session(shell, None, 'ran-P1', settings=prod, audit_dir=real)
        new = evidence['audit'][len(before):]
        expected = hashlib.sha256(shell.encode()).hexdigest()[:12]
        decision = 'deny' if mode == 'enforce' else 'warn'
        ok = (len(new) == 1 and new[0]['decision'] == decision and new[0]['tool'] == 'cargo'
              and new[0]['cmd_sha12'] == expected and evidence['ran'] == (mode != 'enforce'))
        self.results.append({'case': 'P1-production-path', 'mode': mode, 'status': 'PASS' if ok else 'FAIL',
                             'why': 'real default dir %s; hook run from the checkout, not ~/.claude/hooks; one new audit line %s'
                                    % (real, new[0]['cmd_sha12'] if new else 'MISSING'),
                             'command_ran': evidence['ran'], 'answer': evidence['answer'],
                             'audit': [(e['classification'], e['tool'], e['decision'], e['tier']) for e in new]})

    def not_verifiable(self, label, reason):
        self.results.append({'case': label, 'status': 'NOT_YET_VERIFIABLE', 'why': reason})


def one_audit(evidence, decision, classification='heavy'):
    lines = evidence['audit']
    return len(lines) == 1 and lines[0]['decision'] == decision and lines[0]['classification'] == classification


def main(argv):
    harness = Harness(argv[0])
    wanted = set(argv[argv.index('--cases') + 1].split(',')) if '--cases' in argv else None
    harness.prepare()
    free = adapter.measure_free_bytes()
    admitted_now = free >= 100 * GIB
    print('free space %.1f GiB (%s)' % (free / GIB, 'admits heavy' if admitted_now else 'below the 100 GiB heavy floor'))
    plan = [
        ('H1', 'cargo build --help', 'warn',
         lambda e: (e['ran'] and one_audit(e, 'warn' if not admitted_now else 'allow'), 'executes; one audit line')),
        ('H2', 'cargo build --help', 'enforce',
         lambda e: ((not e['ran']) and e['answer'] == 'BLOCKED' and one_audit(e, 'deny'), 'denied; command did not run')
         if not admitted_now else (False, 'host admits heavy; H2 needs free < 100 GiB')),
        ('B1', 'git status --short', 'enforce', lambda e: (e['ran'] and e['audit'] == [], 'runs; no audit line')),
        ('B2', 'echo cargo build', 'enforce', lambda e: (e['ran'] and e['audit'] == [], 'runs; no audit line')),
        ('B3', 'cargo --version', 'enforce', lambda e: (e['ran'] and e['audit'] == [], 'runs; no audit line')),
        ('R1a', 'rtk cargo build --help', 'enforce',
         lambda e: ((not e['ran']) and one_audit(e, 'deny') and e['audit'][0]['tool'] == 'cargo', 'rtk twin classed heavy and denied')),
        ('M1w', 'echo note >> %s/scratch-note' % harness.sandbox, 'warn',
         lambda e: (e['ran'] and one_audit(e, 'warn', 'tamper-suspect'), 'advice only; write happened; audit tamper warn')),
        ('M1e', 'echo note >> %s/scratch-note' % harness.sandbox, 'enforce',
         lambda e: ((not e['ran']) and one_audit(e, 'deny', 'tamper-suspect'), 'denied; write did not happen')),
    ]
    for label, command, mode, check in plan:
        if wanted and label not in wanted:
            continue
        harness.case(label, command, mode, check)
        print(label, harness.results[-1]['status'], harness.results[-1].get('answer'), harness.results[-1].get('audit'), flush=True)
    if '--include-production-path' in argv:
        harness.production_case()
        print('P1', harness.results[-1]['status'], harness.results[-1]['audit'], harness.results[-1]['why'], flush=True)
    live = Path.home() / '.claude/settings.json'
    rtk_active = live.exists() and 'rtk hook claude' in live.read_text()
    static_ok = gate.classify('rtk cargo build --help')['heavy'] and gate.classify('cargo build --help')['heavy']
    harness.results.append({'case': 'R1b-static', 'status': 'PASS' if static_ok else 'FAIL',
                            'why': 'rtk twin and plain command both classify heavy; live rtk hook present: %s' % rtk_active})
    harness.not_verifiable('A1-admitted-path', 'host free space is %.1f GiB, below the 100 GiB heavy floor; heavy cannot be '
                           'admitted here, so the admitted path is not exercised' % (free / GIB)) if not admitted_now else None
    (harness.work / 'result.json').write_text(json.dumps(harness.results, indent=2))
    for row in harness.results:
        print('%-18s %-18s %s' % (row['case'], row['status'], row.get('why', '')))
    return 0 if all(r['status'] in ('PASS', 'NOT_YET_VERIFIABLE') for r in harness.results) else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
