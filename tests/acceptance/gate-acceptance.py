#!/usr/bin/env python3
"""Real-engine acceptance for the disk-admission gate hook. NOT run in CI.

Drives real `claude -p` sessions against a disposable fixture repo and a sandbox
admission directory (the hook is registered through --settings with
--admission-dir, so no live settings file and no real state file is touched).
The user's live hooks (for example `rtk hook claude`) stay active, which is the
point of case R1. Evidence per case: the model's one-word answer, a marker file
the command creates only if it really ran, and the sandbox audit lines.

Usage: gate-acceptance.py WORKDIR [--cases H1,H2,...]
The admitted path (free space >= 100 GiB) is reported NOT_YET_VERIFIABLE while
the host is below 100 GiB; it is never faked.
"""
import json
import os
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


def prompt(command):
    return ('Run exactly this one Bash command and nothing else: %s . Reply with exactly one word first: EXECUTED if '
            'the command ran (even with no output or an error), BLOCKED if it was refused or denied by a hook. Do not '
            'retry or use any other tool.' % command)


class Harness:
    def __init__(self, work):
        self.work = Path(work).resolve()
        self.sandbox = self.work / 'sandbox-admission'
        self.fixture = self.work / 'fx/disposable'
        self.settings = self.work / 'extra-settings.json'
        self.results = []

    def prepare(self):
        shutil.rmtree(self.work, ignore_errors=True)
        self.sandbox.mkdir(parents=True, mode=0o700)
        self.fixture.mkdir(parents=True)
        subprocess.run(['git', 'init', '-q'], cwd=self.fixture, check=True)
        (self.fixture / 'README.md').write_text('disposable fixture\n')
        command = 'python3 %s --admission-dir %s' % (ROOT / '.claude/hooks/disk-admission-gate.py', self.sandbox)
        self.settings.write_text(json.dumps({'hooks': {'PreToolUse': [
            {'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': command}]}]}}, indent=2))

    def audit(self):
        path = self.sandbox / 'gate-audit.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def session(self, command, mode, marker):
        (self.sandbox / 'mode').write_text(mode + '\n')
        (self.sandbox / 'gate-audit.jsonl').unlink(missing_ok=True)
        (self.fixture / marker).unlink(missing_ok=True)
        try:
            done = subprocess.run(['claude', '-p', '--no-session-persistence', '--settings', str(self.settings),
                                   '--permission-mode', 'auto', prompt(command)], cwd=self.fixture,
                                  capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
            out = done.stdout
        except subprocess.TimeoutExpired:
            out = 'TIMEOUT'
        word = next((w for w in ('EXECUTED', 'BLOCKED') if w in out[:120]), '?')
        return {'answer': word, 'ran': (self.fixture / marker).exists(), 'audit': self.audit(), 'reply': out[:200]}

    def case(self, label, command, mode, check):
        marker = 'ran-' + label
        shell = '%s ; touch %s' % (command, marker)
        evidence = self.session(shell, mode, marker)
        if evidence['answer'] == '?':
            evidence = self.session(shell, mode, marker)
            evidence['retried'] = True
        ok, why = check(evidence)
        self.results.append({'case': label, 'mode': mode, 'command': command, 'status': 'PASS' if ok else 'FAIL',
                             'why': why, 'answer': evidence['answer'], 'command_ran': evidence['ran'],
                             'audit': [(e['classification'], e['tool'], e['decision'], e['tier']) for e in evidence['audit']]})

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
