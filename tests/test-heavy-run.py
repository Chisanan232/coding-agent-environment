#!/usr/bin/env python3
"""heavy-run (ADR 0017): declared class, queue entry point, FIFO-safe start, release after exit, conservative unknown accounting.

The admission runner is a fake script in a temp directory that records every call and, like the real one, execs the job only on a
claim; no real admission state is touched. The accounting tests use the real budget arithmetic.
"""
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader, spec_from_file_location
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GIB = 2 ** 30
loader = SourceFileLoader('heavy_run', str(ROOT / 'scripts/heavy-run'))
hr = module_from_spec(spec_from_loader('heavy_run', loader))
loader.exec_module(hr)


def load(name, file):
    spec = spec_from_file_location(name, ROOT / file)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


shadow = load('shadow_budget_tests', 'tests/test-shadow-budget.py')
da = shadow.da

FAKE_RUNNER = '''\
#!/usr/bin/env python3
import json, os, subprocess, sys
d = os.environ['FAKE_DIR']
argv = sys.argv[1:]
mode = open(os.path.join(d, 'mode')).read().strip()
def log(**kw):
    with open(os.path.join(d, 'runner.jsonl'), 'a') as f:
        f.write(json.dumps(kw) + '\\n')
def counter(name):
    path = os.path.join(d, name)
    n = int(open(path).read()) if os.path.exists(path) else 0
    open(path, 'w').write(str(n + 1))
    return n
cmd = argv[0]
log(argv=argv)
if cmd == 'run':
    job = argv[argv.index('--') + 1:]
    queued = '--queue-id' in argv
    if mode == 'claimrefused' and queued and counter('claims') == 0:
        sys.exit(77)                       # the first claim is refused (gate), the grant is kept
    if mode == 'free' or (mode in ('queue', 'claimrefused') and queued):
        log(event='job-start', claim=queued)
        os.execvp(job[0], job)             # like the real runner: the job replaces the runner, so its exit status is the wrapper's input
    if mode in ('queue', 'claimrefused', 'cancelled'):
        sys.exit(77)                       # queue-ahead: entries already wait, a direct start is refused
    if mode == 'broken':
        sys.exit(2)
    sys.exit(77)
if cmd == 'queue' and argv[1] == 'enqueue':
    print(json.dumps({'ok': True, 'id': 'q000900'}))
elif cmd == 'queue' and argv[1] == 'status':
    n = counter('polls')
    state = 'GRANTED' if mode in ('queue', 'claimrefused') and n >= 2 else ('CANCELLED' if mode == 'cancelled' else 'QUEUED')
    print(json.dumps({'entries': [{'id': 'q000900', 'state': state}]}))
'''

COMMAND = '''\
#!/usr/bin/env python3
import os, sys
open(os.path.join(os.environ['FAKE_DIR'], 'command.ran'), 'a').write('x')
if os.environ.get('FAKE_COMMAND_KILL'):
    os.kill(os.getpid(), int(os.environ['FAKE_COMMAND_KILL']))
sys.exit(int(os.environ.get('FAKE_COMMAND_RC', '0')))
'''


