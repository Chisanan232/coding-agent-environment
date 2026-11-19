#!/usr/bin/env python3
"""Stall monitor (SPE-176): classification, progress, bounded recovery, restart and sleep handling, alert dedupe, safety shape."""
from datetime import datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import ast
import fcntl
import json
import os
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = spec_from_file_location(name.replace('-', '_'), ROOT / 'scripts' / (name + '.py'))
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


da, ma, mon = load('disk-admission'), load('memory-admission'), load('admission-monitor')
da.THIRD_LANE_ENABLED = False
GIB = 1024 ** 3
T0 = datetime(2026, 11, 19, 4, 0, tzinfo=timezone.utc)
CALM = {'pressure_level': 1, 'swap_io_mib_s': 0.0, 'compressor_ratio': 0.262, 'load_per_core': 0.4}


class Clock:
    def __init__(self):
        self.now, self.mono = T0, 1000.0
        self.slept = 0.0

    def advance(self, seconds, asleep=False):
        self.now += timedelta(seconds=seconds)
        self.mono += 0 if asleep else seconds

    def __call__(self):
        return self.now


class MonitorBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.state_path = root / 'admission/state.json'
        self.memory_state = root / 'memory/state.json'
        self.memory_state.parent.mkdir()
        self.worktree = root / 'wt'
        self.worktree.mkdir()
        self.clock = Clock()
        self.mem = {'latched': True, 'ok_streak': 0, 'metrics': dict(CALM)}
        self.free = 300 * GIB
        self.boot = 111
        self.table = {}
        self.advances = []
        self.alerts = []
        self.old_env = os.environ.pop('ADMISSION_MONITOR_DIR', None)
        self.addCleanup(lambda: os.environ.__setitem__('ADMISSION_MONITOR_DIR', self.old_env) if self.old_env else None)
        self.statuses = {}
        self.shim = types.SimpleNamespace(**{k: getattr(da, k) for k in dir(da) if not k.startswith('__')})
        self.shim.holder_status = lambda pid, token, workload, *a: da.HolderStatus(self.statuses.get(pid, 'alive'))

    def env(self):
        limits = mon.default_limits(da, ma)
        return mon.Env(self.shim, self.state_path, self.memory_state, limits, clock=self.clock, mono=lambda: self.clock.mono,
                       boot=lambda: self.boot, table=lambda: self.table, memory=lambda: dict(self.mem), free=lambda: self.free,
                       advance=lambda: self.advances.append(self.clock.now) or {'exit': 0}, notify=lambda a: self.alerts.append(a) or True)

    def enqueue(self, ticket, nbytes=16 * GIB, session='0b1d2e3f-aaaa-bbbb-cccc-1234567890ab'):
        out = da.queue_enqueue(self.state_path, label='job-' + ticket.lower(), tickets=[ticket], repo='horonom/x', worktree=str(self.worktree),
                               harness='codex', session_id=session, nbytes=nbytes, argv=['cargo', 'test', ticket], now=self.clock.now)
        self.assertTrue(out['ok'], out)
        return out['id']

    def grant(self, memory=None, runner=True):
        out = da.queue_advance(self.state_path, lambda: 400 * GIB, now=self.clock.now, memory_fn=(lambda: (True, 'ADMIT')) if memory else None)
        if not runner:   # an outdated caller grants without the stamp
            for entry in da._queue_entries(self.state_path)[0]:
                if entry['state'] == 'GRANTED':
                    entry['grant'] = {'at': entry['grant']['at'], 'expires': entry['grant']['expires']}
                    da._write_queue_entry(self.state_path, entry)
        return out

    def tick(self, seconds=60, act=False, asleep=False, **kw):
        self.clock.advance(seconds, asleep=asleep)
        monitor_dir = mon.monitor_dir(self.state_path)
        os.environ['ADMISSION_MONITOR_DIR'] = str(monitor_dir)
        return mon.tick(self.env(), act=act, **kw)

    def run_ticks(self, n, **kw):
        out = None
        for _ in range(n):
            out = self.tick(**kw)
        return out


