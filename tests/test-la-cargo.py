#!/usr/bin/env python3
"""la-cargo heavy-job admission (SPE-174): single accounting, marker, light bypass, runner-missing, unchanged gates.

No real cargo and no real admission state: cargo and the admission runner are fake scripts in a temp directory, and every path
la-cargo writes (target, lock, log) is redirected there.
"""
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader, spec_from_file_location
import contextlib
import fcntl
import io
import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GIB = 2 ** 30
loader = SourceFileLoader('la_cargo', str(ROOT / 'scripts/la-cargo'))
la = module_from_spec(spec_from_loader('la_cargo', loader))
loader.exec_module(la)
spec = spec_from_file_location('disk_admission', ROOT / 'scripts/disk-admission.py')
da = module_from_spec(spec)
spec.loader.exec_module(da)

FAKE_CARGO = '''\
#!/usr/bin/env python3
import fcntl, json, os, sys
try:
    probe = open(os.environ['FAKE_LOCK'], 'a')
    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    lock_free = True
except BlockingIOError:
    lock_free = False
with open(os.path.join(os.environ['FAKE_DIR'], 'cargo.jsonl'), 'a') as f:
    f.write(json.dumps({'args': sys.argv[1:], 'target': os.environ.get('CARGO_TARGET_DIR'), 'jobs': os.environ.get('CARGO_BUILD_JOBS'),
                        'lock_free': lock_free}) + '\\n')
sys.exit(int(os.environ.get('FAKE_CARGO_RC', '0')))
'''

FAKE_RUNNER = '''\
#!/usr/bin/env python3
import json, os, sys
d = os.environ['FAKE_DIR']
argv = sys.argv[1:]
mode = open(os.path.join(d, 'mode')).read().strip()
with open(os.path.join(d, 'runner.jsonl'), 'a') as f:
    f.write(json.dumps({'argv': argv}) + '\\n')
cmd = argv[0]
def counter(name):
    path = os.path.join(d, name)
    n = int(open(path).read()) if os.path.exists(path) else 0
    open(path, 'w').write(str(n + 1))
    return n
if cmd == 'run':
    job = argv[argv.index('--') + 1:]
    queued = '--queue-id' in argv
    if mode == 'admit' or (mode == 'queue' and queued):
        os.execvp(job[0], job)
    if mode == 'crash':
        sys.exit(1)
    print('refused: lane-cap: 2 of 2 heavy lanes in use', file=sys.stderr)
    sys.exit(77)
if cmd == 'queue':
    sub = argv[1]
    if sub == 'enqueue':
        print(json.dumps({'ok': mode != 'enqueue-fails', 'id': 'q000001', 'reason': 'fake'}))
        sys.exit(0 if mode != 'enqueue-fails' else 77)
    if sub == 'status':
        waits = int(os.environ.get('FAKE_WAITS', '0'))
        state = 'GRANTED' if mode == 'queue' and counter('polls') >= waits else 'QUEUED'
        print(json.dumps({'entries': [{'id': 'q000001', 'state': state}]}))
        sys.exit(0)
    print(json.dumps({'ok': True}))
    sys.exit(0)
print('ok')
'''


def script(path, body):
    Path(path).write_text(body)
    os.chmod(path, 0o755)


class LaCargoBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        (self.dir / 'bin').mkdir()
        script(self.dir / 'bin/cargo', FAKE_CARGO)
        script(self.dir / 'runner.py', FAKE_RUNNER)
        (self.dir / 'mode').write_text('admit')
        self.target = str(self.dir / 'targets/local-agent')
        self.lock = str(self.dir / 'targets/local-agent.lock')
        patches = [
            unittest.mock.patch.multiple(la, TARGET=self.target, LOCK=self.lock, LOG=str(self.dir / 'targets/local-agent.log'),
                                         RUNNER=str(self.dir / 'runner.py'), POLL_S=0, JOBS='8'),
            unittest.mock.patch.dict(os.environ, {'PATH': '%s/bin:%s' % (self.dir, os.environ['PATH']), 'FAKE_DIR': str(self.dir),
                                                  'FAKE_LOCK': self.lock, 'FAKE_CARGO_RC': '0'}),
            unittest.mock.patch.object(la, 'free_gib', lambda: self.free),
            unittest.mock.patch.object(la, 'target_gib', lambda: self.size),
            unittest.mock.patch.object(la, 'process_ancestors', lambda: set(self.ancestors)),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop(la.HOLDER_ENV, None)
        self.free, self.size, self.ancestors = 200.0, 1.0, []

    def run_la(self, *args):
        err = io.StringIO()
        with unittest.mock.patch.object(sys, 'argv', ['la-cargo', *args]), contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = la.main()
        return rc, err.getvalue()

    def lines(self, name):
        path = self.dir / name
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def runner_commands(self):
        return [(c['argv'][0], c['argv'][1] if c['argv'][0] == 'queue' else None) for c in self.lines('runner.jsonl')]


class UnchangedGates(LaCargoBase):
    def test_heavy_refused_below_75_and_never_reaches_runner_or_cargo(self):
        self.free = 74.0
        rc, err = self.run_la('build')
        self.assertEqual(rc, 75)
        self.assertEqual(self.lines('runner.jsonl'), [])
        self.assertEqual(self.lines('cargo.jsonl'), [])

    def test_everything_refused_below_50_including_light(self):
        self.free = 49.0
        self.assertEqual(self.run_la('--light', 'metadata')[0], 75)
        self.assertEqual(self.lines('cargo.jsonl'), [])

    def test_warning_below_100_still_builds(self):
        self.free = 90.0
        rc, err = self.run_la('build')
        self.assertEqual(rc, 0)
        self.assertIn('WARNING 90.0 GiB free', err)
        self.assertEqual(len(self.lines('cargo.jsonl')), 1)

    def test_over_budget_prunes_incremental_then_refuses_76_when_still_over(self):
        inc = Path(self.target) / 'debug/incremental'
        inc.mkdir(parents=True)
        (inc / 'junk').write_text('x')
        self.size = 61.0
        rc, err = self.run_la('build')
        self.assertEqual(rc, 76)
        self.assertFalse((inc / 'junk').exists())
        self.assertTrue(inc.is_dir())
        self.assertEqual(self.lines('runner.jsonl'), [])
        self.assertEqual(self.lines('cargo.jsonl'), [])

    def test_prune_that_gets_under_budget_proceeds(self):
        inc = Path(self.target) / 'debug/incremental'
        inc.mkdir(parents=True)
        sizes = iter([61.0, 30.0])
        with unittest.mock.patch.object(la, 'target_gib', lambda: next(sizes)):
            rc, _ = self.run_la('build')
        self.assertEqual(rc, 0)

    def test_cargo_gets_dedicated_target_and_jobs(self):
        self.run_la('build', '--locked')
        (call,) = self.lines('cargo.jsonl')
        self.assertEqual((call['args'], call['target'], call['jobs']), (['build', '--locked'], self.target, '8'))

    def test_heavy_lock_is_held_while_the_runner_and_cargo_run(self):
        self.run_la('build')
        (call,) = self.lines('cargo.jsonl')
        self.assertFalse(call['lock_free'], 'cargo ran without the heavy lock held')

    def test_second_heavy_build_waits_on_the_lock_before_touching_the_runner(self):
        os.makedirs(os.path.dirname(self.lock), exist_ok=True)
        held = open(self.lock, 'w')
        fcntl.flock(held, fcntl.LOCK_EX)
        self.addCleanup(held.close)
        seen = []

        real_flock = fcntl.flock

        def blocking_flock(handle, op):
            if op == fcntl.LOCK_EX | fcntl.LOCK_NB:
                seen.append('nb')
                raise BlockingIOError()
            seen.append('wait')
            real_flock(held, fcntl.LOCK_UN)   # the holder lets go; the blocking acquire then succeeds
            return real_flock(handle, op)

        with unittest.mock.patch.object(la.fcntl, 'flock', blocking_flock):
            rc, err = self.run_la('build')
        self.assertEqual((rc, seen), (0, ['nb', 'wait']))
        self.assertIn('another heavy Local Agent build holds the lock', err)

    def test_status_reports_and_touches_nothing_else(self):
        rc, _ = self.run_la('--status')
        self.assertEqual(rc, 0)
        self.assertEqual(self.lines('runner.jsonl') + self.lines('cargo.jsonl'), [])

    def test_no_arguments_prints_usage(self):
        self.assertEqual(self.run_la()[0], 2)


class SingleAccounting(LaCargoBase):
    def test_a_heavy_build_is_charged_exactly_once_and_released(self):
        rc, _ = self.run_la('build')
        self.assertEqual(rc, 0)
        runs = [c['argv'] for c in self.lines('runner.jsonl') if c['argv'][0] == 'run']
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual(run[run.index('--class') + 1], 'build-workspace')
        self.assertEqual(int(run[run.index('--bytes') + 1]), 40 * GIB)
        self.assertEqual(run[run.index('--') + 6:], ['cargo', 'build'])
        label = run[run.index('--label') + 1]
        self.assertRegex(label, da.LABEL_RE)
        self.assertEqual(len(self.lines('cargo.jsonl')), 1)
        self.assertEqual(self.runner_commands()[-1], ('release', None))
        self.assertEqual(self.lines('runner.jsonl')[-1]['argv'], ['release', '--label', label])

    def test_class_and_bytes_are_accepted_by_the_real_runner_parser(self):
        self.run_la('build')
        argv = [c['argv'] for c in self.lines('runner.jsonl') if c['argv'][0] == 'run'][0]
        args = da.build_parser().parse_args(argv[:argv.index('--') + 1] + ['x'])
        declared, error = da.resolve_workload(args.workload_class, args.mem_gib, args.cpus, args.nbytes, env={}, home='/nonexistent-home',
                                              cwd='/work/wt')
        self.assertIsNone(error)
        self.assertEqual(declared['class'], 'build-workspace')
        self.assertEqual(da.WORKLOAD_CLASSES['build-workspace']['min_jobs'], 4)

    def test_cargos_exit_code_77_is_not_mistaken_for_a_refusal(self):
        os.environ['FAKE_CARGO_RC'] = '77'
        rc, _ = self.run_la('run')
        self.assertEqual(rc, 77)
        self.assertEqual(len(self.lines('cargo.jsonl')), 1)
        self.assertNotIn(('queue', 'enqueue'), self.runner_commands())

    def test_a_busy_lane_queues_then_claims_once(self):
        (self.dir / 'mode').write_text('queue')
        os.environ['FAKE_WAITS'] = '2'
        rc, err = self.run_la('build')
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.lines('cargo.jsonl')), 1)
        kinds = self.runner_commands()
        self.assertEqual(kinds[0], ('run', None))
        self.assertEqual(kinds.count(('queue', 'enqueue')), 1)
        claims = [c['argv'] for c in self.lines('runner.jsonl') if '--queue-id' in c['argv']]
        self.assertEqual(len(claims), 1)
        self.assertEqual(kinds[-1], ('queue', 'release'))
        self.assertNotIn(('queue', 'cancel'), kinds)
        self.assertIn('waiting for a heavy lane', err)
        enqueue = [c['argv'] for c in self.lines('runner.jsonl') if c['argv'][:2] == ['queue', 'enqueue']][0]
        self.assertIn('--again', enqueue)
        self.assertEqual(enqueue[enqueue.index('--harness') + 1], 'claude')
        self.assertEqual(enqueue[enqueue.index('--worktree') + 1], os.getcwd())

    def test_queue_timeout_cancels_and_never_builds(self):
        (self.dir / 'mode').write_text('refuse')
        ticks = iter(range(0, 10 ** 6, 1000))
        with unittest.mock.patch.object(la, 'QUEUE_WAIT_S', 3000):
            rc, err = self.run_la_clocked('build', ticks)
        self.assertEqual(rc, 77)
        self.assertEqual(self.lines('cargo.jsonl'), [])
        self.assertIn(('queue', 'cancel'), self.runner_commands())
        self.assertIn('no heavy lane within', err)

    def run_la_clocked(self, arg, ticks):
        err = io.StringIO()
        orig = la.admitted_build

        def with_clock(a, env, sleep=None, clock=None):
            return orig(a, env, sleep=lambda s: None, clock=lambda: next(ticks))

        with unittest.mock.patch.object(la, 'admitted_build', with_clock), unittest.mock.patch.object(sys, 'argv', ['la-cargo', arg]), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = la.main()
        return rc, err.getvalue()

    def test_an_unqueueable_build_is_refused_not_started(self):
        (self.dir / 'mode').write_text('enqueue-fails')
        rc, err = self.run_la('build')
        self.assertEqual(rc, 77)
        self.assertEqual(self.lines('cargo.jsonl'), [])
        self.assertIn('could not queue the build', err)

    def test_a_runner_that_fails_before_cargo_fails_closed(self):
        (self.dir / 'mode').write_text('crash')
        rc, err = self.run_la('build')
        self.assertEqual(rc, 1)
        self.assertEqual(self.lines('cargo.jsonl'), [])
        self.assertIn('fail closed', err)


