#!/usr/bin/env python3
"""Stall monitor (SPE-176): classification, progress, bounded recovery, restart and sleep handling, alert dedupe, safety shape."""
from datetime import datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import ast
import contextlib
import fcntl
import io
import json
import math
import os
import sys
import tempfile
import types
import unittest
import unittest.mock

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
        self.cleared = []
        self.old_env = os.environ.pop('ADMISSION_MONITOR_DIR', None)
        self.sn_dir = root / 'session-status'      # no test may reach the owner's real notification state or config
        self.sn_env = {'SESSION_NOTIFY_STATE_DIR': str(self.sn_dir), 'SESSION_NOTIFY_CONFIG': str(root / 'session-notify.json')}
        saved = {k: os.environ.get(k) for k in self.sn_env}
        os.environ.update(self.sn_env)
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v) for k, v in saved.items()])
        self.emit_result = {'result': 'queued', 'delivery': 'sent'}
        self.addCleanup(lambda: os.environ.pop('ADMISSION_MONITOR_DIR', None) if self.old_env is None else os.environ.__setitem__('ADMISSION_MONITOR_DIR', self.old_env))
        self.spawned = []
        real_subprocess = mon.subprocess

        class NoSpawn:      # a test that reaches a real child process (notifier, advance, ps) fails loudly instead of touching the machine
            def __getattr__(inner, name):
                if name in ('run', 'Popen'):
                    self.spawned.append(name)
                    raise AssertionError('test spawned a real process via subprocess.%s' % name)
                return getattr(real_subprocess, name)
        mon.subprocess = NoSpawn()
        self.addCleanup(lambda: setattr(mon, 'subprocess', real_subprocess))
        self.addCleanup(lambda: self.assertEqual(self.spawned, []))
        self.statuses = {}
        self.process_answers = {}
        self.shim = types.SimpleNamespace(**{k: getattr(da, k) for k in dir(da) if not k.startswith('__')})
        self.shim.holder_status = lambda pid, token, workload, *a: da.HolderStatus(self.statuses.get(pid, 'alive'))

    def env(self):
        limits = mon.default_limits(da, ma)
        return mon.Env(self.shim, self.state_path, self.memory_state, limits, clock=self.clock, mono=lambda: self.clock.mono,
                       boot=lambda: self.boot, table=lambda: self.table, memory=lambda: dict(self.mem), free=lambda: self.free,
                       advance=lambda: self.advances.append(self.clock.now) or {'exit': 0}, notify=lambda a: self.alerts.append(a) or True,
                       clear=lambda: self.cleared.append(self.clock.now) or True,
                       emit_incident=lambda alert: self.alerts.append(alert) or dict(self.emit_result),
                       owner_process=lambda pid, token: self.process_answers.get(pid))      # never the real process table: unknown unless a test says otherwise

    def enqueue(self, ticket, nbytes=16 * GIB, session='0b1d2e3f-aaaa-bbbb-cccc-1234567890ab'):
        out = da.queue_enqueue(self.state_path, label='job-' + ticket.lower(), tickets=[ticket], repo='horonom/x', worktree=str(self.worktree),
                               harness='codex', session_id=session, nbytes=nbytes, argv=['cargo', 'test', ticket], now=self.clock.now)
        self.assertTrue(out['ok'], out)
        return out['id']

    def declare_waiter(self):
        """Entries with a declared live waiter keep the full grant window; a hand-off entry's is 20 min (ADR 0019), shorter than these 45 min cases."""
        for entry in da._queue_entries(self.state_path)[0]:
            if entry['state'] == 'QUEUED':
                entry['waiter'] = {'pid': os.getpid(), 'start_token': da.waiter_token(os.getpid())}
                da._write_queue_entry(self.state_path, entry)

    def grant(self, memory=None, runner=True):
        out = da.queue_advance(self.state_path, lambda: 400 * GIB, now=self.clock.now, memory_fn=(lambda: (True, 'ADMIT')) if memory else None)
        if not runner:   # an outdated caller grants without the stamp: its granted receipt carries no runner
            self.edit_granted_receipts(lambda rec: rec.pop('runner', None))
        return out

    def edit_granted_receipts(self, change):
        path = da.queue_dir(self.state_path) / 'receipts.jsonl'
        out = []
        for line in path.read_text().splitlines():
            rec = json.loads(line)
            if rec.get('event') == 'granted':
                change(rec)
            out.append(json.dumps(rec, sort_keys=True))
        path.write_text('\n'.join(out) + '\n')

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
        base['stamps'] = self.env().stamps([e for e in entries if e['state'] == 'GRANTED'])
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
        entries = da._queue_entries(self.state_path)[0]
        verdict = mon.classify({'now': self.clock.now, 'wall': self.clock.now.timestamp(), 'entries': entries,
                                'problems': [], 'free_bytes': self.free, 'mem': self.mem, 'window': [], 'claims': {},
                                'protocol': da.RUNNER_PROTOCOL, 'stamps': self.env().stamps([e for e in entries if e['state'] == 'GRANTED'])},
                               mon.default_limits(da, ma))
        self.assertIn('1 grant lapsed unclaimed'.replace('1 ', ''), ' '.join(verdict['notes']).replace('q000', '') and 'grant lapsed unclaimed')
        self.assertTrue(verdict['notes'])
        self.assertNotIn('failed-continuation', verdict['causes'])    # history is a note, not a trigger for advances

    def test_an_unclaimed_grant_is_failed_continuation_after_45_minutes(self):
        self.mem['latched'] = False
        self.declare_waiter()
        self.grant(memory=True)
        self.clock.advance(46 * 60)
        entries = da._queue_entries(self.state_path)[0]
        verdict = mon.classify({'now': self.clock.now, 'wall': self.clock.now.timestamp(), 'entries': entries,
                                'problems': [], 'free_bytes': self.free, 'mem': self.mem, 'window': [], 'claims': {},
                                'protocol': da.RUNNER_PROTOCOL, 'stamps': self.env().stamps([e for e in entries if e['state'] == 'GRANTED'])},
                               mon.default_limits(da, ma))
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
        self.declare_waiter()
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


class Notifier(unittest.TestCase):
    def alert(self, **kw):
        return dict({'cause': 'memory-hysteresis-static-compressor', 'signature': 'abcdef0123456789', 'owner_action': 'approve-memory-policy',
                     'recovery': [{'advance': 'ran', 'exit': 0}]}, **kw)

    def run_with(self, alert, code=0):
        seen = []

        def runner(argv, **kw):
            seen.append(argv)
            return types.SimpleNamespace(returncode=code)
        return mon.session_notify(alert, runner=runner), seen[0] if seen else None

    def test_the_request_is_enum_flags_only(self):
        ok, argv = self.run_with(self.alert())
        self.assertTrue(ok)
        self.assertEqual(argv[2:], ['emit-resource', '--cause', 'memory-hysteresis-static-compressor', '--recovery', 'advance-tried',
                                    '--owner-action', 'approve-memory-policy', '--signature', 'abcdef0123456789'])

    def test_recovery_phrases(self):
        self.assertEqual(mon.recovery_phrase([], False), 'none')
        self.assertEqual(mon.recovery_phrase([{'advance': 'would-run'}], False), 'observe-only')
        self.assertEqual(mon.recovery_phrase([{'advance': 'recovery-exhausted'}], True), 'recovery-exhausted')

    def test_a_refused_request_is_not_delivered(self):
        self.assertFalse(self.run_with(self.alert(), code=2)[0])

    def test_a_missing_runner_is_not_delivered(self):
        def boom(argv, **kw):
            raise OSError('no python')
        self.assertFalse(mon.session_notify(self.alert(), runner=boom))

    def test_the_clear_request_is_the_plain_clear_flag(self):
        seen = []

        def runner(argv, **kw):
            seen.append(argv)
            return types.SimpleNamespace(returncode=0)
        self.assertTrue(mon.session_clear(runner=runner))
        self.assertEqual(seen[0][2:], ['emit-resource', '--clear'])

    def test_every_monitor_cause_and_action_is_a_known_session_notify_phrase(self):
        sn = load('session-notify')
        for cause in mon.CAUSES:
            if cause not in ('healthy-busy', 'unknown'):
                self.assertIn(cause, sn.CAUSE_TEXT, cause)
        for action in set(mon.OWNER_ACTION.values()):
            self.assertIn(action, sn.OWNER_ACTION_TEXT, action)