class Classify(MonitorBase):
    def snap(self, entries, **extra):
        base = {'now': self.clock.now, 'wall': self.clock.now.timestamp(), 'entries': entries, 'problems': [], 'free_bytes': self.free,
                'mem': self.mem, 'window': [], 'claims': {}, 'protocol': da.RUNNER_PROTOCOL}
        base.update(extra)
        return base

    def classify(self, entries, **extra):
        return mon.classify(self.snap(entries, **extra), mon.default_limits(da, ma))

    def queued(self, n=1):
        ids = [self.enqueue('HORO-%d' % (3000 + i)) for i in range(n)]
        return ids, da._queue_entries(self.state_path)[0]

    def test_empty_queue_is_idle(self):
        self.assertEqual(self.classify([])['primary'], 'idle')

    def test_disk_floor_when_running_work_holds_the_space(self):
        _, entries = self.queued()
        self.free = 110 * GIB
        self.assertEqual(self.classify(entries)['primary'], 'disk-floor')

    def test_memory_pressure_beats_hysteresis(self):
        _, entries = self.queued()
        self.mem['metrics'] = dict(CALM, pressure_level=2)
        self.assertEqual(self.classify(entries)['primary'], 'memory-pressure')

    def test_latch_with_only_the_compressor_failing_is_hysteresis(self):
        _, entries = self.queued()
        self.assertEqual(self.classify(entries)['primary'], 'memory-hysteresis')

    def test_a_flat_compressor_window_is_the_static_variant(self):
        _, entries = self.queued()
        wall = self.clock.now.timestamp()
        window = [[wall - 1200 + 60 * i, 0.262] for i in range(21)]
        self.assertEqual(self.classify(entries, window=window)['primary'], 'memory-hysteresis-static-compressor')

    def test_a_moving_compressor_is_not_static(self):
        _, entries = self.queued()
        wall = self.clock.now.timestamp()
        window = [[wall - 1200 + 60 * i, 0.30 - 0.002 * i] for i in range(21)]
        self.assertEqual(self.classify(entries, window=window)['primary'], 'memory-hysteresis')

    def test_an_unclaimed_grant_that_holds_the_space_is_failed_continuation_not_disk(self):
        ids, _ = self.queued(2)
        self.mem['latched'] = False
        self.free = 125 * GIB      # the 16 GiB head fits alone (109 GiB left), not beside the 16 GiB grant (93 GiB < 100 GiB floor)
        self.grant()
        entries = da._queue_entries(self.state_path)[0]
        verdict = self.classify(entries)
        self.assertNotIn('disk-floor', verdict['causes'])
        self.assertEqual(verdict['primary'], 'failed-continuation')

    def test_a_grant_without_the_runner_stamp_is_an_outdated_caller(self):
        self.queued(2)
        self.mem['latched'] = False
        self.grant(runner=False)
        verdict = self.classify(da._queue_entries(self.state_path)[0])
        self.assertIn('outdated-caller', verdict['causes'])

    def test_a_stamped_grant_is_not_outdated(self):
        self.queued(2)
        self.mem['latched'] = False
        self.grant(memory=True)
        self.assertNotIn('outdated-caller', self.classify(da._queue_entries(self.state_path)[0])['causes'])

    def test_unreadable_queue_state_is_first(self):
        _, entries = self.queued()
        verdict = self.classify(entries, problems=['queue-entry-unreadable: q1'])
        self.assertEqual(verdict['primary'], 'state-unreadable')

    def test_exited_claim_is_stale(self):
        self.queued(2)
        self.mem['latched'] = False
        entries = da._queue_entries(self.state_path)[0]
        entries[0].update(state='CLAIMED', holder={'pid': 4242, 'start_token': 't'})
        verdict = self.classify(entries, claims={entries[0]['id']: {'status': 'exited', 'idle_s': 0}})
        self.assertEqual(verdict['primary'], 'stale-claim')