class Marker(LaCargoBase):
    def test_marker_naming_this_process_prevents_a_second_charge(self):
        os.environ[la.HOLDER_ENV] = 'outer-job:%d' % os.getpid()
        rc, _ = self.run_la('build')
        self.assertEqual(rc, 0)
        self.assertEqual(self.lines('runner.jsonl'), [])
        self.assertEqual(len(self.lines('cargo.jsonl')), 1)

    def test_marker_naming_an_ancestor_prevents_a_second_charge(self):
        self.ancestors = [4242]
        os.environ[la.HOLDER_ENV] = 'outer-job:4242'
        self.assertEqual(self.run_la('build')[0], 0)
        self.assertEqual(self.lines('runner.jsonl'), [])

    def test_marker_for_an_unrelated_pid_is_ignored_and_charged(self):
        self.ancestors = [4242]
        os.environ[la.HOLDER_ENV] = 'stale-job:999999'
        self.assertEqual(self.run_la('build')[0], 0)
        self.assertEqual([c['argv'][0] for c in self.lines('runner.jsonl')].count('run'), 1)

    def test_malformed_marker_is_ignored(self):
        for value in ('', 'nolabel', 'a:b', ':12', 'a b:12', 'x:%d; rm' % os.getpid()):
            os.environ[la.HOLDER_ENV] = value
            self.assertIsNone(la.inherited_holder(), value)

    def test_the_marker_does_not_skip_the_heavy_lock_or_gates(self):
        os.environ[la.HOLDER_ENV] = 'outer-job:%d' % os.getpid()
        self.free = 60.0
        self.assertEqual(self.run_la('build')[0], 75)
        self.free = 200.0
        self.run_la('build')
        self.assertFalse(self.lines('cargo.jsonl')[0]['lock_free'])

    def test_the_real_runner_exports_the_marker_it_checks_for(self):
        self.assertEqual(da.HOLDER_ENV, la.HOLDER_ENV)
        seen = []
        with tempfile.TemporaryDirectory() as state, unittest.mock.patch.dict(os.environ):
            os.environ.pop(da.HOLDER_ENV, None)
            path = Path(state) / 'state.json'
            rc = da.run_reserved('la-cargo-1', 40 * GIB, ['true'], path, lambda: 400 * GIB,
                                 exec_fn=lambda program, argv: seen.append(os.environ.get(da.HOLDER_ENV)),
                                 declared=da.resolve_workload('build-workspace', None, None, 40 * GIB, env={}, home='/nonexistent-home',
                                                              cwd='/work/wt')[0])
        self.assertEqual((rc, seen), (0, ['la-cargo-1:%d' % os.getpid()]))

    def test_a_refused_runner_run_exports_no_marker(self):
        with tempfile.TemporaryDirectory() as state, unittest.mock.patch.dict(os.environ):
            os.environ.pop(da.HOLDER_ENV, None)
            rc = da.run_reserved('la-cargo-2', 40 * GIB, ['true'], Path(state) / 'state.json', lambda: 1 * GIB,
                                 exec_fn=lambda program, argv: self.fail('exec'))
            self.assertEqual(rc, da.REFUSED_EXIT)
            self.assertNotIn(da.HOLDER_ENV, os.environ)


