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
import signal
import stat
import sys
import tempfile
import textwrap
import time
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
if mode in ('revoked', 'expired', 'revokedforever'):
    # SPE-176: 'revoked' revokes the claim of q000001 (EXPIRED), accepts --requeue-of and grants the replacement; 'expired' is an
    # ordinary expiry the runner refuses to re-queue; 'revokedforever' accepts every re-queue and expires each replacement.
    cur_path, revoked = os.path.join(d, 'current'), os.path.join(d, 'revoked')
    cur = open(cur_path).read() if os.path.exists(cur_path) else 'q000001'
    if cmd == 'run':
        if '--queue-id' in argv and argv[argv.index('--queue-id') + 1] != 'q000001' and mode == 'revoked':
            job = argv[argv.index('--') + 1:]
            os.execvp(job[0], job)
        if '--queue-id' in argv:
            open(revoked, 'w').write('1')
        print('refused: lane-cap: 2 of 2 heavy lanes in use', file=sys.stderr)
        sys.exit(77)
    if cmd == 'queue' and argv[1] == 'enqueue':
        if '--requeue-of' in argv:
            if mode == 'expired' or argv[argv.index('--requeue-of') + 1] != cur:
                print(json.dumps({'ok': False, 'reason': 'not-requeueable: did not lose a backfilled grant'}))
                sys.exit(77)
            cur = 'q%06d' % (int(cur[1:]) + 1)
            open(cur_path, 'w').write(cur)
        print(json.dumps({'ok': True, 'id': cur}))
    elif cmd == 'queue' and argv[1] == 'status':
        if mode == 'revoked':
            state = 'GRANTED' if cur != 'q000001' or not os.path.exists(revoked) else 'EXPIRED'
        else:
            state = 'EXPIRED'
        print(json.dumps({'entries': [{'id': cur, 'state': state}]}))
    else:
        print(json.dumps({'ok': True}))
    sys.exit(0)
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
                                         RUNNER=str(self.dir / 'runner.py'), POLL_S=0, JOBS='8',
                                         RECOVERY_LIB=str(self.dir / 'absent-recovery.py')),      # the wrapper registry stays off unless a test enables it
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
        waiter = enqueue[enqueue.index('--waiter-pid') + 1]   # SPE-176: the supervising wrapper, so a dead one frees its place
        self.assertTrue(waiter.isdigit() and int(waiter) > 0, waiter)

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

    def queue_argvs(self, sub):
        return [c['argv'] for c in self.lines('runner.jsonl') if c['argv'][:2] == ['queue', sub]]

    def test_a_revoked_backfill_grant_is_requeued_and_the_build_runs_once_on_the_replacement(self):
        (self.dir / 'mode').write_text('revoked')
        rc, err = self.run_la('build')
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.lines('cargo.jsonl')), 1)
        self.assertIn('backfill grant revoked; re-queued as q000002', err)
        first, requeue = self.queue_argvs('enqueue')
        self.assertEqual(requeue[requeue.index('--requeue-of') + 1], 'q000001')
        for flag in ('--session-id', '--waiter-pid', '--label', '--bytes', '--class', '--worktree'):
            self.assertEqual(requeue[requeue.index(flag) + 1], first[first.index(flag) + 1], flag)
        self.assertEqual(requeue[requeue.index('--') + 1:], first[first.index('--') + 1:])
        parsed = da.build_parser().parse_args(requeue[:requeue.index('--') + 1] + ['x'])    # the real runner accepts the re-queue call
        self.assertEqual((parsed.requeue_of, parsed.again), ('q000001', True))
        self.assertEqual([r[r.index('--id') + 1] for r in self.queue_argvs('release')], ['q000002'])
        self.assertEqual(self.queue_argvs('cancel'), [])

    def test_an_ordinary_expiry_still_refuses_with_77(self):
        (self.dir / 'mode').write_text('expired')
        rc, err = self.run_la('build')
        self.assertEqual(rc, 77)
        self.assertEqual(self.lines('cargo.jsonl'), [])
        self.assertIn('la-cargo: queue entry q000001 is EXPIRED; refusing.', err)
        self.assertNotIn('re-queued', err)
        self.assertEqual([c[c.index('--id') + 1] for c in self.queue_argvs('cancel')], ['q000001'])

    def test_requeues_are_bounded(self):
        (self.dir / 'mode').write_text('revokedforever')
        rc, err = self.run_la('build')
        self.assertEqual(rc, 77)
        self.assertEqual(self.lines('cargo.jsonl'), [])
        self.assertEqual(len([a for a in self.queue_argvs('enqueue') if '--requeue-of' in a]), la.REQUEUE_MAX)
        self.assertIn('EXPIRED after 3 backfill re-queues (limit 3)', err)
        self.assertEqual([c[c.index('--id') + 1] for c in self.queue_argvs('cancel')], ['q000004'])

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
    def test_missing_runner_refuses_the_build_by_default(self):
        la.RUNNER = str(self.dir / 'absent.py')
        rc, err = self.run_la('build')
        self.assertEqual(rc, 77)
        self.assertIn('refusing the build', err)
        self.assertEqual(self.lines('cargo.jsonl'), [])
        self.assertIn('REFUSED runner-missing', (self.dir / 'targets/local-agent.log').read_text())

    def test_missing_runner_with_the_explicit_opt_out_builds_loudly_unaccounted(self):
        la.RUNNER = str(self.dir / 'absent.py')
        os.environ['LA_CARGO_ALLOW_UNADMITTED'] = '1'
        self.addCleanup(os.environ.pop, 'LA_CARGO_ALLOW_UNADMITTED', None)
        os.environ['FAKE_CARGO_RC'] = '3'
        rc, err = self.run_la('build')
        self.assertEqual(rc, 3)
        self.assertIn('NOT accounted', err)
        (call,) = self.lines('cargo.jsonl')
        self.assertFalse(call['lock_free'])
        self.assertIn('UNADMITTED runner-missing', (self.dir / 'targets/local-agent.log').read_text())

    def test_a_bad_queue_wait_value_falls_back_instead_of_failing(self):
        for bad in ('abc', '5', '999999'):
            os.environ['LA_CARGO_QUEUE_WAIT_S'] = bad
            self.assertEqual(la._wait_s(), 3600, bad)
        os.environ.pop('LA_CARGO_QUEUE_WAIT_S')

    def test_missing_runner_still_enforces_the_gates(self):
        la.RUNNER = str(self.dir / 'absent.py')
        self.free = 70.0
        self.assertEqual(self.run_la('build')[0], 75)
        self.size = 70.0
        self.free = 200.0
        self.assertEqual(self.run_la('build')[0], 76)
        self.assertEqual(self.lines('cargo.jsonl'), [])