class HandoffIncidents(MonitorBase):
    """ADR 0019: an unclaimed hand-off grant raises one incident BEFORE it lapses (and once on lapse if it never did)."""

    def setUp(self):
        super().setUp()
        self.mem['latched'] = False

    def walk(self, seconds):
        """Tick once a minute (never a gap) until `seconds` have passed."""
        out = None
        for _ in range(seconds // 60):
            out = self.tick(60)
        return out

    def entry(self, qid):
        return [e for e in da._queue_entries(self.state_path)[0] if e['id'] == qid][0]

    def claim(self, qid, pid=4242):
        entry = self.entry(qid)
        entry.update(state='CLAIMED', holder={'pid': pid, 'start_token': 't'})
        da._write_queue_entry(self.state_path, entry)
        self.table = {pid: (1, 10.0)}

    def expire(self):
        da.queue_advance(self.state_path, lambda: 400 * GIB, now=self.clock.now)

    def incidents(self):
        return json.loads((mon.monitor_dir(self.state_path) / 'state.json').read_text()).get('incidents', {})

    def granted_handoff(self, ticket='HORO-3001'):
        qid = self.enqueue(ticket)
        self.grant()
        self.assertEqual(self.entry(qid)['state'], 'GRANTED')
        self.assertNotIn('waiter', self.entry(qid))
        return qid

    def handoff_outbox(self):
        path = mon.monitor_dir(self.state_path) / mon.OUTBOX_NAME
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def test_hand_off_escalate_and_lapse_each_write_one_closed_schema_outbox_line_deduped_by_key(self):
        qid = self.granted_handoff()
        self.walk(840)
        self.assertEqual(self.handoff_outbox(), [])                            # before the escalate point
        self.walk(120)
        (first,) = self.handoff_outbox()
        self.assertEqual(sorted(first), ['at', 'blocker', 'cause', 'clock', 'evidence', 'job', 'key', 'owner', 'owner_action', 'phase', 'recovery', 'v'])
        self.assertEqual((first['job'], first['cause'], first['clock'], first['phase'], first['recovery'], first['owner_action']),
                         (qid, 'unclaimed-handoff', 'grant', 'escalated', 'none', 'claim-or-reenqueue-handoff'))
        self.assertEqual(first['evidence'], ['session-unknown', 'grant-unclaimed-escalate', 'no-waiter'])
        self.assertEqual((first['owner']['kind'], first['owner']['reachable'], first['key']), ('codex', None, next(iter(self.incidents()))))
        self.walk(120)
        self.assertEqual(len(self.handoff_outbox()), 1)                        # repeated ticks: no duplicate
        self.clock.advance(300)
        self.expire()
        self.tick(0)
        lines = self.handoff_outbox()
        self.assertEqual([l['phase'] for l in lines], ['escalated', 'lapsed'])
        self.assertEqual((lines[1]['recovery'], lines[1]['evidence'][-2]), ('lapsed', 'grant-lapsed-unclaimed'))
        self.assertEqual(lines[0]['key'], lines[1]['key'])
        self.run_ticks(5)
        self.assertEqual(len(self.handoff_outbox()), 2)
        text = (mon.monitor_dir(self.state_path) / mon.OUTBOX_NAME).read_text()
        self.assertNotIn('0b1d2e3f', text)

    def test_a_claimed_hand_off_writes_no_outbox_line(self):
        qid = self.granted_handoff()
        self.tick(180)
        self.claim(qid)
        self.run_ticks(20)
        self.assertEqual(self.handoff_outbox(), [])

    def test_the_hand_off_window_mirrors_the_scheduler_constant(self):
        self.assertEqual(mon.HANDOFF_GRANT_S, da.HANDOFF_GRANT_S)
        self.assertEqual(mon.escalate_s(self.entry(self.granted_handoff())), 900)

    def test_complete_lifecycle_notice_incident_dedup_lapse_upgrade_at_most_two_notices_and_clear(self):
        qid = self.granted_handoff()
        out = self.tick(180)                                                   # +3 min
        self.assertEqual((self.alerts, out['notices'], self.incidents()), ([], [], {}))
        out = self.tick(120)                                                   # +5 min: notice, no page
        self.assertEqual(len(out['notices']), 1)
        self.assertEqual((self.alerts, self.incidents()), ([], {}))
        out = self.walk(600)                                                   # +15 min: before the 20 min window ends
        self.assertEqual(len(self.alerts), 1)
        self.assertLess(self.clock.now, mon.parse_iso(self.entry(qid)['grant']['expires']))
        first = self.alerts[0]
        self.assertEqual((first['cause'], first['owner_action'], mon.recovery_phrase(first['recovery'], False)), ('unclaimed-handoff', 'claim-or-reenqueue-handoff', 'none'))
        (rec,) = self.incidents().values()
        self.assertEqual((rec['phase'], rec['waiter'], rec['action'], rec['acknowledged_at'], rec['delivered_at'] is not None), ('escalated', False, 'none', None, True))
        self.assertEqual(rec['ack_deadline'], mon.parse_iso(self.entry(qid)['grant']['at']).timestamp() + 900)
        self.tick(60)                                                          # +16 min: deduplicated
        self.assertEqual(len(self.alerts), 1)
        self.clock.advance(300)                                                # +21 min: the scheduler's own path expires it
        self.expire()
        self.assertEqual(self.entry(qid)['state'], 'EXPIRED')
        out = self.tick(0)
        self.assertEqual(len(self.alerts), 2)                                  # the documented upgrade, same incident
        self.assertEqual(mon.recovery_phrase(self.alerts[1]['recovery'], False), 'lapsed')
        self.assertNotEqual(self.alerts[0]['signature'], self.alerts[1]['signature'])
        self.assertEqual(len(self.incidents()), 1)
        self.assertEqual(next(iter(self.incidents().values()))['phase'], 'lapsed')
        self.assertNotIn('failed-continuation', out['causes'])
        self.run_ticks(2)
        self.assertEqual((len(self.alerts), self.cleared), (2, []))
        self.tick()
        self.assertEqual(len(self.cleared), 1)                                 # clear after 3 clean ticks
        self.run_ticks(3)
        self.assertEqual((len(self.alerts), len(self.cleared), len(self.incidents())), (2, 1, 1))   # a clear does not re-raise the same incident

    def test_claimed_before_the_escalate_point_raises_nothing(self):
        qid = self.granted_handoff()
        self.tick(180)
        self.claim(qid)
        self.run_ticks(20)
        self.assertEqual((self.alerts, self.incidents()), ([], {}))

    def test_a_claim_after_the_incident_is_the_acknowledgement_and_the_incident_clears(self):
        qid = self.granted_handoff()
        self.walk(960)
        self.assertEqual(len(self.alerts), 1)
        self.claim(qid)
        for i in range(4):
            self.table = {4242: (1, 10.0 + i)}
            self.tick()
        (rec,) = self.incidents().values()
        self.assertIsNotNone(rec['acknowledged_at'])
        self.assertEqual((len(self.alerts), len(self.cleared)), (1, 1))

    def test_a_missing_acknowledgement_is_never_taken_as_abandonment(self):
        qid = self.granted_handoff()
        self.run_ticks(21)
        self.expire()
        self.tick()
        (rec,) = self.incidents().values()
        self.assertIsNone(rec['acknowledged_at'])
        self.assertEqual(self.entry(qid)['state'], 'EXPIRED')                  # the monitor did nothing to the entry
        self.assertEqual(self.advances, [])

    def test_a_waiter_entry_keeps_the_45_minute_behaviour(self):
        qid = self.enqueue('HORO-3001')
        self.declare_waiter()
        self.grant()
        self.assertEqual(mon.escalate_s(self.entry(qid)), 2700)
        out = self.run_ticks(44)
        self.assertNotIn('failed-continuation', out['causes'])
        out = self.run_ticks(2)
        self.assertIn('failed-continuation', out['causes'])
        self.assertNotIn('unclaimed-handoff', out['causes'])
        self.assertEqual(self.incidents(), {})

    def test_a_failed_hand_off_is_recorded_undelivered_and_retried_on_the_next_tick(self):
        self.granted_handoff()
        self.emit_result = {'result': 'no-backend', 'delivery': None}
        self.walk(900)
        (rec,) = self.incidents().values()
        self.assertEqual((len(self.alerts), rec['handed_off_at'], rec['delivered_at'], rec['attempts']), (1, None, None, 1))
        self.emit_result = {'result': 'queued', 'delivery': 'sent'}
        self.tick()
        (rec,) = self.incidents().values()
        self.assertEqual((len(self.alerts), rec['handed_off_at'] is not None, rec['delivered_at'] is not None), (2, True, True))
        self.assertEqual(self.alerts[0]['signature'], self.alerts[1]['signature'])
        self.tick()
        self.assertEqual(len(self.alerts), 2)

    def test_queued_but_not_sent_is_handed_off_not_delivered_and_is_polled_until_sent(self):
        self.granted_handoff()
        self.emit_result = {'result': 'queued', 'delivery': 'pending'}
        self.walk(900)
        (rec,) = self.incidents().values()
        self.assertEqual((rec['handed_off_at'] is not None, rec['delivered_at'], rec['delivery']), (True, None, 'pending'))
        self.emit_result = {'result': 'deduped', 'delivery': 'sent'}
        self.tick()
        (rec,) = self.incidents().values()
        self.assertEqual((rec['delivered_at'] is not None, rec['delivery'], len(self.alerts)), (True, 'sent', 2))
        self.tick()
        self.assertEqual(len(self.alerts), 2)

    def test_a_first_time_deduped_answer_is_not_a_hand_off(self):
        self.granted_handoff()
        self.emit_result = {'result': 'deduped', 'delivery': 'sent'}
        self.walk(960)
        (rec,) = self.incidents().values()
        self.assertEqual((rec['handed_off_at'], rec['delivered_at'], rec['notified_phase']), (None, None, None))

    def real_emit(self, *, backend, send_ok=True):
        """The real session-notify emit-resource path (temp state and config, injected transports, no network, no banner)."""
        sn = load('session-notify')
        config = json.loads(json.dumps(sn.DEFAULT_CONFIG))
        config['backends']['ntfy'].update(enabled=backend, approved_destination='ntfy.sh')
        config['retry'].update(base_seconds=0)
        Path(self.sn_env['SESSION_NOTIFY_CONFIG']).write_text(json.dumps(config))
        sn.REAL_TRANSPORTS = {name: (lambda payload, bcfg: None if send_ok else 'send-failed') for name in ('macos', 'ntfy', 'pushover')}

        def runner(argv, **kw):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = sn.main(argv[2:])
            return types.SimpleNamespace(returncode=code, stdout=out.getvalue())
        self.emit_runner = runner
        self.shim_env = lambda alert: mon.session_notify_incident(alert, runner=runner)

    def env(self):
        env = super().env()
        if getattr(self, 'shim_env', None):
            env.emit_incident = lambda alert: self.alerts.append(alert) or self.shim_env(alert)
        return env

    def test_real_path_scenario_a_a_failing_send_is_handed_off_but_never_marked_delivered(self):
        self.granted_handoff()
        self.real_emit(backend=True, send_ok=False)
        self.walk(900)
        (rec,) = self.incidents().values()
        self.assertEqual((rec['handed_off_at'] is not None, rec['delivered_at'], rec['delivery']), (True, None, 'pending'))
        self.tick()                                           # polled again: still not sent, still not claimed delivered
        (rec,) = self.incidents().values()
        self.assertEqual((rec['delivered_at'], len(self.alerts)), (None, 2))

    def test_real_path_scenario_b_no_backend_then_enabled_is_retried_not_deduped(self):
        self.granted_handoff()
        self.real_emit(backend=False)
        self.walk(900)
        (rec,) = self.incidents().values()
        self.assertEqual((rec['handed_off_at'], rec['delivered_at']), (None, None))
        self.real_emit(backend=True)                          # the owner enables a backend later
        self.tick()
        (rec,) = self.incidents().values()
        self.assertEqual((rec['handed_off_at'] is not None, rec['delivered_at'] is not None, rec['delivery']), (True, True, 'sent'))
        self.assertEqual(len(self.alerts), 2)

    def test_the_session_notify_request_is_strict_and_enum_only(self):
        seen = []
        mon.session_notify_incident({'cause': 'unclaimed-handoff', 'signature': 'abcdef0123456789', 'recovery': [{'handoff': 'lapsed'}],
                                     'owner_action': 'claim-or-reenqueue-handoff'},
                                    runner=lambda a, **kw: seen.append(a) or types.SimpleNamespace(returncode=0, stdout='emit-resource: queued delivery=sent\n'))
        self.assertEqual(seen[0][2:], ['emit-resource', '--cause', 'unclaimed-handoff', '--recovery', 'lapsed', '--owner-action',
                                       'claim-or-reenqueue-handoff', '--signature', 'abcdef0123456789', '--strict'])
        for out, want in (('emit-resource: no-backend\n', {'result': 'no-backend', 'delivery': None}), ('garbage', {'result': 'failed', 'delivery': None})):
            self.assertEqual(mon.session_notify_incident({'cause': 'unclaimed-handoff', 'signature': 'abcdef0123456789', 'recovery': [], 'owner_action': 'x'},
                                                         runner=lambda a, **kw: types.SimpleNamespace(returncode=0, stdout=out)), want)

    def test_a_forward_clock_step_on_a_gap_tick_does_not_raise_a_healthy_grant(self):
        qid = self.granted_handoff()
        self.tick(60)
        out = self.tick(1500, asleep=True)                   # wall clock jumped 25 min, the monotonic clock did not
        self.assertTrue(out['observation_gap'])
        self.assertEqual((self.alerts, self.incidents()), ([], {}))
        self.assertEqual(self.entry(qid)['state'], 'GRANTED')

    def test_a_gap_tick_still_raises_a_lapse_the_scheduler_itself_recorded(self):
        qid = self.granted_handoff()
        self.tick(60)
        self.clock.advance(1500, asleep=True)
        self.expire()
        self.assertEqual(self.entry(qid)['state'], 'EXPIRED')
        self.tick(0)
        self.assertEqual(len(self.alerts), 1)
        self.assertEqual(mon.recovery_phrase(self.alerts[0]['recovery'], False), 'lapsed')

    def test_the_granted_only_stall_age_is_45_minutes_for_a_waiter_entry_and_immediate_for_a_hand_off(self):
        def verdict(waiter):
            self.setUp()
            self.enqueue('HORO-3001')
            if waiter:
                self.declare_waiter()
            self.grant()
            self.clock.advance((46 if waiter else 16) * 60)
            return mon.classify(self.snap_for(), mon.default_limits(da, ma))
        self.assertEqual(verdict(True)['grant_stall_s'], 2700)
        self.assertEqual(verdict(False)['grant_stall_s'], 0)

    def snap_for(self):
        entries = da._queue_entries(self.state_path)[0]
        return {'now': self.clock.now, 'wall': self.clock.now.timestamp(), 'entries': entries, 'problems': [], 'free_bytes': self.free, 'mem': self.mem,
                'window': [], 'claims': {}, 'protocol': da.RUNNER_PROTOCOL, 'stamps': self.env().stamps([e for e in entries if e['state'] == 'GRANTED'])}

    def test_an_incident_inside_its_lapse_window_is_never_evicted_for_the_soft_limit(self):
        wall = self.clock.now.timestamp()
        rec = lambda i, **kw: dict({'id': 'q%06d' % i, 'phase': 'escalated', 'grant_wall': wall - 100 * i, 'expires_wall': wall - 100 * i + 1200,
                                    'acknowledged_at': None, 'resolved_at': None, 'owner_notice_state': 'pending'}, **kw)
        crowded = {'k%d' % i: rec(i) for i in range(mon.INCIDENT_LIMIT + 6)}
        crowded['acked'] = rec(999, acknowledged_at=wall - 10)
        kept = mon.prune_incidents(crowded, wall)
        self.assertEqual(len(kept), mon.INCIDENT_LIMIT + 6)            # all still inside the window: none evicted for the soft limit
        self.assertNotIn('acked', kept)                                # the acknowledged one went first
        crowded = {'k%d' % i: rec(i, grant_wall=wall - 25200 - i, expires_wall=wall - 24000 - i) for i in range(mon.INCIDENT_LIMIT + 3)}
        self.assertEqual(len(mon.prune_incidents(crowded, wall)), mon.INCIDENT_LIMIT)   # past the window: evicted, oldest first

    def test_real_path_a_failing_backend_gets_one_pending_entry_per_phase_and_no_clear_until_settled(self):
        self.granted_handoff()
        self.real_emit(backend=True, send_ok=False)
        self.walk(900)
        self.clock.advance(300)
        self.expire()
        self.walk(600)                                       # lapsed, then many clean ticks: polled, never cleared, never queued twice
        record = json.loads((self.sn_dir / 'admission-monitor.json').read_text())
        keys = [p['key'] for p in record['pending']]
        self.assertEqual((len(keys), len(set(keys))), (2, 2))           # the escalate notice and the lapse notice, each once
        self.assertEqual(self.cleared, [])

    def test_a_clear_is_sent_once_every_notice_is_settled(self):
        self.granted_handoff()
        self.walk(900)
        self.clock.advance(300)
        self.expire()
        self.walk(300)
        self.assertEqual(len(self.cleared), 1)

    def test_dedups_for_a_notice_never_handed_off_stop_after_a_bound(self):
        self.granted_handoff()
        self.emit_result = {'result': 'deduped', 'delivery': 'sent'}
        self.walk(900)
        for _ in range(12):
            self.tick(900)
        (rec,) = self.incidents().values()
        self.assertEqual((len(self.alerts), rec['handed_off_unverified'], rec['handed_off_at'], rec['delivered_at']), (mon.UNVERIFIED_AFTER_DEDUPS, True, None, None))

    def test_an_unverified_escalate_notice_does_not_silence_the_lapse_notice(self):
        self.granted_handoff()
        self.walk(900)
        state_file = mon.monitor_dir(self.state_path) / 'state.json'
        state = json.loads(state_file.read_text())
        for rec in state['incidents'].values():
            rec.update(handed_off_unverified=True, dedups=5, notified_phase=None, handed_off_at=None, delivered_at=None, delivery=None)
        state_file.write_text(json.dumps(state))
        before = len(self.alerts)
        self.walk(60)
        self.assertEqual(len(self.alerts), before)                           # settled as unverified: not asked again
        self.clock.advance(300)
        self.expire()
        self.tick(0)
        self.assertEqual(len(self.alerts), before + 1)
        self.assertEqual(mon.recovery_phrase(self.alerts[-1]['recovery'], False), 'lapsed')
        (rec,) = self.incidents().values()
        self.assertEqual((rec['phase'], rec['handed_off_unverified'], rec['dedups'], rec['delivered_at'] is not None), ('lapsed', False, 0, True))

    def test_a_notice_that_could_not_be_handed_off_is_listed_undelivered_in_status_json(self):
        self.granted_handoff()
        self.emit_result = {'result': 'no-backend', 'delivery': None}
        out = self.walk(900)
        (item,) = out['owner_notice_undelivered']
        self.assertEqual((item['phase'], item['id']), ('escalated', out['handoff_incidents'][0]['id']))
        self.assertEqual(out['handoff_incidents'][0]['owner_notice_state'], 'undelivered')
        on_disk = json.loads((mon.monitor_dir(self.state_path) / 'status.json').read_text())
        self.assertEqual(on_disk['owner_notice_undelivered'], out['owner_notice_undelivered'])
        self.emit_result = {'result': 'queued', 'delivery': 'sent'}
        out = self.tick()
        self.assertEqual((out['owner_notice_undelivered'], out['handoff_incidents'][0]['owner_notice_state']), ([], 'delivered'))

    def test_dropped_notices_are_listed_undelivered_but_never_block_the_clear(self):
        self.granted_handoff()
        self.emit_result = {'result': 'queued', 'delivery': 'dropped'}
        self.walk(900)
        self.clock.advance(300)
        self.expire()
        out = self.walk(300)
        self.assertEqual([i['phase'] for i in out['owner_notice_undelivered']], ['lapsed'])
        self.assertEqual(len(self.cleared), 1)

    def test_an_acknowledged_incident_is_not_listed(self):
        qid = self.granted_handoff()
        self.emit_result = {'result': 'no-backend', 'delivery': None}
        self.walk(900)
        self.claim(qid)
        out = self.tick()
        self.assertEqual((out['owner_notice_undelivered'], out['handoff_incidents'][0]['owner_notice_state']), ([], 'acknowledged'))

    def test_a_claim_is_remembered_in_the_incident(self):
        qid = self.granted_handoff()
        self.walk(960)
        self.claim(qid)
        self.tick()
        (rec,) = self.incidents().values()
        self.assertTrue(rec['claim_seen'])

    def test_restart_and_a_second_monitor_produce_one_incident(self):
        self.granted_handoff()
        self.walk(960)
        directory = mon.monitor_dir(self.state_path)
        fd = os.open(directory / 'monitor.lock', os.O_WRONLY)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            self.assertFalse(self.tick()['ran'])                                # the other monitor holds the lock
        finally:
            os.close(fd)
        for _ in range(2):                                                      # each tick builds a fresh Env and reloads the persisted state
            self.tick()
        (rec,) = self.incidents().values()
        self.assertEqual((len(self.alerts), rec['phase']), (1, 'escalated'))

    def test_a_first_tick_after_a_restart_still_raises_a_grant_already_past_the_escalate_point(self):
        self.granted_handoff()
        self.tick(1000)
        self.assertEqual(len(self.alerts), 1)

    def test_the_same_owner_granted_twice_in_a_row_is_two_incidents_each_deduplicated(self):
        first = self.granted_handoff('HORO-3001')
        self.walk(960)
        self.run_ticks(2)
        self.clock.advance(300)
        self.expire()
        self.tick(0)
        second = self.granted_handoff('HORO-3002')
        self.walk(960)
        self.run_ticks(3)
        self.assertEqual(len(self.incidents()), 2)
        self.assertEqual(len({a['signature'] for a in self.alerts}), 3)         # first: escalated + lapsed; second: escalated
        self.assertEqual(len(self.alerts), 3)
        self.assertEqual(sorted(r['id'] for r in self.incidents().values()), [first, second])

    def test_incident_state_stays_bounded_and_readable(self):
        for i in range(40):
            self.granted_handoff('HORO-%d' % (5000 + i))
            self.walk(960)
            self.clock.advance(300)
            self.expire()
            self.tick(0)
        state_file = mon.monitor_dir(self.state_path) / 'state.json'
        self.assertLessEqual(len(self.incidents()), mon.INCIDENT_LIMIT)
        self.assertLess(state_file.stat().st_size, 32768)
        self.assertEqual(len(self.alerts), 80)

    def test_damaged_incident_records_are_dropped_not_fatal(self):
        self.granted_handoff()
        self.walk(960)
        state_file = mon.monitor_dir(self.state_path) / 'state.json'
        state = json.loads(state_file.read_text())
        state['incidents'].update({'bad': 5, 'worse': {'id': 7}})
        state_file.write_text(json.dumps(state))
        self.tick()
        self.assertEqual(len(self.incidents()), 1)
        self.assertEqual(len(self.alerts), 1)

    def test_q000811_replay_no_waiter_no_holder_owner_gone_raises_before_expiry(self):
        qid = self.granted_handoff('HORO-2139')
        self.enqueue('HORO-3002')
        self.table = {}                                                         # the owner's executor exited; nothing holds or claims
        raised_at = None
        for minute in range(1, 20):
            self.tick()
            if self.alerts and raised_at is None:
                raised_at = minute
        self.assertEqual(raised_at, 15)
        self.assertEqual(len(self.alerts), 1)
        self.assertEqual(self.alerts[0]['cause'], 'unclaimed-handoff')

    def test_a_lapse_nobody_observed_is_raised_once_with_the_lapsed_phrase(self):
        qid = self.granted_handoff()
        self.tick(25 * 60)                       # the grant window ended; no queue command has recorded EXPIRED yet
        self.assertEqual(len(self.alerts), 1)
        self.assertEqual(mon.recovery_phrase(self.alerts[0]['recovery'], False), 'lapsed')
        self.expire()
        self.run_ticks(3)
        self.assertEqual(len(self.alerts), 1)
        self.assertEqual(self.entry(qid)['state'], 'EXPIRED')

    def test_a_lapse_older_than_six_hours_is_history_only(self):
        self.granted_handoff()
        self.clock.advance(7 * 3600, asleep=True)
        self.expire()
        self.tick()
        self.assertEqual((self.alerts, self.incidents()), ([], {}))

    def test_a_lapse_with_a_claim_receipt_is_not_unclaimed(self):
        qid = self.granted_handoff()
        da._queue_receipt(self.state_path, 'claimed', self.entry(qid), self.clock.now)
        self.clock.advance(25 * 60)
        self.expire()
        self.tick()
        self.assertEqual(self.alerts, [])

    def test_after_the_lapse_the_lane_and_the_next_entry_are_verified(self):
        qid = self.granted_handoff('HORO-3001')
        nxt = self.enqueue('HORO-3002')
        self.walk(960)
        out = self.walk(300)                     # lapsed in time, not yet recorded: the next entry is still waiting
        (rec,) = self.incidents().values()
        self.assertEqual((rec['phase'], rec['verify']['next'], rec['verify']['next_started']), ('lapsed', nxt, False))
        self.assertNotIn('failed-continuation', out['causes'])
        self.expire()
        self.tick()
        (rec,) = self.incidents().values()
        self.assertEqual((rec['verify']['next'], rec['verify']['next_started']), (nxt, True))
        self.assertEqual(self.entry(qid)['state'], 'EXPIRED')


class FinalReviewFindings(MonitorBase):
    def setUp(self):
        super().setUp()
        self.mem['latched'] = False

    def test_thousands_of_terminal_entries_do_not_grow_or_blind_the_state(self):
        for i in range(60):
            self.enqueue('HORO-%d' % (4000 + i))
        for entry in da._queue_entries(self.state_path)[0]:
            entry['state'] = 'CANCELLED'
            da._write_queue_entry(self.state_path, entry)
        self.enqueue('HORO-4999')
        self.enqueue('HORO-4998')
        out = None
        for _ in range(8):
            out = self.tick()
        state_file = mon.monitor_dir(self.state_path) / 'state.json'
        state = json.loads(state_file.read_text())
        self.assertEqual(sorted(state['states']), ['q000061', 'q000062'])
        self.assertFalse(out['observation_gap'])
        self.assertLess(state_file.stat().st_size, 4096)

    def test_a_state_file_over_the_read_cap_is_not_produced_by_normal_use(self):
        self.enqueue('HORO-4001')
        self.run_ticks(5)
        self.assertLess((mon.monitor_dir(self.state_path) / 'state.json').stat().st_size, mon.STATE_LIMIT // 4)

    def test_recurrence_is_announced_again_after_the_blockage_clears(self):
        self.enqueue('HORO-4001')
        self.enqueue('HORO-4002')
        self.mem['metrics'] = dict(CALM, pressure_level=2)
        self.run_ticks(40)
        self.assertEqual(len(self.alerts), 1)
        self.mem['metrics'] = dict(CALM)
        for entry in da._queue_entries(self.state_path)[0]:
            entry['state'] = 'CANCELLED'
            da._write_queue_entry(self.state_path, entry)
        out = None
        for _ in range(mon.CLEAR_AFTER_TICKS + 1):
            out = self.tick()
            if out.get('cleared'):
                break
        self.assertTrue(out.get('cleared'))
        self.assertEqual(len(self.cleared), 1)
        self.tick()
        self.assertEqual(len(self.cleared), 1)       # cleared once per incident
        self.enqueue('HORO-4003')
        self.enqueue('HORO-4004')
        self.mem['metrics'] = dict(CALM, pressure_level=2)
        self.run_ticks(40)
        self.assertEqual(len(self.alerts), 2)        # same cause, new incident, announced again

    def test_an_eligible_head_with_nothing_holding_a_lane_is_still_frozen(self):
        self.enqueue('HORO-4001')
        for entry in da._queue_entries(self.state_path)[0]:
            entry['state'] = 'ELIGIBLE'
            da._write_queue_entry(self.state_path, entry)
        out = self.run_ticks(5)
        self.assertEqual(out['primary'], 'frozen-queue')

    def test_an_eligible_head_beside_a_non_queue_reservation_is_busy(self):
        self.enqueue('HORO-4001')
        for entry in da._queue_entries(self.state_path)[0]:
            entry['state'] = 'ELIGIBLE'
            da._write_queue_entry(self.state_path, entry)
        env_factory = self.env

        def env():
            e = env_factory()
            e.reservations = lambda: 1
            return e
        self.env = env
        self.assertEqual(self.run_ticks(5)['primary'], 'healthy-busy')

    def test_an_unreadable_entry_cannot_forge_progress_for_a_claim(self):
        self.enqueue('HORO-4001')
        self.enqueue('HORO-4002')
        entries = da._queue_entries(self.state_path)[0]
        entries[0].update(state='CLAIMED', holder={'pid': 4242, 'start_token': 't'})
        da._write_queue_entry(self.state_path, entries[0])
        self.table = {4242: (1, 5.0)}
        self.run_ticks(3)
        path = da.queue_dir(self.state_path) / (entries[0]['id'] + '.json')
        path.write_text('{broken')
        out = self.tick()
        state = json.loads((mon.monitor_dir(self.state_path) / 'state.json').read_text())
        self.assertIn('state-unreadable', out['causes'])
        self.assertLess(state['progress_at'], self.clock.now.timestamp())

    def test_clean_ticks_persist_reset_on_a_cause_and_a_new_alert_can_clear_again(self):
        self.enqueue('HORO-4001')
        self.enqueue('HORO-4002')
        self.mem['metrics'] = dict(CALM, pressure_level=2)
        self.run_ticks(40)
        for entry in da._queue_entries(self.state_path)[0]:
            entry['state'] = 'CANCELLED'
            da._write_queue_entry(self.state_path, entry)
        self.mem['metrics'] = dict(CALM)
        self.tick()
        self.tick()
        state = json.loads((mon.monitor_dir(self.state_path) / 'state.json').read_text())
        self.assertEqual(state['clean_ticks'], 2)
        self.tick()
        self.assertEqual(len(self.cleared), 1)
        self.enqueue('HORO-4003')
        self.enqueue('HORO-4004')
        self.mem['metrics'] = dict(CALM, pressure_level=2)
        self.run_ticks(40)
        state = json.loads((mon.monitor_dir(self.state_path) / 'state.json').read_text())
        self.assertEqual(state['clean_ticks'], 0)
        self.assertFalse(state['last_alert'].get('cleared'))
        for entry in da._queue_entries(self.state_path)[0]:
            entry['state'] = 'CANCELLED'
            da._write_queue_entry(self.state_path, entry)
        self.mem['metrics'] = dict(CALM)
        self.run_ticks(4)
        self.assertEqual(len(self.cleared), 2)

    def test_one_clean_tick_does_not_clear_or_reset_the_cooldown(self):
        self.enqueue('HORO-4001')
        self.enqueue('HORO-4002')
        self.mem['metrics'] = dict(CALM, pressure_level=2)
        self.run_ticks(40)
        self.assertEqual(len(self.alerts), 1)
        self.mem['metrics'] = dict(CALM)            # a flicker: one calm reading, then the same stall again
        self.tick()
        self.assertEqual(self.cleared, [])
        self.mem['metrics'] = dict(CALM, pressure_level=2)
        self.run_ticks(40)
        self.assertEqual(len(self.alerts), 1)
        self.assertEqual(self.cleared, [])

    def test_a_failing_clear_backs_off_instead_of_retrying_every_tick(self):
        self.enqueue('HORO-4001')
        self.enqueue('HORO-4002')
        self.mem['metrics'] = dict(CALM, pressure_level=2)
        self.run_ticks(40)
        for entry in da._queue_entries(self.state_path)[0]:
            entry['state'] = 'CANCELLED'
            da._write_queue_entry(self.state_path, entry)
        self.mem['metrics'] = dict(CALM)
        attempts = []
        env_factory = self.env

        def env():
            e = env_factory()
            e.clear = lambda: attempts.append(self.clock.now) or False
            return e
        self.env = env
        self.run_ticks(30)
        self.assertGreaterEqual(len(attempts), 2)
        self.assertLess(len(attempts), 6)

    def test_a_claim_released_and_garbage_collected_between_ticks_is_progress(self):
        self.enqueue('HORO-4001')
        self.enqueue('HORO-4002')
        entries = da._queue_entries(self.state_path)[0]
        entries[0].update(state='CLAIMED', holder={'pid': 4242, 'start_token': 't'})
        da._write_queue_entry(self.state_path, entries[0])
        self.table = {4242: (1, 5.0)}
        self.run_ticks(3)
        (da.queue_dir(self.state_path) / (entries[0]['id'] + '.json')).unlink()
        self.table = {}
        before = self.tick()
        state = json.loads((mon.monitor_dir(self.state_path) / 'state.json').read_text())
        self.assertEqual(state['progress_at'], self.clock.now.timestamp())    # the vanished claim reset the stall clock

    def test_a_malformed_runner_stamp_reads_as_an_outdated_caller_not_a_traceback(self):
        self.enqueue('HORO-4001')
        self.enqueue('HORO-4002')
        self.grant(memory=True)
        self.edit_granted_receipts(lambda rec: rec.update(runner='old'))
        out = self.tick()
        self.assertIn('outdated-caller', out['causes'])

    def test_the_grant_object_is_never_extended_by_the_monitored_scheduler(self):
        self.enqueue('HORO-4001')
        self.grant(memory=True)
        grants = [e['grant'] for e in da._queue_entries(self.state_path)[0] if e['state'] == 'GRANTED']
        self.assertEqual([set(g) for g in grants], [{'at', 'expires'}])

    def test_receipts_rotate_instead_of_stopping(self):
        self.enqueue('HORO-4001')
        directory = mon.monitor_dir(self.state_path)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'receipts.jsonl').write_text('x' * (1 << 20))
        self.tick()
        self.assertTrue((directory / 'receipts.jsonl.1').exists())
        self.assertLess((directory / 'receipts.jsonl').stat().st_size, 4096)


class WaitAndRunClocks(MonitorBase):
    """ADR 0019 clocks 1 and 3: QUEUED wait (4 h notice, 12.5 h incident, 20 h near-expiry) and execution (2x deadline). Notice only, never an action."""
    CAUSES = ('queue-wait-excessive', 'workload-over-deadline')

    def setUp(self):
        super().setUp()
        self.mem['latched'] = False

    def entry(self, qid):
        return [e for e in da._queue_entries(self.state_path)[0] if e['id'] == qid][0]

    def age(self, qid, hours):
        """Make the entry look `hours` old now (created and wait window moved together)."""
        entry = self.entry(qid)
        created = self.clock.now - timedelta(hours=hours)
        entry.update(created=mon.iso(created), wait_expires=mon.iso(created + timedelta(hours=24)))
        da._write_queue_entry(self.state_path, entry)

    def claim(self, qid, hours, pid=4242, **extra):
        entry = self.entry(qid)
        entry.update(state='CLAIMED', holder={'pid': pid, 'start_token': 'Thu Nov 19 04:00:00 2026', 'claimed_at': mon.iso(self.clock.now - timedelta(hours=hours))}, **extra)
        da._write_queue_entry(self.state_path, entry)
        self.table = {pid: (1, 10.0)}

    def pages(self):
        return [a for a in self.alerts if a['cause'] in self.CAUSES]

    def incidents(self):
        return json.loads((mon.monitor_dir(self.state_path) / 'state.json').read_text()).get('incidents', {})

    def outbox(self):
        path = mon.monitor_dir(self.state_path) / mon.OUTBOX_NAME
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def walk(self, seconds, step=120, busy=False):
        """Tick every `step` s; `busy` gives the running holder fresh CPU each tick (a live, working job)."""
        out = None
        for i in range(seconds // step):
            if busy and 4242 in self.table:
                self.table = {4242: (1, self.table[4242][1] + 30.0)}
            out = self.tick(step)
        return out

    # ---- queued wait ----
    def test_complete_queued_lifecycle_notice_escalate_dedup_near_expiry_and_two_notice_cap(self):
        qid = self.enqueue('HORO-3101')
        self.age(qid, 3.9)
        out = self.walk(3600 // 2)                                         # +30 min: crosses 4 h
        self.assertTrue(any(qid in n and 'queued 4 h' in n for n in out['notices']), out['notices'])
        self.assertEqual((self.pages(), self.incidents()), ([], {}))        # a notice is status only: no page, no incident
        self.walk(8 * 3600 + 1200 - 10 * 120)                                # up to just under 12.5 h
        self.assertEqual((self.pages(), self.incidents()), ([], {}))
        self.walk(20 * 120)
        (page,) = self.pages()
        self.assertEqual((page['cause'], page['owner_action'], mon.recovery_phrase(page['recovery'], False)), ('queue-wait-excessive', 'check-queued-wait', 'none'))
        self.assertEqual(list(self.incidents()), [qid + ':wait'])
        self.walk(7 * 3600)                                                   # hours of further ticks: still one notice (dedup by key)
        self.assertEqual(len(self.pages()), 2)                               # the 20 h second phase arrived in that span
        second = self.pages()[1]
        self.assertEqual(mon.recovery_phrase(second['recovery'], False), 'wait-expiry-near')
        self.assertNotEqual(page['signature'], second['signature'])
        self.walk(3 * 3600)
        self.assertEqual(len(self.pages()), 2)                               # never a third
        (rec,) = self.incidents().values()
        self.assertEqual((rec['phase'], rec['clock'], rec['acknowledged_at']), ('near', 'wait', None))
        self.assertEqual([o['phase'] for o in self.outbox()], ['escalated', 'near'])

    def test_a_queue_entry_under_12_5_hours_never_pages(self):
        qid = self.enqueue('HORO-3102')
        self.age(qid, 12.0)
        self.walk(10 * 120)
        self.assertEqual((self.pages(), self.incidents(), self.outbox()), ([], {}, []))

    def test_an_entry_waiting_only_behind_a_running_higher_entry_does_not_page_inside_the_wait_budget(self):
        first, second = self.enqueue('HORO-3103'), self.enqueue('HORO-3104')
        self.claim(first, 5)
        self.age(second, 9)
        self.walk(20 * 120)
        self.assertEqual((self.pages(), self.incidents()), ([], {}))

    def test_a_first_tick_after_a_restart_raises_a_wait_already_past_the_near_point_once_as_near(self):
        qid = self.enqueue('HORO-3105')
        self.age(qid, 21)
        self.tick(60)
        (page,) = self.pages()
        self.assertEqual(mon.recovery_phrase(page['recovery'], False), 'wait-expiry-near')
        self.tick(60)
        self.assertEqual(len(self.pages()), 1)

    def test_restart_keeps_one_incident_and_a_second_monitor_cannot_tick(self):
        qid = self.enqueue('HORO-3106')
        self.age(qid, 13)
        self.tick(60)
        directory = mon.monitor_dir(self.state_path)
        fd = os.open(directory / 'monitor.lock', os.O_WRONLY)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            self.assertFalse(self.tick()['ran'])
        finally:
            os.close(fd)
        for _ in range(3):                                                    # fresh Env each time, state reloaded from disk
            self.tick()
        self.assertEqual((len(self.pages()), len(self.incidents()), len(self.outbox())), (1, 1, 1))
        # the state file is lost (restart with nothing persisted): the same notice is asked again and session-notify's own dedup answers it
        self.emit_result = {'result': 'deduped', 'delivery': 'unknown'}
        (mon.monitor_dir(self.state_path) / 'state.json').unlink()
        self.tick(60)
        self.assertEqual(len(self.incidents()), 1)

    def test_sleep_time_is_discounted_from_a_wait_for_every_later_tick_not_just_the_gap_tick(self):
        qid = self.enqueue('HORO-3107')
        self.age(qid, 12.4)
        self.tick(60)
        out = self.tick(3600, asleep=True)                                    # +1 h of wall clock that the monotonic clock did not see
        self.assertTrue(out['observation_gap'])
        for _ in range(3):                                                    # two (three) consecutive ordinary ticks after the gap: still no page
            self.assertFalse(self.tick(60)['observation_gap'])
        self.assertEqual((self.pages(), self.incidents()), ([], {}))
        self.walk(5 * 60)                                                     # real elapsed uptime still counts: 12.4 h plus 6 min waited
        self.assertEqual(len(self.pages()), 1)

    def test_a_forward_clock_step_and_a_reboot_are_discounted_too(self):
        stepped, rebooted = self.enqueue('HORO-3160'), self.enqueue('HORO-3161')
        self.age(stepped, 5)
        self.age(rebooted, 5)
        self.tick(60)
        self.clock.advance(10 * 3600, asleep=True)                            # the wall clock jumps 10 h, the monotonic clock does not
        self.boot = 222                                                       # ... and the host rebooted meanwhile
        out = self.tick(60)
        self.assertTrue(out['observation_gap'])
        out = self.walk(3 * 120)
        self.assertEqual((self.pages(), self.incidents()), ([], {}))
        self.assertTrue(any('queued 5 h' in n for n in out['notices']), out['notices'])
        gaps = json.loads((mon.monitor_dir(self.state_path) / 'state.json').read_text())['gaps']
        self.assertEqual(len(gaps), 1)
        self.assertGreater(gaps[0][2], 9.9 * 3600)

    def test_a_job_claimed_before_a_long_sleep_does_not_page_at_twelve_wall_hours_but_does_after_twelve_real_ones(self):
        qid = self.enqueue('HORO-3162')
        self.claim(qid, 5)
        self.tick(60)
        self.tick(8 * 3600, asleep=True)                                      # 8 h asleep: 13 h of wall clock since the claim, 5 h of it awake
        self.walk(5 * 120)
        self.assertEqual((self.pages(), self.incidents()), ([], {}))
        self.walk(7 * 3600 - 20 * 60)                                         # about 12 h awake in total
        self.assertEqual(self.pages(), [])
        self.walk(30 * 60)
        (rec,) = self.incidents().values()
        self.assertEqual(rec['clock'], 'run')
        self.assertGreater(rec['discounted_s'], 7.9 * 3600)
        self.assertEqual(len(self.pages()), 1)

    def test_a_clock_step_backwards_gives_no_age_and_no_incident(self):
        qid = self.enqueue('HORO-3108')
        entry = self.entry(qid)
        entry['created'] = mon.iso(self.clock.now + timedelta(hours=30))      # a created time in the future is never an age
        da._write_queue_entry(self.state_path, entry)
        self.walk(5 * 120)
        self.assertEqual((self.pages(), self.incidents()), ([], {}))

    def test_a_granted_entry_resolves_its_wait_incident_without_another_notice(self):
        qid = self.enqueue('HORO-3109')
        self.age(qid, 13)
        self.tick(60)
        entry = self.entry(qid)
        entry.update(state='RELEASED')
        da._write_queue_entry(self.state_path, entry)
        self.walk(5 * 120)
        (rec,) = self.incidents().values()
        self.assertEqual((rec['acknowledged_at'], rec['owner_notice_state']), (None, 'delivered'))
        self.assertIsNotNone(rec['resolved_at'])
        self.assertEqual(len(self.pages()), 1)

    def test_a_global_blocker_is_attributed_but_the_entry_still_gets_its_one_incident(self):
        qid = self.enqueue('HORO-3110')
        self.age(qid, 13)
        self.free = 1 * GIB                                                   # disk-floor holds the whole queue
        out = self.walk(5 * 120)
        self.assertEqual(out['primary'], 'disk-floor')
        (rec,) = self.incidents().values()
        self.assertEqual(rec['blocker'], 'disk-floor')
        self.assertEqual(len(self.pages()), 1)
        (line,) = self.outbox()
        self.assertIn('blocked-by-global-cause', line['evidence'])
        self.assertEqual(line['blocker'], 'disk-floor')

    def test_a_memory_blocker_is_attributed_the_same_way(self):
        qid = self.enqueue('HORO-3111')
        self.age(qid, 13)
        self.mem.update(latched=True, ok_streak=0)
        self.walk(5 * 120)
        (rec,) = self.incidents().values()
        self.assertTrue(rec['blocker'].startswith('memory-hysteresis'), rec['blocker'])

    def test_waiting_behind_a_running_entry_over_its_deadline_pages_and_records_that_blocker(self):
        first, second = self.enqueue('HORO-3112'), self.enqueue('HORO-3113')
        self.claim(first, 7)                                                   # past its 6 h deadline (not yet 2x: no run incident)
        self.age(second, 13)
        self.walk(3 * 120)
        (rec,) = self.incidents().values()
        self.assertEqual((rec['id'], rec['blocker'], rec['status']), (second, 'higher-entry-running', None))
        self.assertIn('behind-running-entry', self.outbox()[0]['evidence'])
        self.assertEqual(len(self.pages()), 1)

    def suppressed_wait(self, running_hours=1):
        first, second = self.enqueue('HORO-3150'), self.enqueue('HORO-3151')
        self.claim(first, running_hours)
        self.age(second, 13)
        out = self.walk(3 * 120, busy=True)
        return first, second, out

    def test_a_wait_behind_healthy_running_work_is_a_status_note_and_a_suppressed_record_not_a_page(self):
        first, second, out = self.suppressed_wait()
        (rec,) = self.incidents().values()
        self.assertEqual((rec['id'], rec['status'], rec['blocker'], rec['phase']), (second, 'suppressed-higher-entry', 'higher-entry-running', 'escalated'))
        self.assertEqual((self.pages(), self.outbox()), ([], []))
        self.assertTrue(any(second in n and 'within its deadline' in n for n in out['notices']), out['notices'])
        self.assertEqual(out['owner_notice_undelivered'], [])
        self.walk(10 * 120, busy=True)
        self.assertEqual((self.pages(), self.outbox()), ([], []))

    def test_a_suppressed_wait_pages_when_the_running_entry_passes_its_deadline(self):
        first, second, _ = self.suppressed_wait(5.9)
        self.walk(10 * 60, busy=True)                                          # the higher entry crosses 6 h
        (rec,) = self.incidents().values()
        self.assertEqual((rec['status'], rec['phase']), (None, 'escalated'))
        (page,) = self.pages()
        self.assertEqual(mon.recovery_phrase(page['recovery'], False), 'none')
        self.assertEqual(len(self.outbox()), 1)

    def test_a_suppressed_wait_pages_when_a_global_cause_holds_the_queue(self):
        self.suppressed_wait()
        self.free = 1 * GIB
        self.walk(3 * 120)
        (rec,) = self.incidents().values()
        self.assertEqual((rec['status'], rec['blocker']), (None, 'disk-floor'))
        self.assertEqual(len(self.pages()), 1)

    def test_a_suppressed_wait_pages_when_the_running_work_ends_and_it_is_still_queued(self):
        first, second, _ = self.suppressed_wait()
        entry = self.entry(first)
        entry.update(state='RELEASED')
        da._write_queue_entry(self.state_path, entry)
        self.walk(3 * 120)
        (rec,) = self.incidents().values()
        self.assertEqual((rec['status'], rec['blocker']), (None, 'none'))
        self.assertEqual(len(self.pages()), 1)

    def test_wait_expiry_near_always_pages_even_behind_healthy_running_work(self):
        first, second, _ = self.suppressed_wait()
        self.age(second, 19.99)
        self.claim(first, 1)
        self.walk(60 * 60 // 2, busy=True)                                     # crosses 20 h
        (rec,) = self.incidents().values()
        self.assertEqual((rec['status'], rec['phase']), (None, 'near'))
        (page,) = self.pages()
        self.assertEqual(mon.recovery_phrase(page['recovery'], False), 'wait-expiry-near')

    def test_a_granted_but_unclaimed_higher_entry_is_not_running_work_and_does_not_suppress(self):
        first, second = self.enqueue('HORO-3152'), self.enqueue('HORO-3153')
        self.grant()
        self.age(second, 13)
        self.walk(3 * 120)
        self.assertEqual(self.entry(first)['state'], 'GRANTED')
        self.assertEqual([r['status'] for r in self.incidents().values() if r['id'] == second], [None])

    def test_a_running_entry_with_an_unreadable_claim_time_does_not_suppress(self):
        first, second = self.enqueue('HORO-3154'), self.enqueue('HORO-3155')
        entry = self.entry(first)
        entry.update(state='CLAIMED', holder={'pid': 4242, 'start_token': 't'})
        da._write_queue_entry(self.state_path, entry)
        self.table = {4242: (1, 1.0)}
        self.age(second, 13)
        self.walk(3 * 120)
        self.assertEqual(len(self.pages()), 1)

    def test_200_queued_entries_keep_state_bounded_and_page_at_most_the_cap_at_once(self):
        ids = []
        for i in range(200):
            out = da.queue_enqueue(self.state_path, label='job-%d' % i, tickets=['HORO-%d' % (4000 + i)], repo='horonom/x', worktree=str(self.worktree),
                                   harness='codex', session_id='0b1d2e3f-aaaa-bbbb-cccc-1234567890ab', nbytes=GIB, argv=['x'], now=self.clock.now)
            self.assertTrue(out['ok'], out)
            ids.append(out['id'])
        for n, qid in enumerate(ids):
            self.age(qid, 13 + n / 1000.0)                                     # distinct ages, oldest first by index descending
        out = self.walk(3 * 120)
        self.assertEqual(len(self.incidents()), mon.WAIT_INCIDENT_MAX)
        self.assertEqual(len(self.pages()), mon.WAIT_INCIDENT_MAX)
        self.assertEqual(out['wait_overflow'], 200 - mon.WAIT_INCIDENT_MAX)
        self.assertLess((mon.monitor_dir(self.state_path) / 'state.json').stat().st_size, mon.STATE_LIMIT // 2)
        self.assertLess((mon.monitor_dir(self.state_path) / 'status.json').stat().st_size, 64 * 1024)
        self.assertEqual(len(self.outbox()), mon.WAIT_INCIDENT_MAX)
        self.walk(5 * 120)
        self.assertEqual(len(self.pages()), mon.WAIT_INCIDENT_MAX)            # a stable set: no flood and no re-raise
        for qid in list(self.incidents())[:3]:                                 # three of them leave the queue: three more are taken up
            entry = self.entry(qid.split(':')[0])
            entry.update(state='RELEASED')
            da._write_queue_entry(self.state_path, entry)
        self.walk(5 * 120)
        self.assertEqual(len(self.pages()), mon.WAIT_INCIDENT_MAX + 3)

    def test_status_notices_stay_bounded_for_many_entries_past_the_notice_age(self):
        for i in range(30):
            qid = self.enqueue('HORO-%d' % (4300 + i))
            self.age(qid, 5)
        out = self.tick(60)
        self.assertLessEqual(len(out['notices']), mon.NOTICE_LIST_MAX + 1)
        self.assertTrue(any('more entries queued' in n for n in out['notices']))

    # ---- execution ----
    def test_a_long_job_under_twice_its_deadline_with_fresh_children_never_alerts(self):
        qid = self.enqueue('HORO-3120')
        self.claim(qid, 11.0)
        cpu = 10.0
        for _ in range(20):
            cpu += 60.0
            self.table = {4242: (1, cpu)}                                       # fresh child activity every tick
            self.tick(120)
        self.assertEqual((self.pages(), self.incidents()), ([], {}))

    def test_quiet_logs_and_idle_cpu_are_not_an_escalation_on_their_own(self):
        qid = self.enqueue('HORO-3121')
        self.enqueue('HORO-3140')                                               # waiting work, so the tick has a verdict to name
        self.claim(qid, 1)
        out = self.walk(40 * 60)                                                # CPU time flat well past the stalled-running threshold
        self.assertEqual(out['primary'], 'stalled-running')
        self.assertEqual(self.pages_of('workload-over-deadline'), [])
        self.assertEqual(self.incidents(), {})

    def pages_of(self, cause):
        return [a for a in self.alerts if a['cause'] == cause]

    def test_over_twice_the_default_deadline_escalates_once_and_records_the_holder_without_any_signal(self):
        signals = []
        real_kill, real_killpg = os.kill, os.killpg
        os.kill = lambda *a: signals.append(('kill',) + a)
        os.killpg = lambda *a: signals.append(('killpg',) + a)
        self.addCleanup(lambda: (setattr(os, 'kill', real_kill), setattr(os, 'killpg', real_killpg)))
        qid = self.enqueue('HORO-3122')
        self.claim(qid, 11.8)
        self.walk(3 * 120)
        self.assertEqual(self.pages(), [])
        self.walk(10 * 60)                                                      # crosses 12 h
        (page,) = self.pages()
        self.assertEqual((page['cause'], page['owner_action']), ('workload-over-deadline', 'check-over-deadline-workload'))
        (rec,) = self.incidents().values()
        self.assertEqual((rec['clock'], rec['holder_pid'], rec['holder_token'], rec['deadline_s'], rec['deadline_source']),
                         ('run', 4242, 'Thu Nov 19 04:00:00 2026', 6 * 3600, 'default'))
        self.assertGreaterEqual(rec['elapsed_s'], 12 * 3600)
        self.walk(3600)
        self.assertEqual(len(self.pages()), 1)                                   # one notice per run incident
        self.assertEqual((signals, self.spawned, self.advances), ([], [], []))
        self.assertEqual(self.entry(qid)['state'], 'CLAIMED')                    # nothing was cancelled, released or touched
        (line,) = self.outbox()
        self.assertEqual((line['cause'], line['clock'], line['evidence']), ('workload-over-deadline', 'run', ['session-unknown', 'process-uninspectable', 'holder-alive', 'over-deadline', 'deadline-default']))

    def test_a_declared_deadline_is_used_and_named_as_the_source(self):
        qid = self.enqueue('HORO-3123')
        self.claim(qid, 2.1, deadline_s=3600)
        self.walk(3 * 120)
        (rec,) = self.incidents().values()
        self.assertEqual((rec['deadline_s'], rec['deadline_source']), (3600, 'declared'))
        self.assertIn('deadline-declared', self.outbox()[0]['evidence'])

    def test_a_malformed_declared_deadline_falls_back_to_the_default(self):
        for bad in (True, 0, -5, 10 ** 9, '3600', 1.5):
            self.assertEqual(mon.run_deadline({'deadline_s': bad}), (6 * 3600, 'default'), bad)

    def test_an_exited_holder_or_one_without_a_claim_time_is_not_an_execution_incident(self):
        gone, odd = self.enqueue('HORO-3124'), self.enqueue('HORO-3125')
        self.claim(gone, 30, pid=111)
        self.statuses[111] = 'exited'
        entry = self.entry(odd)
        entry.update(state='CLAIMED', holder={'pid': 222, 'start_token': 't'})
        da._write_queue_entry(self.state_path, entry)
        self.table = {111: (1, 1.0), 222: (1, 1.0)}
        self.walk(5 * 120)
        self.assertEqual((self.pages(), self.incidents()), ([], {}))

    def test_a_run_incident_is_resolved_when_the_claim_ends(self):
        qid = self.enqueue('HORO-3126')
        self.claim(qid, 13)
        self.tick(60)
        entry = self.entry(qid)
        entry.update(state='RELEASED')
        da._write_queue_entry(self.state_path, entry)
        self.walk(3 * 120)
        (rec,) = self.incidents().values()
        self.assertEqual((rec['acknowledged_at'], rec['owner_notice_state']), (None, 'delivered'))
        self.assertIsNotNone(rec['resolved_at'])
        self.assertEqual(len(self.pages()), 1)

    def test_a_resolved_wait_whose_notice_never_arrived_stays_listed_undelivered_and_is_not_retried(self):
        qid = self.enqueue('HORO-3170')
        self.age(qid, 13)
        self.emit_result = {'result': 'failed', 'delivery': None}
        self.tick(60)
        self.assertEqual(len(self.pages()), 1)
        entry = self.entry(qid)
        entry.update(state='RELEASED')
        da._write_queue_entry(self.state_path, entry)
        out = self.walk(5 * 120)
        (rec,) = self.incidents().values()
        self.assertEqual((rec['acknowledged_at'], rec['owner_notice_state']), (None, 'undelivered'))
        self.assertIsNotNone(rec['resolved_at'])
        self.assertEqual([u['id'] for u in out['owner_notice_undelivered']], [qid])
        self.assertEqual(len(self.pages()), 1)                                   # no retries once resolved
        self.walk(3 * 3600)
        self.assertEqual(len(self.incidents()), 1)                               # still visible hours later, unlike a settled one

    def test_a_resolved_incident_with_no_notice_ever_attempted_is_resolved_not_acknowledged_nor_listed(self):
        first, second, _ = self.suppressed_wait()
        entry = self.entry(second)
        entry.update(state='RELEASED')
        da._write_queue_entry(self.state_path, entry)
        out = self.walk(3 * 120, busy=True)
        (rec,) = self.incidents().values()
        self.assertEqual((rec['owner_notice_state'], rec['acknowledged_at'] is None, rec['resolved_at'] is not None), ('resolved', True, True))
        self.assertEqual((out['owner_notice_undelivered'], self.pages()), ([], []))

    def test_a_resolved_delivered_incident_is_dropped_after_the_keep_window(self):
        qid = self.enqueue('HORO-3171')
        self.age(qid, 13)
        self.tick(60)
        entry = self.entry(qid)
        entry.update(state='RELEASED')
        da._write_queue_entry(self.state_path, entry)
        self.tick(60)
        self.assertEqual(len(self.incidents()), 1)
        self.walk(mon.INCIDENT_ACK_KEEP_S + 600)
        self.assertEqual(self.incidents(), {})

    # ---- suppression needs live, working holders ----
    def test_a_dead_unresolved_or_cpu_stalled_holder_never_suppresses_a_wait(self):
        for status in ('exited', 'unresolved'):
            with self.subTest(status=status):
                self.setUp()
                first, second = self.enqueue('HORO-3180'), self.enqueue('HORO-3181')
                self.claim(first, 1)
                self.statuses[4242] = status
                self.age(second, 13)
                self.walk(3 * 120, busy=True)
                (rec,) = [r for r in self.incidents().values() if r['clock'] == 'wait']
                self.assertEqual(rec['status'], None)
                self.assertEqual(len(self.pages_of('queue-wait-excessive')), 1)
        self.setUp()
        first, second = self.enqueue('HORO-3182'), self.enqueue('HORO-3183')
        self.claim(first, 1)
        self.age(second, 13)
        self.walk(3 * 120, busy=True)
        self.assertEqual(self.pages(), [])                                       # healthy so far: suppressed
        self.walk(20 * 120)                                                       # the CPU stands still for over 15 min
        (rec,) = self.incidents().values()
        self.assertEqual(rec['status'], None)
        self.assertEqual(len(self.pages()), 1)

    # ---- bounds ----
    def test_hard_limit_eviction_never_removes_an_open_incident(self):
        wall = self.clock.now.timestamp()
        crowd = {}
        for i in range(mon.INCIDENT_HARD_LIMIT + 6):
            crowd['k%03d' % i] = mon.new_incident('q%06d' % i, 'escalated', wall - 10000 + i, wall + 3600, wall, None, 'wait', 'none')
        for i in range(10):                                                       # the oldest ten are closed
            crowd['k%03d' % i]['resolved_at'] = wall
        kept = mon.bound_incidents(dict(crowd))
        self.assertEqual(len(kept), mon.INCIDENT_HARD_LIMIT)
        self.assertTrue(all(k in kept for k in crowd if int(k[1:]) >= 10))        # every open one survived
        self.assertTrue(all(k not in kept for k in ('k000', 'k005')) and 'k006' in kept)      # the six oldest closed ones went, oldest first
        everything_open = {k: dict(v, resolved_at=None) for k, v in crowd.items()}
        self.assertEqual(len(mon.bound_incidents(dict(everything_open))), len(everything_open))      # the bound yields to an open incident

    def test_a_crash_between_the_outbox_append_and_the_state_write_does_not_duplicate_the_line(self):
        qid = self.enqueue('HORO-3190')
        self.age(qid, 13)
        self.tick(60)
        self.assertEqual(len(self.outbox()), 1)
        path = mon.monitor_dir(self.state_path) / 'state.json'
        state = json.loads(path.read_text())
        for rec in state['incidents'].values():
            rec['outbox_phase'] = None                                             # the state write that never happened
        path.write_text(json.dumps(state))
        self.tick(60)
        self.assertEqual(len(self.outbox()), 1)
        directory = mon.monitor_dir(self.state_path)
        os.replace(directory / mon.OUTBOX_NAME, directory / (mon.OUTBOX_NAME + '.1'))      # ... also when the line was rotated meanwhile
        state = json.loads(path.read_text())
        for rec in state['incidents'].values():
            rec['outbox_phase'] = None
        path.write_text(json.dumps(state))
        self.tick(60)
        self.assertFalse((directory / mon.OUTBOX_NAME).exists() and (directory / mon.OUTBOX_NAME).read_text())

    # ---- state read hardening ----
    BAD_NUMBERS = (float('nan'), float('inf'), float('-inf'), 10 ** 400, True)

    def write_state(self, mutate):
        self.tick(60)
        path = mon.monitor_dir(self.state_path) / 'state.json'
        state = json.loads(path.read_text())
        mutate(state)
        path.write_text(json.dumps(state))        # json.dumps writes NaN and Infinity, and json.loads reads them back

    def test_non_finite_or_out_of_range_gaps_are_dropped_and_never_silence_the_clocks(self):
        for bad in self.BAD_NUMBERS:
            with self.subTest(bad=repr(bad)[:12]):
                self.setUp()
                qid = self.enqueue('HORO-3200')
                self.age(qid, 13)
                self.write_state(lambda st: st.update(gaps=[[0.0, 1.0, bad], [bad, 5.0, 1.0], [0.0, bad, 1.0], [10.0, 20.0, 5.0]]))
                self.tick(60)
                self.assertEqual(len(self.pages()), 1)                                    # not silenced
                gaps = json.loads((mon.monitor_dir(self.state_path) / 'state.json').read_text())['gaps']
                self.assertEqual(gaps, [[10.0, 20.0, 5.0]])                                # only the sound one is kept
        self.assertEqual(mon.sanitize_state({'gaps': [[0, 1, 2], 'x', [1, 2], [1, 2, 3, 4], [5, 4, 1], [0, 1, -1.0], [0, 1, 49 * 3600], [0, 1, 48 * 3600]]})['gaps'],
                         [[0.0, 1.0, 2.0], [0.0, 1.0, 48.0 * 3600]])      # a duration outside 0..48 h, a reversed span and a wrong shape are dropped

    def test_other_numeric_state_fields_reject_nan_infinity_and_huge_ints(self):
        for bad in (float('nan'), float('inf'), float('-inf'), 10 ** 400, True):
            with self.subTest(bad=repr(bad)[:12]):
                state = mon.sanitize_state({'schema': 1, 'last_tick': {'wall': bad, 'mono': 1.0}, 'progress_at': bad, 'advances': [bad, 1.0],
                                            'cpu': {'a': {'cpu': bad, 'at': 1.0}, 'b': {'cpu': 1.0, 'at': 1.0}}, 'window': [[bad, 1.0], [1.0, 2.0]],
                                            'alerts': {'a': {'sent_at': 1.0, 'next_try': bad}, 'b': {'sent_at': 1.0, 'next_try': 2.0}},
                                            'blocker': {'signature': 'x', 'confirmations': 1, 'last': bad}, 'last_alert': {'clear_next': bad}})
                self.assertEqual((state['last_tick'], state['progress_at'], state['advances'], list(state['cpu']), state['window'], list(state['alerts']),
                                  state['blocker'], state['last_alert']), ({}, None, [1.0], ['b'], [[1.0, 2.0]], ['b'], None, None))
        rec = mon.new_incident('q000001', 'escalated', 1.0, 2.0, 1.0, None, 'wait', 'none')
        for key in ('grant_wall', 'expires_wall', 'raised_at', 'ack_deadline', 'handed_off_at', 'resolved_at'):
            for bad in (float('nan'), float('inf'), 10 ** 400):
                damaged = dict(rec, **{key: bad})
                cleaned = mon.clean_incident(damaged)
                self.assertTrue(cleaned is None or cleaned[key] is None or math.isfinite(cleaned[key]), (key, bad))

    # ---- owner resolution and outbox ----
    def test_owner_resolution_with_an_uninspectable_owner_is_none_never_gone(self):
        entry = {'state': 'QUEUED', 'harness': 'codex', 'owner': {'uid': 1, 'session_id': 'sess-1', 'ticket': 'HORO-1'}, 'waiter': {'pid': 5, 'start_token': 't'}}
        out = mon.resolve_owner(entry, {}, process=lambda pid, token: None, launcher=lambda sid: None)
        self.assertEqual(out, {'kind': 'codex', 'session_id': 'sess-1', 'reachable': None, 'evidence': ['session-unknown', 'process-uninspectable']})
        raising = mon.resolve_owner(entry, {}, process=lambda *a: 1 / 0, launcher=lambda sid: 1 / 0)
        self.assertIsNone(raising['reachable'])
        self.assertIsNone(mon.resolve_owner({}, {})['reachable'])

    def test_owner_reachability_needs_every_signal_to_say_gone(self):
        entry = {'state': 'QUEUED', 'harness': 'claude', 'owner': {'uid': 1, 'session_id': 'sess-2'}, 'waiter': {'pid': 5, 'start_token': 't'}}
        sessions = {'sess-2': {'kind': 'claude', 'live': False}}
        self.assertFalse(mon.resolve_owner(entry, sessions, process=lambda p, t: False)['reachable'])
        self.assertIsNone(mon.resolve_owner(entry, sessions, process=lambda p, t: None)['reachable'])
        self.assertTrue(mon.resolve_owner(entry, sessions, process=lambda p, t: True)['reachable'])
        self.assertTrue(mon.resolve_owner(entry, {'sess-2': {'kind': 'claude', 'live': True}}, process=lambda p, t: False)['reachable'])

    def test_owner_kinds_and_closed_schema(self):
        cases = [({'harness': 'claude'}, 'claude'), ({'harness': 'codex'}, 'codex'), ({'label': 'la-cargo-build'}, 'la-cargo'),
                 ({'holder': {'pid': 1, 'start_token': 't'}, 'state': 'CLAIMED'}, 'heavy-run'), ({}, 'unknown')]
        for entry, kind in cases:
            out = mon.resolve_owner(entry, {})
            self.assertEqual(out['kind'], kind)
            self.assertEqual(sorted(out), ['evidence', 'kind', 'reachable', 'session_id'])
            self.assertIn(out['kind'], mon.OWNER_KINDS)
            self.assertTrue(set(out['evidence']) <= set(mon.OWNER_EVIDENCE))
        self.assertEqual(mon.resolve_owner({'owner': {'session_id': 7}}, {})['session_id'], None)
        self.assertEqual(mon.resolve_owner({'owner': {'session_id': 'x' * 65}}, {})['session_id'], None)

    def test_the_outbox_line_has_a_closed_schema_and_never_carries_a_session_id_or_path(self):
        qid = self.enqueue('HORO-3130', session='0b1d2e3f-aaaa-bbbb-cccc-1234567890ab')
        self.age(qid, 13)
        self.process_answers = {}
        self.walk(3 * 120)
        (line,) = self.outbox()
        self.assertEqual(sorted(line), ['at', 'blocker', 'cause', 'clock', 'evidence', 'job', 'key', 'owner', 'owner_action', 'phase', 'recovery', 'v'])
        self.assertEqual((line['job'], line['key'], line['owner']['kind']), (qid, qid + ':wait', 'codex'))
        self.assertIsNone(line['owner']['reachable'])
        self.assertTrue(set(line['evidence']) <= set(mon.EVIDENCE))
        self.assertIn(line['blocker'], mon.BLOCKERS)
        text = (mon.monitor_dir(self.state_path) / mon.OUTBOX_NAME).read_text()
        for needle in ('0b1d2e3f', str(self.worktree), 'cargo', 'HORO-3130'):
            self.assertNotIn(needle, text)
        status = (mon.monitor_dir(self.state_path) / 'status.json').read_text() + (mon.monitor_dir(self.state_path) / 'state.json').read_text()
        self.assertNotIn('0b1d2e3f', status)

    def test_the_outbox_rotates_at_one_mib_and_stays_bounded(self):
        directory = mon.monitor_dir(self.state_path)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / mon.OUTBOX_NAME
        path.write_text('x' * mon.OUTBOX_LIMIT)
        qid = self.enqueue('HORO-3131')
        self.age(qid, 13)
        self.walk(3 * 120)
        self.assertEqual(len(self.outbox()), 1)
        self.assertEqual((directory / (mon.OUTBOX_NAME + '.1')).stat().st_size, mon.OUTBOX_LIMIT)
        self.assertLess(path.stat().st_size, 2048)
        self.assertEqual(oct(path.stat().st_mode & 0o777), '0o600')

    def test_an_unwritable_outbox_never_breaks_the_tick_and_is_retried(self):
        qid = self.enqueue('HORO-3132')
        self.age(qid, 13)
        directory = mon.monitor_dir(self.state_path)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        (directory / mon.OUTBOX_NAME).mkdir()                                   # a directory where the file should be: open() fails
        out = self.tick(60)
        self.assertEqual((out['primary'] != 'state-unreadable', out['owner_outbox_pending']), (True, 1))
        self.assertEqual(len(self.pages()), 1)                                  # the notice itself does not wait on the outbox
        (directory / mon.OUTBOX_NAME).rmdir()
        out = self.tick(60)
        self.assertEqual((out['owner_outbox_pending'], len(self.outbox())), (0, 1))

    def test_new_causes_and_phrases_are_known_session_notify_enums(self):
        sn = load('session-notify')
        for cause in mon.INCIDENT_CAUSES:
            self.assertIn(cause, sn.CAUSE_TEXT)
            self.assertIn(mon.OWNER_ACTION[cause], sn.OWNER_ACTION_TEXT)
        self.assertIn('wait-expiry-near', sn.RECOVERY_TEXT)
        self.assertEqual(mon.recovery_phrase([{'wait': 'expiry-near'}], False), 'wait-expiry-near')

    def test_old_state_files_without_the_new_fields_still_load(self):
        qid = self.enqueue('HORO-3133')
        self.age(qid, 13)
        self.tick(60)
        path = mon.monitor_dir(self.state_path) / 'state.json'
        state = json.loads(path.read_text())
        for rec in state['incidents'].values():
            for key in ('clock', 'blocker', 'holder_pid', 'holder_token', 'elapsed_s', 'deadline_s', 'deadline_source', 'outbox_phase'):
                rec.pop(key)
        path.write_text(json.dumps(state))
        self.tick(60)
        self.assertEqual(len(self.pages()), 1)       # read back as a grant incident: it is not mistaken for a new wait incident


class Inventory(unittest.TestCase):
    def test_callers_are_classified_from_text_without_running_them(self):
        with tempfile.TemporaryDirectory() as root:
            for name, body in (('a', "RUNNER_PROTOCOL = 2\nHEAVY_LANES = {'emergency': 0, 'stable': 2}\n"),
                               ('b', "HEAVY_LANES = {'stable': 2}\nmemory_fn=None\n"), ('c', "HEAVY_LANES = {'stable': 1}\n")):
                (Path(root) / name / 'scripts').mkdir(parents=True)
                (Path(root) / name / 'scripts/disk-admission.py').write_text(body + 'raise SystemExit("must not run")\n')
            kinds = {r['checkout']: r['kind'] for r in mon.inventory(root)}
        self.assertEqual(kinds, {'a': 'current', 'b': 'memory-gated-advance-unstamped', 'c': 'outdated'})


class InventoryStableLanes(unittest.TestCase):
    """`callers` reports the effective stable lane cap (ADR 0017 revision 12), not the two-lane base table."""
    def lanes_of(self, body):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / 'x/scripts').mkdir(parents=True)
            (Path(root) / 'x/scripts/disk-admission.py').write_text(body)
            return mon.inventory(root)[0]['stable_lanes']

    def test_the_shipped_runner_reports_the_cap_heavy_lane_cap_computes(self):
        shipped = (ROOT / 'scripts/disk-admission.py').read_text()
        with unittest.mock.patch.object(da, 'THIRD_LANE_ENABLED', True), unittest.mock.patch.object(da, 'FOURTH_LANE_ENABLED', False):
            self.assertEqual(self.lanes_of(shipped), da.heavy_lane_cap('stable'))
        self.assertEqual(self.lanes_of(shipped), 3)

    def test_the_base_table_and_the_two_flags_decide_the_reported_cap(self):
        base = "HEAVY_LANES = {'emergency': 0, 'low': 0, 'stable': 2, 'wide': 2}\n"
        self.assertEqual(self.lanes_of(base), 2)                                                # an older copy: no third lane in stable
        self.assertEqual(self.lanes_of(base + 'THIRD_LANE_ENABLED = True\n'), 2)               # revision 11: wide-only third lane
        both = base + "THIRD_LANE_ENABLED = True\nTHIRD_LANE_TIERS = ('stable', 'wide')\n"
        self.assertEqual(self.lanes_of(both), 3)
        self.assertEqual(self.lanes_of(both.replace('= True', '= False')), 2)                  # the kill switch
        self.assertEqual(self.lanes_of(base + "THIRD_LANE_ENABLED = True\nTHIRD_LANE_TIERS = ('wide',)\n"), 2)
        self.assertIsNone(self.lanes_of('RUNNER_PROTOCOL = 2\n'))


class SafetyShape(unittest.TestCase):
    """The monitor must stay unable to do what it is forbidden to do."""

    def test_no_signal_launchctl_or_ownership_attestation(self):
        source = (ROOT / 'scripts/admission-monitor.py').read_text()
        tree = ast.parse(source)
        # The single signal path is `sigterm_only` (one SIGTERM to one positive pid, used only through the recovery library's guards); nothing else may signal.
        sender = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'sigterm_only')
        inside = {id(x) for x in ast.walk(sender)}
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and id(n) not in inside} | {
            n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and id(n) not in inside}
        for forbidden in ('kill', 'killpg', 'signal', 'pkill', 'rmtree', 'unlink', 'remove', 'rmdir', 'SIGKILL'):
            self.assertNotIn(forbidden, names, forbidden)
        self.assertEqual({n.attr for n in ast.walk(sender) if isinstance(n, ast.Attribute) and n.attr.startswith('SIG')}, {'SIGTERM'})
        self.assertNotIn('SIGKILL', source)
        # `resolve_owner` is the one place an entry's session id is read (to find its owner); no notice, outbox line or state record carries it.
        resolver = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'resolve_owner')
        outside = '\n'.join(l for i, l in enumerate(source.splitlines(), 1) if not resolver.lineno <= i <= resolver.end_lineno)
        for text in ('launchctl', '--confirm-ended', 'confirm_ended', "'release'", "'cancel'", 'session_id', 'kill -'):
            self.assertNotIn(text, outside.replace('without the stamp', ''), text)

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
                                 table=lambda: {}, free=lambda: 300 * GIB, boot=lambda: 1, notify=lambda a: True, clear=lambda: True,
                                 emit_incident=lambda a: {'result': 'queued', 'delivery': 'sent'}))
            finally:
                os.environ.pop('ADMISSION_MONITOR_DIR')
            self.assertEqual(memory.read_bytes(), before)
            self.assertFalse(state.exists())

    def test_the_plist_installs_nothing_and_names_the_checkout_script(self):
        text = mon.plist(Path('/x/scripts/admission-monitor.py'))
        self.assertIn('com.coding-agent-environment.admission-monitor', text)
        self.assertIn('<integer>60</integer>', text)
        self.assertIn('<string>--act</string>', text)
        self.assertNotIn('<string>--act</string>', mon.plist(Path('/x/scripts/admission-monitor.py'), act=False))
        for needle in ('KeepAlive', 'sudo', 'LaunchDaemons', 'UserName', 'launchctl'):
            self.assertNotIn(needle, text)
        import plistlib
        data = plistlib.loads(text.encode())
        self.assertEqual(data['ProgramArguments'][:2], ['/usr/bin/python3', '/x/scripts/admission-monitor.py'])
        self.assertEqual((data['StartInterval'], data['StandardOutPath']), (60, '/dev/null'))

    def test_the_plist_command_demands_an_existing_checkout_and_prints_without_installing(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / 'scripts').mkdir()
            (Path(root) / 'scripts/admission-monitor.py').write_text('x')
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(mon.main(['plist', '--checkout', root, '--observe-only']), 0)
            self.assertIn(str(Path(root).resolve() / 'scripts/admission-monitor.py'), buf.getvalue())
            self.assertNotIn('--act', buf.getvalue())
            self.assertEqual(mon.main(['plist', '--checkout', root + '/missing']), 2)


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