class LightBypass(LaCargoBase):
    def test_light_is_never_charged_locked_or_queued(self):
        rc, _ = self.run_la('--light', 'metadata')
        self.assertEqual(rc, 0)
        self.assertEqual(self.lines('runner.jsonl'), [])
        (call,) = self.lines('cargo.jsonl')
        self.assertTrue(call['lock_free'], 'light must not take the heavy lock')
        self.assertEqual(call['args'], ['metadata'])

    def test_light_is_allowed_between_50_and_75(self):
        self.free = 60.0
        self.assertEqual(self.run_la('--light', 'tree')[0], 0)

    def test_light_with_a_missing_runner_stays_silent(self):
        la.RUNNER = str(self.dir / 'absent.py')
        rc, err = self.run_la('--light', 'metadata')
        self.assertEqual(rc, 0)
        self.assertNotIn('WARNING admission runner', err)


class RunnerMissing(LaCargoBase):
    def test_missing_runner_degrades_loudly_to_the_previous_behaviour(self):
        la.RUNNER = str(self.dir / 'absent.py')
        os.environ['FAKE_CARGO_RC'] = '3'
        rc, err = self.run_la('build')
        self.assertEqual(rc, 3)
        self.assertIn('WARNING admission runner not found', err)
        self.assertIn('NOT accounted', err)
        (call,) = self.lines('cargo.jsonl')
        self.assertFalse(call['lock_free'])
        self.assertIn('UNADMITTED runner-missing', (self.dir / 'targets/local-agent.log').read_text())

    def test_missing_runner_still_enforces_the_gates(self):
        la.RUNNER = str(self.dir / 'absent.py')
        self.free = 70.0
        self.assertEqual(self.run_la('build')[0], 75)
        self.size = 70.0
        self.free = 200.0
        self.assertEqual(self.run_la('build')[0], 76)
        self.assertEqual(self.lines('cargo.jsonl'), [])


if __name__ == '__main__':
    unittest.main()