class Registry(LaCargoBase):
    """ADR 0019: a queued la-cargo records itself for the monitor's guarded recovery; a failure to do so never fails the build."""

    def setUp(self):
        super().setUp()
        self.registry = self.dir / 'wrappers'
        patch_env = unittest.mock.patch.dict(os.environ, {'ADMISSION_WRAPPERS_DIR': str(self.registry)})
        patch_env.start()
        self.addCleanup(patch_env.stop)
        self.seen = []
        real_capture = la._capture

        def spy(cmd, env):
            self.seen.append({p.name: json.loads(p.read_text()) for p in self.registry.glob('q*.json')} if self.registry.exists() else {})
            return real_capture(cmd, env)
        patcher = unittest.mock.patch.object(la, '_capture', spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.use_lib(str(ROOT / 'scripts/admission-recovery.py'))

    def use_lib(self, path, identity='python3 la-cargo'):
        """Point la-cargo at a recovery library; this test process is not a la-cargo script, so its identity is fixed to one."""
        real = spec_from_file_location

        def fake(name, location):
            spec = real(name, location)
            inner = spec.loader

            class Loader:
                def create_module(self, spec):
                    return None

                def exec_module(self, module):
                    inner.exec_module(module)
                    module.exe_identity = lambda pid, run=None, timeout=5: identity
            spec.loader = Loader()
            return spec
        for patcher in (unittest.mock.patch.object(la, 'RECOVERY_LIB', path), unittest.mock.patch.object(la, 'spec_from_file_location', fake)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def left(self):
        return sorted(p.name for p in self.registry.iterdir()) if self.registry.exists() else []

    def test_a_queued_build_is_recorded_while_it_waits_and_removed_after_cargo_exits(self):
        (self.dir / 'mode').write_text('queue')
        os.environ['FAKE_WAITS'] = '2'
        self.assertEqual(self.run_la('build')[0], 0)
        waiting = [snap for snap in self.seen if snap]
        self.assertTrue(waiting, 'the record was never present during the wait')
        record = waiting[0]['q000001.json']
        self.assertEqual((record['qid'], record['kind'], record['pid'], record['exe_id']), ('q000001', 'la-cargo', os.getpid(), 'python3 la-cargo'))
        self.assertEqual(set(record), set(('qid', 'pid', 'start_token', 'pgid', 'ppid', 'exe_id', 'kind', 'created')))
        self.assertEqual(self.left(), [])

    def test_the_record_is_private_and_holds_no_command_line(self):
        (self.dir / 'mode').write_text('queue')
        os.environ['FAKE_WAITS'] = '2'
        seen = []
        real = la._capture

        def spy(cmd, env):
            for p in self.registry.glob('q*.json'):
                seen.append((oct(p.stat().st_mode & 0o777), oct(self.registry.stat().st_mode & 0o777), p.read_text()))
            return real(cmd, env)
        with unittest.mock.patch.object(la, '_capture', spy):
            self.run_la('build', '--release')
        self.assertTrue(seen)
        for file_mode, dir_mode, text in seen:
            self.assertEqual((file_mode, dir_mode), ('0o600', '0o700'))
            self.assertNotIn('--release', text)
            self.assertNotIn('build', text)

    def test_a_build_admitted_at_once_registers_nothing(self):
        self.assertEqual(self.run_la('build')[0], 0)
        self.assertEqual((self.left(), any(self.seen)), ([], False))

    def test_a_timed_out_or_refused_queue_wait_removes_the_record(self):
        (self.dir / 'mode').write_text('refuse')
        ticks = iter(range(0, 10 ** 6, 1000))
        err = io.StringIO()
        orig = la.admitted_build

        def with_clock(a, env, sleep=None, clock=None):
            return orig(a, env, sleep=lambda s: None, clock=lambda: next(ticks))
        with unittest.mock.patch.object(la, 'QUEUE_WAIT_S', 3000), unittest.mock.patch.object(la, 'admitted_build', with_clock), \
                unittest.mock.patch.object(sys, 'argv', ['la-cargo', 'build']), contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(la.main(), 77)
        self.assertTrue(any(self.seen))
        self.assertEqual(self.left(), [])

    def signal_during_registration(self, signum):
        """A signal arriving while the registry is written (an injected slow register); la-cargo's own handlers raise as in production."""
        (self.dir / 'mode').write_text('queue')
        os.environ['FAKE_WAITS'] = '5'
        for sig in (signal.SIGTERM, signal.SIGHUP):
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))
            signal.signal(sig, la._terminate)      # never an inherited ignore: the wrapper's own handler is what is under test
        self.addCleanup(signal.signal, signal.SIGINT, signal.getsignal(signal.SIGINT))
        signal.signal(signal.SIGINT, signal.default_int_handler)      # a background launch inherits SIGINT as ignored, which would make the test a no-op

        def slow_register(qid, kind):
            os.kill(os.getpid(), signum)
            time.sleep(5)
            return None
        with unittest.mock.patch.object(la, '_register', slow_register), self.assertRaises((SystemExit, KeyboardInterrupt)) as raised:
            self.run_la('build')
        return raised.exception

    def test_a_signal_during_registration_still_cancels_the_entry_with_the_same_exit_code(self):
        for signum in (signal.SIGTERM, signal.SIGHUP):
            (self.dir / 'runner.jsonl').unlink(missing_ok=True)
            stopped = self.signal_during_registration(signum)
            self.assertEqual((type(stopped), stopped.code), (SystemExit, 128 + signum))
            self.assertIn(('queue', 'cancel'), self.runner_commands(), signum)
            self.assertEqual(self.lines('cargo.jsonl'), [])

    def test_an_interrupt_during_registration_still_cancels_the_entry(self):
        (self.dir / 'runner.jsonl').unlink(missing_ok=True)
        self.assertIsInstance(self.signal_during_registration(signal.SIGINT), KeyboardInterrupt)
        self.assertIn(('queue', 'cancel'), self.runner_commands())

    def test_unregister_is_the_last_cleanup_step_and_its_failure_changes_nothing(self):
        (self.dir / 'mode').write_text('refuse')
        order = []

        class Lib:
            @staticmethod
            def unregister_wrapper(directory, qid):
                order.append('unregister')
                raise RuntimeError('registry')
        real = la._capture

        def spy(cmd, env):
            if 'cancel' in cmd:
                order.append('cancel')
            return real(cmd, env)
        ticks = iter(range(0, 10 ** 6, 1000))
        orig = la.admitted_build
        with unittest.mock.patch.object(la, '_register', lambda qid, kind: (Lib, '/x')), unittest.mock.patch.object(la, '_capture', spy), \
                unittest.mock.patch.object(la, 'QUEUE_WAIT_S', 3000), \
                unittest.mock.patch.object(la, 'admitted_build', lambda a, e, sleep=None, clock=None: orig(a, e, sleep=lambda s: None, clock=lambda: next(ticks))), \
                unittest.mock.patch.object(sys, 'argv', ['la-cargo', 'build']), contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(la.main(), 77)
        self.assertEqual(order, ['cancel', 'unregister'])

    def test_a_registration_error_of_any_kind_is_ignored(self):
        (self.dir / 'mode').write_text('queue')
        os.environ['FAKE_WAITS'] = '1'
        bad = self.dir / 'odd-recovery.py'
        bad.write_text('class Odd(BaseException):\n    pass\nraise Odd()\n')
        self.use_lib(str(bad))
        self.assertEqual(self.run_la('build')[0], 0)
        self.assertEqual(len(self.lines('cargo.jsonl')), 1)

    def test_a_registry_that_cannot_be_written_never_fails_the_build(self):
        (self.dir / 'mode').write_text('queue')
        os.environ['FAKE_WAITS'] = '1'
        blocker = self.dir / 'blocker'
        blocker.write_text('x')
        with unittest.mock.patch.dict(os.environ, {'ADMISSION_WRAPPERS_DIR': str(blocker / 'wrappers')}):
            self.assertEqual(self.run_la('build')[0], 0)
        self.assertEqual(len(self.lines('cargo.jsonl')), 1)

    def test_a_missing_or_broken_recovery_library_never_fails_the_build(self):
        (self.dir / 'mode').write_text('queue')
        os.environ['FAKE_WAITS'] = '1'
        self.use_lib(str(self.dir / 'does-not-exist.py'))
        self.assertEqual(self.run_la('build')[0], 0)
        bad = self.dir / 'bad-recovery.py'
        bad.write_text('raise RuntimeError("boom")\n')
        self.use_lib(str(bad))
        self.assertEqual(self.run_la('build')[0], 0)
        self.assertEqual(len(self.lines('cargo.jsonl')), 2)
        self.assertEqual(self.left(), [])


class Signals(unittest.TestCase):
    def test_sigterm_and_sighup_unwind_like_an_exit(self):
        import signal
        with self.assertRaises(SystemExit) as raised:
            la._terminate(signal.SIGTERM, None)
        self.assertEqual(raised.exception.code, 128 + signal.SIGTERM)


if __name__ == '__main__':
    unittest.main()