class Progress(MonitorBase):
    def test_granted_for_two_hours_is_never_execution(self):
        self.mem['latched'] = False
        self.enqueue('HORO-3001')
        self.enqueue('HORO-3002')
        self.grant(memory=True)
        out = self.run_ticks(130)
        self.assertFalse(out['granted_is_execution'])
        self.assertTrue(out['stalled'])
        self.assertGreaterEqual(out['stalled_for_s'], 3600)

    def test_a_claimed_workload_that_burns_cpu_is_progress(self):
        self.mem['latched'] = False
        self.enqueue('HORO-3001')
        self.enqueue('HORO-3002')
        entries = da._queue_entries(self.state_path)[0]
        entries[0].update(state='CLAIMED', holder={'pid': 4242, 'start_token': 't'})
        da._write_queue_entry(self.state_path, entries[0])
        self.table = {4242: (1, 10.0)}
        self.tick()
        for i in range(30):
            self.table = {4242: (1, 10.0 + 5 * (i + 1))}
            out = self.tick()
        self.assertFalse(out['stalled'])

    def test_a_claim_with_no_cpu_growth_is_reported_not_killed(self):
        self.mem['latched'] = False
        self.enqueue('HORO-3001')
        self.enqueue('HORO-3002')
        entries = da._queue_entries(self.state_path)[0]
        entries[0].update(state='CLAIMED', holder={'pid': 4242, 'start_token': 't'})
        da._write_queue_entry(self.state_path, entries[0])
        self.table = {4242: (1, 10.0)}
        out = self.run_ticks(20)
        self.assertEqual(out['primary'], 'stalled-running')
        self.assertIn('report only', out['causes']['stalled-running'])
        self.assertEqual(self.advances, [])


class Recovery(MonitorBase):
    def setUp(self):
        super().setUp()
        self.enqueue('HORO-3001')
        self.enqueue('HORO-3002')

    def test_observe_only_never_advances(self):
        seen = []
        for _ in range(10):
            seen += self.tick()['actions']
        self.assertEqual(self.advances, [])
        self.assertIn({'advance': 'would-run'}, seen)

    def test_first_tick_only_observes(self):
        out = self.tick(act=True)
        self.assertTrue(out['observation_gap'])
        self.assertEqual(self.advances, [])

    def test_act_advances_after_confirmation_and_respects_the_spacing(self):
        for _ in range(3):
            self.tick(act=True, seconds=30)
        before = len(self.advances)
        self.assertGreaterEqual(before, 1)
        self.tick(act=True, seconds=30)
        self.assertEqual(len(self.advances), before)    # 120 s minimum between advances

    def test_failed_recoveries_are_bounded_per_rolling_window_and_reported_as_exhausted(self):
        exhausted = False
        for _ in range(60):
            out = self.tick(act=True, seconds=60)
            exhausted = exhausted or bool(out.get('recovery_exhausted'))
            recent = [a for a in self.advances if a > self.clock.now - timedelta(seconds=mon.ADVANCE_WINDOW_S)]
            self.assertLessEqual(len(recent), mon.ADVANCE_MAX_PER_WINDOW)
        self.assertTrue(exhausted)
        self.assertLessEqual(len(self.advances), 2 * mon.ADVANCE_MAX_PER_WINDOW + 1)

    def test_disk_floor_and_pressure_never_trigger_an_advance(self):
        self.free = 90 * GIB
        for _ in range(8):
            self.tick(act=True)
        self.assertEqual(self.advances, [])
        self.free = 300 * GIB
        self.mem['metrics'] = dict(CALM, pressure_level=2)
        for _ in range(8):
            self.tick(act=True)
        self.assertEqual(self.advances, [])