class Wrapper(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        for name, body in (('runner', FAKE_RUNNER), ('command', COMMAND)):
            path = self.dir / name
            path.write_text(body)
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        old = hr.RUNNER
        hr.RUNNER = str(self.dir / 'runner')
        self.addCleanup(setattr, hr, 'RUNNER', old)
        self.env = {'PATH': os.environ['PATH'], 'FAKE_DIR': str(self.dir)}
        self.now = 0.0
        for sig in (hr.signal.SIGTERM, hr.signal.SIGHUP):
            self.addCleanup(hr.signal.signal, sig, hr.signal.getsignal(sig))   # main() installs handlers into this process

    def mode(self, value):
        (self.dir / 'mode').write_text(value)

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def invoke(self, extra=('--class', 'build-workspace'), max_wait=3600, rc=0):
        self.env['FAKE_COMMAND_RC'] = str(rc)
        argv = ['--label', 'pricing-fix-full4', '--bytes', str(40 * GIB), '--max-wait-s', str(max_wait), *extra, '--',
                str(self.dir / 'command')]
        args = hr.parse(argv)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = hr.admitted(args, self.env, sleep=self.sleep, clock=self.clock)
        return code, err.getvalue()

    def calls(self):
        path = self.dir / 'runner.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def argvs(self, *prefix):
        return [c['argv'] for c in self.calls() if 'argv' in c and c['argv'][:len(prefix)] == list(prefix)]

    def ran(self):
        path = self.dir / 'command.ran'
        return len(path.read_text()) if path.exists() else 0

    def test_a_free_lane_runs_the_command_with_the_declared_class_and_releases_after_exit(self):
        self.mode('free')
        code, _ = self.invoke(rc=3)
        self.assertEqual(code, 3)                                  # the command's own exit code, not the wrapper's
        run = self.argvs('run')[0]
        self.assertEqual(run[run.index('--class') + 1], 'build-workspace')
        self.assertEqual(run[run.index('--bytes') + 1], str(40 * GIB))
        self.assertEqual(self.argvs('queue', 'enqueue'), [])
        self.assertEqual(self.argvs('release'), [['release', '--label', 'pricing-fix-full4']])
        self.assertEqual(self.ran(), 1)

    def test_a_job_that_cannot_start_directly_queues_with_the_same_class_and_argv_and_starts_only_after_the_grant(self):
        self.mode('queue')
        code, _ = self.invoke()
        self.assertEqual(code, 0)
        direct, claim = self.argvs('run')
        enqueue = self.argvs('queue', 'enqueue')[0]
        self.assertNotIn('--queue-id', direct)
        self.assertEqual(claim[claim.index('--queue-id') + 1], 'q000900')
        for argv in (enqueue, claim):                              # the fingerprint covers class, bytes and argv: they must match
            self.assertEqual(argv[argv.index('--class') + 1], 'build-workspace')
            self.assertEqual(argv[argv.index('--bytes') + 1], str(40 * GIB))
        self.assertEqual(enqueue[enqueue.index('--') + 1:], claim[claim.index('--') + 1:])
        self.assertGreaterEqual(len(self.argvs('queue', 'advance')), 3)   # it waited through QUEUED polls
        starts = [c for c in self.calls() if c.get('event') == 'job-start']
        self.assertEqual([s['claim'] for s in starts], [True])     # started once, and only as the claim: never as the direct attempt
        self.assertEqual(self.ran(), 1)

    def test_the_queue_entry_is_released_after_the_command_exits_with_its_own_session(self):
        self.mode('queue')
        self.invoke(rc=5)
        enqueue = self.argvs('queue', 'enqueue')[0]
        release = self.argvs('queue', 'release')[0]
        self.assertEqual(release[release.index('--id') + 1], 'q000900')
        self.assertEqual(release[release.index('--session-id') + 1], enqueue[enqueue.index('--session-id') + 1])
        self.assertEqual(self.argvs('queue', 'cancel'), [])
        order = [c['argv'][0] if c['argv'][0] != 'queue' else 'queue ' + c['argv'][1] for c in self.calls() if 'argv' in c]
        self.assertEqual(order[-1], 'queue release')               # released last, after the claimed command exited

    def test_a_command_that_exits_77_itself_is_not_mistaken_for_a_refusal(self):
        self.mode('free')
        code, _ = self.invoke(rc=77)
        self.assertEqual(code, 77)
        self.assertEqual(self.argvs('queue', 'enqueue'), [])
        self.assertEqual(self.ran(), 1)
        self.assertEqual(len(self.argvs('release')), 1)

    def test_waiting_past_the_bound_never_starts_the_command_and_cancels_the_entry(self):
        self.mode('refuse')
        code, err = self.invoke(max_wait=90)
        self.assertEqual(code, hr.TIMEOUT_EXIT)
        self.assertEqual(self.ran(), 0)
        self.assertIn('NOT started', err)
        self.assertEqual(len(self.argvs('queue', 'cancel')), 1)

    def test_a_runner_failure_other_than_a_refusal_fails_closed(self):
        self.mode('broken')
        code, err = self.invoke()
        self.assertEqual(code, 2)
        self.assertEqual(self.ran(), 0)
        self.assertEqual(self.argvs('queue', 'enqueue'), [])
        self.assertIn('fail closed', err)

    def test_a_job_without_a_class_declares_none_and_says_it_is_charged_as_unknown(self):
        self.mode('free')
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            code = hr.main(['--label', 'x1', '--bytes', str(GIB), '--', str(self.dir / 'command')], self.env)
        self.assertEqual(code, 0)
        self.assertNotIn('--class', self.argvs('run')[0])
        self.assertIn('unknown workload', out.getvalue())

    def test_a_job_already_inside_a_lane_is_not_charged_again(self):
        self.mode('free')
        env = dict(self.env, **{hr.HOLDER_ENV: 'outer:%d' % os.getpid()})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = hr.main(['--label', 'x2', '--class', 'build-workspace', '--bytes', str(40 * GIB), '--', str(self.dir / 'command')], env)
        self.assertEqual(code, 0)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.ran(), 1)

    def test_a_refused_claim_keeps_the_grant_and_retries_without_starting_the_job_outside_a_lane(self):
        self.mode('claimrefused')
        code, err = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.argvs('run')), 3)                # direct (refused), claim (refused), claim (started)
        self.assertEqual([c['claim'] for c in self.calls() if c.get('event') == 'job-start'], [True])
        self.assertIn('retrying', err)
        self.assertEqual(self.argvs('queue', 'cancel'), [])

    def test_a_queue_entry_that_is_no_longer_waiting_refuses_and_starts_nothing(self):
        self.mode('cancelled')
        code, err = self.invoke()
        self.assertEqual(code, hr.REFUSED_EXIT)
        self.assertEqual(self.ran(), 0)
        self.assertIn('CANCELLED', err)

    def test_a_command_killed_by_a_signal_exits_as_a_shell_would(self):
        self.mode('free')
        self.env['FAKE_COMMAND_KILL'] = '9'
        code, _ = self.invoke()
        self.assertEqual(code, 137)

    def test_an_interrupt_after_the_claim_started_the_job_releases_the_entry_instead_of_cancelling_it(self):
        self.mode('queue')
        real_call = hr.subprocess.call

        def call(cmd, **kw):
            if '--queue-id' in cmd:
                proof = cmd[cmd.index('--') + 5]                    # /bin/sh -c SCRIPT heavy-run PROOF ...
                Path(proof).touch()
                raise KeyboardInterrupt
            return real_call(cmd, **kw)

        hr.subprocess.call = call
        self.addCleanup(setattr, hr.subprocess, 'call', real_call)
        with self.assertRaises(KeyboardInterrupt):
            self.invoke()
        self.assertEqual(len(self.argvs('queue', 'release')), 1)
        self.assertEqual(self.argvs('queue', 'cancel'), [])

    def test_a_ttl_the_runner_would_refuse_is_a_usage_error_not_a_retry_loop(self):
        for ttl in ('0', '-5', '14401'):
            with contextlib.redirect_stderr(io.StringIO()):
                code = hr.main(['--label', 'x', '--bytes', '1', '--ttl-s', ttl, '--', str(self.dir / 'command')], self.env)
            self.assertEqual(code, 64, ttl)
        self.assertEqual(self.calls(), [])

    def test_a_missing_command_inside_a_lane_exits_127_without_a_traceback(self):
        env = dict(self.env, **{hr.HOLDER_ENV: 'outer:%d' % os.getpid()})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = hr.main(['--label', 'x', '--bytes', '1', '--', str(self.dir / 'no-such-command')], env)
        self.assertEqual(code, 127)
        self.assertIn('cannot run', err.getvalue())

    def test_a_forged_holder_marker_for_an_unrelated_pid_is_ignored(self):
        self.assertIsNone(hr.inherited_holder({hr.HOLDER_ENV: 'outer:1999999'}))
        self.assertIsNone(hr.inherited_holder({hr.HOLDER_ENV: 'bad marker'}))

    def test_usage_errors_exit_64_and_the_runner_is_the_checkouts_own(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(hr.main(['--label', 'x', '--bytes', '1'], self.env), 64)
        fresh_loader = SourceFileLoader('heavy_run_fresh', str(ROOT / 'scripts/heavy-run'))
        fresh = module_from_spec(spec_from_loader('heavy_run_fresh', fresh_loader))
        fresh_loader.exec_module(fresh)
        self.assertEqual(fresh.RUNNER, str(ROOT / 'scripts/disk-admission.py'))


class Accounting(unittest.TestCase):
    """Resource accounting with the real budget arithmetic: what the declared class changes for the other lanes."""
    WORKSPACE = {'class': 'build-workspace', 'mem_bytes': 8 * GIB, 'cpu_slots': 8, 'test_threads': 8}

    def test_a_job_without_a_declared_class_is_charged_the_whole_budget_and_blocks_a_second_lane(self):
        out = shadow.decide(holders=[shadow.holder('full4', {'bytes': 34 * GIB}, excl=())], req=shadow.request(shadow.CRATE))
        self.assertTrue(out['holders'][0]['legacy'])
        self.assertFalse(out['fits_if_cap_were_2'])

    def test_the_same_job_with_its_documented_class_leaves_room_for_a_second_lane(self):
        out = shadow.decide(holders=[shadow.holder('full4', self.WORKSPACE, excl=('cargo-target:/a',))], req=shadow.request(shadow.CRATE))
        self.assertFalse(out['holders'][0]['legacy'])
        self.assertLess(out['holders'][0]['charge_bytes'], 16 * GIB)
        self.assertTrue(out['fits_if_cap_were_2'], out.get('resource_reasons'))


if __name__ == '__main__':
    unittest.main()