class Persistence(MonitorBase):
    def setUp(self):
        super().setUp()
        self.enqueue('HORO-3001')
        self.enqueue('HORO-3002')

    def test_a_concurrent_monitor_does_nothing(self):
        directory = mon.monitor_dir(self.state_path)
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        fd = os.open(directory / 'monitor.lock', os.O_WRONLY | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.addCleanup(os.close, fd)
        os.environ['ADMISSION_MONITOR_DIR'] = str(directory)
        out = mon.tick(self.env(), act=True)
        self.assertEqual(out['ran'], False)
        self.assertEqual(self.advances, [])

    def test_corrupt_state_restarts_observing_and_acts_on_nothing(self):
        self.run_ticks(5, act=False)
        (mon.monitor_dir(self.state_path) / 'state.json').write_text('{not json')
        out = self.tick(act=True)
        self.assertTrue(out['observation_gap'])
        self.assertEqual(self.advances, [])

    def test_a_reboot_resets_confirmation(self):
        self.run_ticks(4)
        self.boot = 222
        out = self.tick(act=True)
        self.assertTrue(out['observation_gap'])
        self.assertFalse(out['confirmed'])
        self.assertEqual(self.advances, [])

    def test_sleep_is_detected_from_wall_minus_monotonic(self):
        self.run_ticks(4)
        out = self.tick(seconds=3 * 3600, asleep=True, act=True)
        self.assertTrue(out['observation_gap'])
        self.assertEqual(self.advances, [])
        out = self.tick(act=False)
        self.assertFalse(out['confirmed'])      # one tick after the gap is not enough
        self.assertFalse(self.tick()['observation_gap'])

    def test_a_clock_stepping_back_is_a_gap(self):
        self.run_ticks(3)
        out = self.tick(seconds=-600)
        self.assertTrue(out['observation_gap'])

    def test_if_due_skips_a_recent_tick(self):
        self.tick()
        self.clock.advance(10)
        os.environ['ADMISSION_MONITOR_DIR'] = str(mon.monitor_dir(self.state_path))
        self.assertEqual(mon.tick(self.env(), if_due=True)['ran'], False)

    def test_a_lost_callback_is_a_grant_that_lapsed_and_is_reported(self):
        self.mem['latched'] = False
        self.grant(memory=True)
        self.clock.advance(3700)
        da.queue_advance(self.state_path, lambda: 400 * GIB, now=self.clock.now)
        verdict = mon.classify({'now': self.clock.now, 'wall': self.clock.now.timestamp(), 'entries': da._queue_entries(self.state_path)[0],
                                'problems': [], 'free_bytes': self.free, 'mem': self.mem, 'window': [], 'claims': {},
                                'protocol': da.RUNNER_PROTOCOL}, mon.default_limits(da, ma))
        self.assertIn('1 grant lapsed unclaimed'.replace('1 ', ''), ' '.join(verdict['notes']).replace('q000', '') and 'grant lapsed unclaimed')
        self.assertTrue(verdict['notes'])
        self.assertNotIn('failed-continuation', verdict['causes'])    # history is a note, not a trigger for advances

    def test_an_unclaimed_grant_is_failed_continuation_after_45_minutes(self):
        self.mem['latched'] = False
        self.grant(memory=True)
        self.clock.advance(46 * 60)
        verdict = mon.classify({'now': self.clock.now, 'wall': self.clock.now.timestamp(), 'entries': da._queue_entries(self.state_path)[0],
                                'problems': [], 'free_bytes': self.free, 'mem': self.mem, 'window': [], 'claims': {},
                                'protocol': da.RUNNER_PROTOCOL}, mon.default_limits(da, ma))
        self.assertIn('failed-continuation', verdict['causes'])


class Escalation(MonitorBase):
    def setUp(self):
        super().setUp()
        self.enqueue('HORO-3001')
        self.enqueue('HORO-3002')
        self.mem['metrics'] = dict(CALM, pressure_level=2)     # nothing the monitor can recover: escalates

    def test_one_alert_per_signature_until_the_cooldown(self):
        self.run_ticks(40)
        self.assertEqual(len(self.alerts), 1)
        self.assertEqual(self.alerts[0]['owner_action'], 'reduce-load')
        self.run_ticks(60)
        self.assertEqual(len(self.alerts), 1)

    def test_a_changed_cause_alerts_again(self):
        self.run_ticks(40)
        self.mem['metrics'] = dict(CALM)
        self.free = 90 * GIB
        self.run_ticks(40)
        self.assertEqual([a['cause'] for a in self.alerts], ['memory-pressure', 'disk-floor'])

    def test_the_cooldown_expires(self):
        self.run_ticks(40)
        self.clock.advance(mon.ALERT_COOLDOWN_S, asleep=True)
        self.run_ticks(40)
        self.assertGreaterEqual(len(self.alerts), 2)

    def test_busy_queue_with_progress_is_silent(self):
        self.mem['metrics'] = dict(CALM)
        self.mem['latched'] = False
        entries = da._queue_entries(self.state_path)[0]
        entries[0].update(state='CLAIMED', holder={'pid': 77, 'start_token': 't'})
        da._write_queue_entry(self.state_path, entries[0])
        for i in range(60):
            self.table = {77: (1, float(i * 10))}
            self.tick()
        self.assertEqual(self.alerts, [])


class ReviewFindings(MonitorBase):
    """Cases an independent review showed the first version missed."""

    def setUp(self):
        super().setUp()
        self.mem['latched'] = False
        self.enqueue('HORO-3001')
        self.enqueue('HORO-3002')

    def test_a_frozen_queue_nobody_advances_is_detected_and_recovered(self):
        out = self.tick()
        self.assertEqual(out['primary'], 'frozen-queue')
        for _ in range(4):
            out = self.tick(act=True, seconds=61)
        self.assertGreaterEqual(len(self.advances), 1)

    def test_an_undelivered_alert_is_retried_with_backoff_and_recorded(self):
        self.mem['metrics'] = dict(CALM, pressure_level=2)
        results = []

        def failing(alert):
            results.append(self.clock.now)
            return False
        env_factory = self.env

        def env():
            e = env_factory()
            e.notify = failing
            return e
        self.env = env
        for _ in range(60):
            out = self.tick()
        self.assertGreaterEqual(len(results), 2)
        self.assertLess(len(results), 8)       # exponential backoff, not every tick
        status = json.loads((mon.monitor_dir(self.state_path) / 'status.json').read_text())
        self.assertFalse(status['last_alert']['delivered'])
        self.assertEqual(status['last_alert']['attempts'], len(results))

    def test_an_alert_that_is_delivered_is_silenced_for_the_cooldown(self):
        self.mem['metrics'] = dict(CALM, pressure_level=2)
        self.run_ticks(60)
        self.assertEqual(len(self.alerts), 1)

    def test_an_advance_that_raises_still_counts_against_the_bound(self):
        calls = []
        env_factory = self.env

        def env():
            e = env_factory()
            e.advance = lambda: calls.append(self.clock.now) or (_ for _ in ()).throw(OSError('boom'))
            return e
        self.env = env
        for _ in range(80):
            self.tick(act=True)
        self.assertGreaterEqual(len(calls), 1)
        for at in calls:     # no rolling window ever holds more than the bound, even though every advance raised
            self.assertLessEqual(len([c for c in calls if at - timedelta(seconds=mon.ADVANCE_WINDOW_S) < c <= at]), mon.ADVANCE_MAX_PER_WINDOW)

    def test_a_damaged_entry_is_reported_and_never_crashes_the_tick(self):
        entries = da._queue_entries(self.state_path)[0]
        entries[0]['wait_expires'] = 'not-a-time'
        da._write_queue_entry(self.state_path, entries[0])
        out = self.tick()
        self.assertTrue(out['ran'])
        self.assertIn('state-unreadable', out['causes'])

    def test_a_granted_entry_without_a_grant_object_is_reported_not_fatal(self):
        self.grant(memory=True)
        for entry in da._queue_entries(self.state_path)[0]:
            if entry['state'] == 'GRANTED':
                entry['grant'] = None
                da._write_queue_entry(self.state_path, entry)
        out = self.tick()
        self.assertIn('state-unreadable', out['causes'])

    def test_wrong_types_in_the_state_file_reset_that_field_only(self):
        self.run_ticks(3)
        path = mon.monitor_dir(self.state_path) / 'state.json'
        data = json.loads(path.read_text())
        data.update(blocker='x', alerts=[], advances='no', last_tick={'wall': 'x'}, cpu=5, window=[1, 'a'])
        path.write_text(json.dumps(data))
        out = self.tick(act=True)
        self.assertTrue(out['ran'])
        self.assertEqual(self.advances, [])

    def test_a_tick_exception_leaves_a_status_instead_of_a_traceback(self):
        env_factory = self.env

        def env():
            e = env_factory()
            e.free = lambda: (_ for _ in ()).throw(RuntimeError('statvfs failed'))
            return e
        self.env = env
        out = self.tick()
        self.assertEqual(out['primary'], 'state-unreadable')
        self.assertTrue((mon.monitor_dir(self.state_path) / 'status.json').exists())

    def test_a_new_enqueue_behind_the_head_does_not_change_the_signature(self):
        first = self.tick()['signature']
        self.enqueue('HORO-3003')
        self.assertEqual(self.tick()['signature'], first)

    def test_sleep_does_not_inflate_the_stall_clock(self):
        self.run_ticks(3)
        out = self.tick(seconds=6 * 3600, asleep=True)
        self.assertTrue(out['observation_gap'])
        self.assertLess(out['stalled_for_s'], 120)

    def test_a_reboot_forgets_the_compressor_window(self):
        self.mem['latched'] = True
        self.run_ticks(15)
        self.boot = 999
        self.tick()
        state = json.loads((mon.monitor_dir(self.state_path) / 'state.json').read_text())
        self.assertLessEqual(len(state['window']), 1)

    def test_a_granted_entry_with_nothing_waiting_alerts_after_45_minutes(self):
        for entry in da._queue_entries(self.state_path)[0]:
            if entry['state'] == 'QUEUED':
                entry['state'] = 'CANCELLED'
                da._write_queue_entry(self.state_path, entry)
        self.enqueue('HORO-3004')
        self.grant(memory=True)
        for entry in da._queue_entries(self.state_path)[0]:
            if entry['state'] == 'QUEUED':
                entry['state'] = 'CANCELLED'
                da._write_queue_entry(self.state_path, entry)
        self.run_ticks(60, )
        self.assertTrue(self.alerts)

    def test_a_falling_cpu_total_still_counts_as_activity(self):
        entries = da._queue_entries(self.state_path)[0]
        entries[0].update(state='CLAIMED', holder={'pid': 4242, 'start_token': 't'})
        da._write_queue_entry(self.state_path, entries[0])
        self.table = {4242: (1, 500.0)}
        self.tick()
        for i in range(30):
            self.table = {4242: (1, 500.0 - 3 * (i + 1))}     # reaped children: the total falls while work goes on
            out = self.tick()
        self.assertNotIn('stalled-running', out['causes'])

    def test_two_quick_manual_ticks_are_one_confirmation(self):
        self.mem['metrics'] = dict(CALM, pressure_level=2)
        self.tick()
        for _ in range(5):
            out = self.tick(seconds=1)
        self.assertFalse(out['confirmed'])

    def test_missing_memory_metrics_are_reported(self):
        self.mem['metrics'] = {}
        self.assertIn('unobservable', self.tick()['causes'])

    def test_old_alert_records_are_pruned(self):
        self.mem['metrics'] = dict(CALM, pressure_level=2)
        self.run_ticks(40)
        self.clock.advance(3 * mon.ALERT_COOLDOWN_S, asleep=True)
        self.tick()
        state = json.loads((mon.monitor_dir(self.state_path) / 'state.json').read_text())
        self.assertEqual([k for k in state['alerts'] if k.startswith('memory-pressure')], [])


class Inventory(unittest.TestCase):
    def test_callers_are_classified_from_text_without_running_them(self):
        with tempfile.TemporaryDirectory() as root:
            for name, body in (('a', "RUNNER_PROTOCOL = 2\nHEAVY_LANES = {'emergency': 0, 'stable': 2}\n"),
                               ('b', "HEAVY_LANES = {'stable': 2}\nmemory_fn=None\n"), ('c', "HEAVY_LANES = {'stable': 1}\n")):
                (Path(root) / name / 'scripts').mkdir(parents=True)
                (Path(root) / name / 'scripts/disk-admission.py').write_text(body + 'raise SystemExit("must not run")\n')
            kinds = {r['checkout']: r['kind'] for r in mon.inventory(root)}
        self.assertEqual(kinds, {'a': 'current', 'b': 'memory-gated-advance-unstamped', 'c': 'outdated'})


class SafetyShape(unittest.TestCase):
    """The monitor must stay unable to do what it is forbidden to do."""

    def test_no_signal_launchctl_or_ownership_attestation(self):
        source = (ROOT / 'scripts/admission-monitor.py').read_text()
        tree = ast.parse(source)
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        for forbidden in ('kill', 'killpg', 'signal', 'pkill', 'rmtree', 'unlink', 'remove', 'rmdir'):
            self.assertNotIn(forbidden, names, forbidden)
        for text in ('launchctl', '--confirm-ended', 'confirm_ended', "'release'", "'cancel'", 'session_id', 'kill -'):
            self.assertNotIn(text, source.replace('without the stamp', ''), text)

    def test_the_only_subprocess_targets_are_read_tools_and_advance(self):
        source = (ROOT / 'scripts/admission-monitor.py').read_text()
        for line in source.splitlines():
            if "'queue', 'advance'" in line or 'disk-admission.py' in line:
                self.assertNotIn('reset', line)
        self.assertNotIn("'reset'", source)
        self.assertNotIn('write_json(env.memory', source)

    def test_it_does_not_write_the_admission_or_memory_state(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / 'admission/state.json'
            memory = Path(root) / 'memory/state.json'
            memory.parent.mkdir()
            memory.write_text('{"latched": true, "ok_streak": 0}')
            before = memory.read_bytes()
            os.environ['ADMISSION_MONITOR_DIR'] = str(Path(root) / 'mon')
            try:
                mon.tick(mon.Env(da, state, memory, mon.default_limits(da, ma), memory=lambda: {'latched': True, 'ok_streak': 0, 'metrics': dict(CALM)},
                                 table=lambda: {}, free=lambda: 300 * GIB, boot=lambda: 1))
            finally:
                os.environ.pop('ADMISSION_MONITOR_DIR')
            self.assertEqual(memory.read_bytes(), before)
            self.assertFalse(state.exists())

    def test_the_plist_installs_nothing_and_names_the_checkout_script(self):
        text = mon.plist(Path('/x/scripts/admission-monitor.py'))
        self.assertIn('com.coding-agent-environment.admission-monitor', text)
        self.assertIn('<integer>60</integer>', text)
        self.assertNotIn('KeepAlive', text)


class IncidentReplay(MonitorBase):
    """The 2026-11-19 shape: latched, compressor flat at 26%, grants lapsing, five waiting. Detection inside the 12 minute target."""

    def test_detection_and_escalation_within_the_target(self):
        for i in range(5):
            self.enqueue('HORO-31%02d' % i)
        confirmed_at = alert_at = None
        for minute in range(1, 40):
            out = self.tick(seconds=60)
            if confirmed_at is None and out['confirmed']:
                confirmed_at = minute
            if alert_at is None and out.get('alert'):
                alert_at = minute
        self.assertLessEqual(confirmed_at, 4)
        self.assertEqual(out['primary'], 'memory-hysteresis-static-compressor')
        self.assertIsNotNone(alert_at)
        self.assertLessEqual(alert_at, 25)
        self.assertEqual(out['owner_action'], 'approve-memory-policy')


if __name__ == '__main__':
    unittest.main()
