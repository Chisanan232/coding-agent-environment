#!/usr/bin/env python3
"""Guarded recovery library (SPE-176 slice 1b): abandoned-unstarted cancel, obsolete-wrapper SIGTERM, lock, receipts, allowlist."""
from datetime import datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import ast
import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = spec_from_file_location(name.replace('-', '_'), ROOT / 'scripts' / (name + '.py'))
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rec, da = load('admission-recovery'), load('disk-admission')
NOW = datetime(2026, 11, 19, 11, 30, 0, tzinfo=timezone.utc)
GRANT_AT = rec.iso(NOW - timedelta(minutes=20))
ME = os.getuid()


class Clock(object):
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)


def entry(**over):
    base = {'id': 'q000811', 'state': 'GRANTED', 'seq': 811, 'grant': {'at': GRANT_AT, 'expires': rec.iso(NOW + timedelta(minutes=40))},
            'waiter': {'pid': 4001, 'start_token': 'Thu Nov 19 10:50:00 2026'}}
    base.update(over)
    return base


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.kills = []
        self.cancels = []
        self.tokens = {}        # pid -> token; absent = gone
        self.table = {}
        self.entries = {}
        self.owner = 'gone'
        self.receipts = []
        self.uids = {}
        self.registered = None
        self.status = {'entries': [], 'lane_ids': [], 'workloads': {}}

    def env(self, **over):
        fns = dict(
            table_fn=lambda: dict(self.table), token_fn=lambda pid: self.tokens.get(pid), uid_fn=lambda pid: self.uids.get(pid, ME),
            kill_fn=lambda pid, sig: self.kills.append((pid, sig)), clock=self.clock, sleep_fn=self.clock.sleep,
            cancel_fn=lambda qid: self.cancels.append(qid) or {'ok': True, 'reason': 'cancelled'},
            read_entry_fn=lambda qid: self.entries.get(qid), status_fn=lambda: self.status, receipts_fn=lambda qid: list(self.receipts),
            owner_exec_fn=lambda e: self.owner, registered_fn=lambda qid: self.registered)
        fns.update(over)
        return rec.Env(self.tmp.name, **fns)

    def lines(self):
        path = Path(self.tmp.name) / rec.RECEIPT_NAME
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


class AbandonedUnstarted(Base):
    def decide(self, e, **over):
        self.entries[e['id']] = e
        return rec.abandoned_unstarted(e, self.env(**over))

    def test_q000811_replay_cancels(self):
        # waiter exited (pid gone), executor exited, no holder, grant 20 min old
        d = self.decide(entry())
        self.assertEqual(d['action'], 'cancel')
        self.assertIn('waiter-gone', d['reasons'])
        self.assertIn('owner-gone', d['reasons'])
        self.assertTrue(set(d['reasons']) <= set(rec.REASONS))

    def refusal(self, minutes_ago=10, kind='malformed-grant'):
        return {'event': 'claim-refused', 'id': 'q000811', 'kind': kind, 'ts': rec.iso(NOW - timedelta(minutes=minutes_ago))}

    def test_permanent_refusal_as_latest_event_cancels(self):
        self.owner = 'refused-permanently'
        self.receipts = [{'event': 'granted', 'ts': GRANT_AT}, self.refusal()]
        d = self.decide(entry())
        self.assertEqual(d['action'], 'cancel')
        self.assertIn('owner-refused', d['reasons'])

    def test_unknown_owner_with_refusal_receipt_is_none(self):
        self.owner = 'unknown'
        self.receipts = [self.refusal()]
        self.assertEqual(self.decide(entry())['reasons'], ['owner-uninspectable'])

    def test_transient_refusal_kinds_never_count(self):
        self.owner = 'refused-permanently'
        for kind in ('memory-gate', 'floor', 'lane', 'budget', 'other', None):
            self.receipts = [self.refusal(kind=kind)]
            self.assertEqual(self.decide(entry())['reasons'], ['owner-refusal-unproven'], kind)

    def test_refusal_must_be_the_latest_event(self):
        self.owner = 'refused-permanently'
        for later in ({'event': 'claim-refused', 'kind': 'lane', 'ts': rec.iso(NOW - timedelta(minutes=5))},
                      {'event': 'claimed', 'ts': rec.iso(NOW - timedelta(minutes=5))},
                      {'event': 'granted', 'ts': rec.iso(NOW - timedelta(minutes=5))}):
            self.receipts = [self.refusal(), later]
            self.assertEqual(self.decide(entry())['reasons'], ['owner-refusal-unproven'])

    def test_refusal_without_receipts_or_before_grant_is_unproven(self):
        self.owner = 'refused-permanently'
        self.receipts = []
        self.assertEqual(self.decide(entry())['reasons'], ['owner-refusal-unproven'])
        self.receipts = [self.refusal(minutes_ago=120)]
        self.assertEqual(self.decide(entry())['reasons'], ['owner-refusal-unproven'])

    def test_non_dict_receipt_is_unproven_not_a_crash(self):
        self.owner = 'refused-permanently'
        for junk in ('x', None, 7, ['claim-refused']):
            self.receipts = [junk]
            self.assertEqual(self.decide(entry())['reasons'], ['owner-refusal-unproven'], junk)

    def test_receipts_reader_failure_is_unproven(self):
        self.owner = 'refused-permanently'

        def boom(qid):
            raise OSError('x')
        self.assertEqual(self.decide(entry(), receipts_fn=boom)['reasons'], ['owner-refusal-unproven'])

    def test_malformed_waiter_fails_closed_without_crashing(self):
        for waiter in ({'pid': 4001}, {'start_token': 'x'}, {'pid': '4001', 'start_token': 'x'}, {'pid': True, 'start_token': 'x'}, 'x', ['a']):
            e = entry(waiter=waiter)
            self.entries['q000811'] = e
            self.assertEqual(rec.do_cancel(e, self.env())['result'], 'none', waiter)
        self.assertEqual(self.cancels, [])

    def test_manual_handoff_with_live_owner_is_none(self):
        self.owner = 'live'
        d = self.decide(entry(waiter=None))
        self.assertEqual(d, {'action': 'none', 'reasons': ['owner-live']})

    def test_live_owner_vetoes_even_after_refusal(self):
        self.owner = 'live'
        self.receipts = [self.refusal()]
        self.assertEqual(self.decide(entry())['reasons'], ['owner-live'])

    def test_waiter_alive_is_none(self):
        self.tokens[4001] = 'Thu Nov 19 10:50:00 2026'
        self.assertEqual(self.decide(entry()), {'action': 'none', 'reasons': ['waiter-alive']})

    def test_waiter_uninspectable_is_none(self):
        def boom(pid):
            raise OSError('ps failed')
        self.assertEqual(self.decide(entry(), token_fn=boom), {'action': 'none', 'reasons': ['waiter-uninspectable']})

    def test_reused_waiter_pid_counts_as_gone(self):
        self.tokens[4001] = 'Thu Nov 19 11:10:00 2026'
        self.assertEqual(self.decide(entry())['action'], 'cancel')

    def test_owner_uninspectable_is_none(self):
        self.owner = 'unknown'
        self.assertEqual(self.decide(entry()), {'action': 'none', 'reasons': ['owner-uninspectable']})

    def test_owner_probe_error_is_uninspectable(self):
        def boom(e):
            raise RuntimeError('x')
        self.assertEqual(self.decide(entry(), owner_exec_fn=boom)['reasons'], ['owner-uninspectable'])

    def test_young_grant_is_none(self):
        young = entry(grant={'at': rec.iso(NOW - timedelta(seconds=60)), 'expires': GRANT_AT})
        self.assertEqual(self.decide(young)['reasons'], ['grant-too-young'])

    def test_not_granted_states_are_none(self):
        for state in ('QUEUED', 'ELIGIBLE', 'CLAIMED', 'CANCELLED', 'EXPIRED', 'RELEASED'):
            self.assertEqual(self.decide(entry(state=state))['reasons'], ['not-granted'], state)

    def test_holder_release_workload_are_none(self):
        self.assertEqual(self.decide(entry(holder={'pid': 9, 'start_token': 't'}))['reasons'], ['has-holder'])
        self.assertEqual(self.decide(entry(release={'at': GRANT_AT}))['reasons'], ['has-release'])
        self.assertEqual(self.decide(entry(holder={'pid': 9, 'workload': {'pgid': 9}}))['reasons'], ['has-holder', 'has-workload'])

    def test_claimed_between_decision_and_action_is_none(self):
        snapshot = entry()
        self.entries['q000811'] = entry(state='CLAIMED', holder={'pid': 77, 'start_token': 't'})
        d = rec.abandoned_unstarted(snapshot, self.env())
        self.assertEqual(d, {'action': 'none', 'reasons': ['entry-changed']})

    def test_reread_missing_or_failing_is_none(self):
        self.assertEqual(rec.abandoned_unstarted(entry(), self.env())['reasons'], ['entry-missing'])

        def boom(qid):
            raise OSError('x')
        self.assertEqual(rec.abandoned_unstarted(entry(), self.env(read_entry_fn=boom))['reasons'], ['reread-failed'])

    def test_owner_turning_live_between_reads_is_none(self):
        answers = iter(['gone', 'live'])
        e = entry()
        self.entries['q000811'] = e
        d = rec.abandoned_unstarted(e, self.env(owner_exec_fn=lambda x: next(answers)))
        self.assertEqual(d['reasons'], ['owner-live'])

    def test_decision_schema_is_closed(self):
        d = self.decide(entry())
        self.assertEqual(set(d), {'action', 'reasons'})
        self.assertIn(d['action'], rec.DECISION_ACTIONS)


class DoCancel(Base):
    def test_cancels_with_queue_id_only(self):
        e = entry()
        self.entries['q000811'] = e
        out = rec.do_cancel(e, self.env())
        self.assertEqual(out['result'], 'cancelled')
        self.assertEqual(self.cancels, ['q000811'])
        kinds = [(l['kind'], l['result']) for l in self.lines()]
        self.assertEqual(kinds, [('decision', 'decided'), ('cancel', 'cancelled')])

    def test_none_does_not_call_scheduler(self):
        self.owner = 'live'
        e = entry()
        self.entries['q000811'] = e
        self.assertEqual(rec.do_cancel(e, self.env())['result'], 'none')
        self.assertEqual(self.cancels, [])

    def test_scheduler_refusal_is_failed_not_assumed(self):
        e = entry()
        self.entries['q000811'] = e
        out = rec.do_cancel(e, self.env(cancel_fn=lambda qid: {'ok': False, 'reason': 'q000811 is CLAIMED'}))
        self.assertEqual(out['result'], 'failed')

    def test_bad_id_never_reaches_scheduler(self):
        out = rec.do_cancel(entry(id='../x'), self.env())
        self.assertEqual(out['result'], 'none')
        self.assertEqual(self.cancels, [])

    def test_concurrent_recovery_is_busy(self):
        e = entry()
        self.entries['q000811'] = e
        os.makedirs(self.tmp.name, exist_ok=True)
        fd = os.open(os.path.join(self.tmp.name, rec.LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(lambda: os.close(fd))
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.assertEqual(rec.do_cancel(e, self.env())['result'], 'busy')
        self.assertEqual(self.cancels, [])
        self.assertEqual(rec.terminate_wrapper('q000811', 5, self.env())['result'], 'busy')
        fcntl.flock(fd, fcntl.LOCK_UN)
        self.assertEqual(rec.do_cancel(e, self.env())['result'], 'cancelled')

    def test_lock_released_after_exception(self):
        e = entry()
        self.entries['q000811'] = e

        def boom(qid):
            raise RuntimeError('x')
        with self.assertRaises(RuntimeError):
            rec.do_cancel(e, self.env(cancel_fn=boom))
        self.assertEqual(rec.do_cancel(e, self.env())['result'], 'cancelled')

    def test_real_cancel_binding_passes_only_the_id(self):
        calls = []
        da_real = rec._disk_admission()
        original = da_real.queue_cancel
        da_real.queue_cancel = lambda *a, **k: calls.append((a, k)) or {'ok': True}
        try:
            env = rec.default_env(Path(self.tmp.name) / 'state' / 'disk.json', self.tmp.name)
            env.cancel_fn('q000811')
        finally:
            da_real.queue_cancel = original
        self.assertEqual(len(calls), 1)
        args, kwargs = calls[0]
        self.assertEqual(kwargs, {})
        self.assertEqual(args[1], 'q000811')
        self.assertEqual(len(args), 2)


class VerifyRecovery(Base):
    def test_verified_pending_failed(self):
        cancelled = {'id': 'q000811', 'state': 'CANCELLED', 'seq': 811}
        nxt = {'id': 'q000812', 'state': 'GRANTED', 'seq': 812}
        self.status = {'entries': [cancelled], 'lane_ids': [], 'workloads': {}}
        self.assertEqual(rec.verify_recovery('q000811', self.env())['state'], 'verified')
        self.status = {'entries': [cancelled, nxt], 'lane_ids': ['q000812'], 'workloads': {}}
        self.assertEqual(rec.verify_recovery('q000811', self.env(), rec.iso(NOW))['state'], 'pending')
        self.clock.now = NOW + timedelta(seconds=rec.VERIFY_WINDOW_S + 1)
        self.assertEqual(rec.verify_recovery('q000811', self.env(), rec.iso(NOW))['state'], 'failed')
        started = {'id': 'q000812', 'state': 'CLAIMED', 'seq': 812}
        self.status = {'entries': [cancelled, started], 'lane_ids': ['q000812'], 'workloads': {}}
        self.assertEqual(rec.verify_recovery('q000811', self.env(), rec.iso(NOW))['state'], 'verified')

    def test_failed_when_not_terminal_or_lane_held_or_detached_workload(self):
        live = {'id': 'q000811', 'state': 'GRANTED', 'seq': 811}
        self.status = {'entries': [live], 'lane_ids': [], 'workloads': {}}
        self.assertEqual(rec.verify_recovery('q000811', self.env())['state'], 'failed')
        done = {'id': 'q000811', 'state': 'CANCELLED', 'seq': 811}
        self.status = {'entries': [done], 'lane_ids': ['q000811'], 'workloads': {}}
        self.assertEqual(rec.verify_recovery('q000811', self.env())['state'], 'failed')
        self.status = {'entries': [done], 'lane_ids': [], 'workloads': {'q000811': [555]}}
        self.table = {555: (1, 555, 'S', 't')}
        out = rec.verify_recovery('q000811', self.env())
        self.assertEqual((out['state'], out['checks']['no_detached_workload']), ('failed', False))

    def test_unreadable_status_is_pending_never_assumed_verified(self):
        def boom():
            raise OSError('x')
        self.assertEqual(rec.verify_recovery('q000811', self.env(status_fn=boom))['state'], 'pending')


class Wrapper(Base):
    PID = 5000

    def setUp(self):
        Base.setUp(self)
        self.tokens[self.PID] = 'Thu Nov 19 09:00:00 2026'
        self.table = {self.PID: (4000, 5000, 'S', 'local'), 4000: (1, 4000, 'S', 'x')}
        self.registered = {'pid': self.PID, 'start_token': 'Thu Nov 19 09:00:00 2026', 'pgid': 5000, 'ppid': 4000, 'exe': 'python3:heavy-run'}
        self.exe = 'python3:heavy-run'
        self.holds = False
        self.entries['q000811'] = {'id': 'q000811', 'state': 'EXPIRED', 'waiter': {'pid': self.PID, 'start_token': 'x'}}

    def wenv(self, **over):
        over.setdefault('exe_fn', lambda pid: self.exe)
        over.setdefault('holds_workload_fn', lambda pid: self.holds)
        return self.env(**over)

    def verdict(self, **over):
        over.setdefault('exe_fn', lambda pid: self.exe)
        over.setdefault('holds_workload_fn', lambda pid: self.holds)
        return rec.obsolete_wrapper('q000811', self.PID, self.env(**over))

    def test_obsolete_when_entry_terminal(self):
        for state in rec.TERMINAL:
            self.entries['q000811']['state'] = state
            self.assertTrue(self.verdict()['obsolete'], state)

    def test_obsolete_when_entry_gone(self):
        del self.entries['q000811']
        v = self.verdict()
        self.assertTrue(v['obsolete'])
        self.assertEqual(v['reasons'], ['obsolete-entry-missing'])

    def test_live_entry_is_not_obsolete(self):
        for state in ('QUEUED', 'ELIGIBLE', 'GRANTED', 'CLAIMED'):
            self.entries['q000811']['state'] = state
            self.assertEqual(self.verdict()['reasons'], ['entry-live'], state)

    def test_pid_reuse_refuses(self):
        self.tokens[self.PID] = 'Thu Nov 19 11:00:00 2026'
        self.assertEqual(self.verdict()['reasons'], ['pid-reused'])
        out = rec.terminate_wrapper('q000811', self.PID, self.wenv())
        self.assertEqual(out['result'], 'refused')
        self.assertEqual(self.kills, [])

    def test_wrong_uid_wrong_group_wrong_parent_unregistered(self):
        self.uids[self.PID] = ME + 1
        self.assertEqual(self.verdict()['reasons'], ['wrong-uid'])
        self.uids.clear()
        self.table[self.PID] = (4000, 6000, 'S', 'l')
        self.assertEqual(self.verdict()['reasons'], ['wrong-process-group'])
        self.table[self.PID] = (4999, 5000, 'S', 'l')
        self.assertEqual(self.verdict()['reasons'], ['wrong-parent'])
        self.table[self.PID] = (4000, 5000, 'S', 'l')
        self.registered = None
        self.assertEqual(self.verdict()['reasons'], ['wrapper-unregistered'])
        self.registered = {'pid': 1234, 'start_token': 'x', 'pgid': 5000, 'ppid': 4000}
        self.assertEqual(self.verdict()['reasons'], ['wrapper-unregistered'])

    def test_not_alive_and_unreadable_table(self):
        del self.table[self.PID]
        self.assertEqual(self.verdict()['reasons'], ['pid-not-alive'])

        def boom():
            raise OSError('x')
        self.assertEqual(self.verdict(table_fn=boom)['reasons'], ['table-unreadable'])

    def test_terminal_entry_whose_waiter_is_another_process_refuses(self):
        self.entries['q000811']['waiter'] = {'pid': 9999, 'start_token': 'x'}
        self.assertEqual(self.verdict()['reasons'], ['not-the-entry-waiter'])

    def test_live_child_workload_reported_no_signal(self):
        self.table[5001] = (self.PID, 5000, 'S', 'c')
        self.table[5002] = (5001, 5000, 'R', 'c')
        out = rec.terminate_wrapper('q000811', self.PID, self.wenv())
        self.assertEqual(out['result'], 'refused')
        self.assertEqual(out['verdict']['children'], [5001, 5002])
        self.assertEqual(self.kills, [])

    def test_zombie_child_is_not_a_workload(self):
        self.table[5001] = (self.PID, 5000, 'Z', 'c')
        self.assertTrue(self.verdict()['obsolete'])

    def test_detached_child_reported_never_signalled(self):
        self.table[7001] = (1, 5000, 'S', 'c')     # reparented to init, same process group, not a descendant
        out = rec.terminate_wrapper('q000811', self.PID, self.wenv())
        self.assertEqual(out['result'], 'refused')
        self.assertEqual(out['verdict']['detached'], [7001])
        self.assertEqual(out['verdict']['reasons'], ['detached-workload-present'])
        self.assertEqual(self.kills, [])

    def test_sigterm_only_exactly_once(self):
        def kill(pid, sig):
            self.kills.append((pid, sig))
            del self.table[pid]
        out = rec.terminate_wrapper('q000811', self.PID, self.wenv(kill_fn=kill))
        self.assertEqual(out['result'], 'terminated')
        self.assertEqual(self.kills, [(self.PID, signal.SIGTERM)])
        self.assertEqual([l['result'] for l in self.lines()], ['intent', 'terminated'])

    def test_stubborn_process_fails_without_escalation(self):
        out = rec.terminate_wrapper('q000811', self.PID, self.wenv(), grace_s=5, poll_s=1)
        self.assertEqual(out['result'], 'failed-to-terminate')
        self.assertEqual(out['evidence']['pid'], self.PID)
        self.assertEqual(self.kills, [(self.PID, signal.SIGTERM)])
        self.assertEqual(self.clock.now, NOW + timedelta(seconds=5))
        self.assertEqual(self.lines()[-1]['result'], 'failed-to-terminate')

    def test_pid_reused_after_signal_counts_as_gone(self):
        def kill(pid, sig):
            self.kills.append((pid, sig))
            self.tokens[pid] = None
        self.assertEqual(rec.terminate_wrapper('q000811', self.PID, self.wenv(kill_fn=kill))['result'], 'terminated')

    def test_kill_error_reported(self):
        def kill(pid, sig):
            raise PermissionError('x')
        self.assertEqual(rec.terminate_wrapper('q000811', self.PID, self.wenv(kill_fn=kill))['result'], 'failed-to-terminate')

    def test_invalid_pids_refuse_and_never_signal(self):
        for bad in (0, 1, -1, os.getpid(), os.getppid(), True, False, '5000', 5000.0, None, b'5', (5000,)):
            out = rec.terminate_wrapper('q000811', bad, self.wenv())
            self.assertEqual(out['result'], 'refused', bad)
            self.assertEqual(out['verdict']['reasons'], ['invalid-pid'], bad)
        self.assertEqual(self.kills, [])

    def test_pgid_zero_or_negative_refuses(self):
        for pgid in (0, -1):
            self.table[self.PID] = (4000, pgid, 'S', 'l')
            self.registered['pgid'] = pgid
            self.assertEqual(self.verdict()['reasons'], ['wrong-process-group'])
        self.assertEqual(self.kills, [])

    def test_exec_workload_identity_guards(self):
        del self.entries['q000811']['waiter']       # terminal entry without a waiter: only identity can tell wrapper from workload
        self.exe = 'cargo:build'
        self.assertEqual(self.verdict()['reasons'], ['exe-mismatch'])
        self.assertEqual(self.verdict(exe_fn=lambda pid: None)['reasons'], ['exe-unverified'])
        self.registered['exe'] = ''
        self.assertEqual(self.verdict()['reasons'], ['exe-unverified'])
        del self.registered['exe']
        self.assertEqual(self.verdict()['reasons'], ['exe-unverified'])
        self.registered['exe'] = 'python3:heavy-run'
        self.exe = 'python3:heavy-run'
        self.holds = True
        self.assertEqual(self.verdict()['reasons'], ['holds-workload'])
        self.assertEqual(self.verdict(holds_workload_fn=lambda pid: None)['reasons'], ['workload-unverified'])

        def boom(pid):
            raise OSError('x')
        self.assertEqual(self.verdict(holds_workload_fn=boom)['reasons'], ['workload-unverified'])
        self.assertEqual(self.verdict(exe_fn=boom)['reasons'], ['exe-unverified'])
        self.holds = False
        self.assertTrue(self.verdict()['obsolete'])
        self.assertEqual(self.kills, [])

    def test_unwired_defaults_refuse(self):
        env = self.env()          # exe_fn / holds_workload_fn left at their unknown defaults
        self.assertEqual(rec.obsolete_wrapper('q000811', self.PID, env)['reasons'], ['exe-unverified'])
        self.assertEqual(rec.terminate_wrapper('q000811', self.PID, env)['result'], 'refused')
        self.assertEqual(self.kills, [])

    def test_default_kill_is_unwired(self):
        env = rec.Env(self.tmp.name)
        with self.assertRaises(OSError):
            env.kill_fn(1, signal.SIGTERM)


class RealThrowawayProcess(Base):
    """Own throwaway child only: real process table, token and uid readers; kill stays injected so no real process is ever signalled."""

    def test_real_token_mismatch_refuses_and_nothing_is_signalled(self):
        child = subprocess.Popen(['/bin/sleep', '60'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (child.terminate(), child.wait()))
        table = da.process_table()
        self.assertIn(child.pid, table)
        token = da.waiter_token(child.pid)
        self.entries = {'q000811': {'id': 'q000811', 'state': 'EXPIRED'}}
        reg = {'pid': child.pid, 'start_token': token, 'pgid': table[child.pid][1], 'ppid': os.getpid(), 'exe': 'sleep'}
        env = self.env(table_fn=da.process_table, token_fn=da.waiter_token, uid_fn=da.process_owner, registered_fn=lambda q: dict(reg, start_token='Mon Jan  1 00:00:00 2001'))
        self.assertEqual(rec.obsolete_wrapper('q000811', child.pid, env)['reasons'], ['pid-reused'])
        env = self.env(table_fn=da.process_table, token_fn=da.waiter_token, uid_fn=da.process_owner, registered_fn=lambda q: dict(reg, pgid=reg['pgid'] + 1))
        self.assertEqual(rec.obsolete_wrapper('q000811', child.pid, env)['reasons'], ['wrong-process-group'])
        self.assertEqual(rec.terminate_wrapper('q000811', child.pid, env)['result'], 'refused')
        self.assertEqual(self.kills, [])
        self.assertIsNone(child.poll())

    def test_real_child_of_our_group_is_a_reported_sibling(self):
        child = subprocess.Popen(['/bin/sleep', '60'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (child.terminate(), child.wait()))
        table = da.process_table()
        self.entries = {'q000811': {'id': 'q000811', 'state': 'EXPIRED'}}
        reg = {'pid': child.pid, 'start_token': da.waiter_token(child.pid), 'pgid': table[child.pid][1], 'ppid': os.getpid(), 'exe': 'sleep'}
        env = self.env(table_fn=da.process_table, token_fn=da.waiter_token, uid_fn=da.process_owner, registered_fn=lambda q: reg, exe_fn=lambda p: 'sleep', holds_workload_fn=lambda p: False)
        verdict = rec.obsolete_wrapper('q000811', child.pid, env)
        self.assertFalse(verdict['obsolete'])
        self.assertIn(os.getpid(), verdict['detached'])   # the test runner shares the group and is not a descendant


class Receipts(Base):
    def test_closed_schema_and_bounds(self):
        env = self.env()
        self.assertTrue(rec.write_receipt(env, 'decision', 'q000811', 'none', 'decided', ['owner-live'], pid=3, detail='x' * 500))
        line = self.lines()[0]
        self.assertTrue(set(line) <= set(rec.RECEIPT_KEYS))
        self.assertEqual(len(line['detail']), 200)
        with self.assertRaises(ValueError):
            rec.write_receipt(env, 'bogus', 'q000811', 'none', 'x')
        path = Path(self.tmp.name) / rec.RECEIPT_NAME
        self.assertEqual(oct(path.stat().st_mode & 0o777), '0o600')

    def test_rotates_at_one_mib_and_keeps_one_generation(self):
        env = self.env()
        path = Path(self.tmp.name) / rec.RECEIPT_NAME
        path.write_text('x' * rec.RECEIPT_LIMIT)
        rec.write_receipt(env, 'decision', 'q000811', 'none', 'decided')
        self.assertEqual(len(self.lines()), 1)
        self.assertEqual((Path(str(path) + '.1')).stat().st_size, rec.RECEIPT_LIMIT)
        for _ in range(3):
            path.write_text('y' * rec.RECEIPT_LIMIT)
            rec.write_receipt(env, 'decision', 'q000811', 'none', 'decided')
        self.assertEqual(sorted(p.name for p in Path(self.tmp.name).iterdir() if p.name.startswith('recovery-receipts')),
                         [rec.RECEIPT_NAME, rec.RECEIPT_NAME + '.1'])

    def test_unwritable_directory_returns_false(self):
        blocker = Path(self.tmp.name) / 'file'
        blocker.write_text('')
        env = rec.Env(str(blocker / 'sub'), clock=self.clock)
        self.assertFalse(rec.write_receipt(env, 'decision', 'q000811', 'none', 'decided'))

    def test_monitor_dir_must_be_private_real_and_ours(self):
        base = Path(self.tmp.name)
        loose = base / 'loose'
        loose.mkdir(mode=0o755)
        os.chmod(str(loose), 0o755)
        env = rec.Env(str(loose), clock=self.clock)
        self.assertFalse(rec.write_receipt(env, 'decision', 'q000811', 'none', 'decided'))
        self.assertEqual(rec.do_cancel(entry(), env)['result'], 'unsafe-monitor-dir')
        real = base / 'real'
        real.mkdir(mode=0o700)
        link = base / 'link'
        link.symlink_to(real)
        env = rec.Env(str(link), clock=self.clock)
        self.assertFalse(rec.write_receipt(env, 'decision', 'q000811', 'none', 'decided'))
        self.assertEqual(rec.terminate_wrapper('q000811', 5000, env)['result'], 'unsafe-monitor-dir')
        self.assertEqual(list(real.iterdir()), [])
        fresh = rec.Env(str(base / 'new' / 'dir'), clock=self.clock)
        self.assertTrue(rec.write_receipt(fresh, 'decision', 'q000811', 'none', 'decided'))
        self.assertEqual(os.stat(str(base / 'new' / 'dir')).st_mode & 0o777, 0o700)

    def test_receipt_file_with_loose_mode_is_refused(self):
        path = Path(self.tmp.name) / rec.RECEIPT_NAME
        path.write_text('')
        os.chmod(str(path), 0o644)
        self.assertFalse(rec.write_receipt(self.env(), 'decision', 'q000811', 'none', 'decided'))

    def test_verify_receipt_is_written_under_the_lock(self):
        done = {'id': 'q000811', 'state': 'CANCELLED', 'seq': 811}
        self.status = {'entries': [done], 'lane_ids': [], 'workloads': {}}
        self.assertTrue(rec.verify_recovery('q000811', self.env())['receipt_ok'])
        fd = os.open(os.path.join(self.tmp.name, rec.LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(lambda: os.close(fd))
        fcntl.flock(fd, fcntl.LOCK_EX)
        out = rec.verify_recovery('q000811', self.env())
        self.assertEqual((out['state'], out['receipt_ok']), ('verified', False))
        self.assertEqual(len([l for l in self.lines() if l['kind'] == 'verify']), 1)

    def test_unwritable_decision_receipt_blocks_cancel_and_kill(self):
        def failing(*args, **kwargs):
            raise OSError('disk')
        original = rec.write_receipt
        for writer in (failing, lambda *a, **k: False):
            rec.write_receipt = writer
            try:
                e = entry()
                self.entries['q000811'] = e
                out = rec.do_cancel(e, self.env())
                self.assertEqual(out['result'], 'receipt-unwritable')
                self.assertEqual(self.cancels, [])
                self.table = {5000: (4000, 5000, 'S', 'l'), 4000: (1, 4000, 'S', 'x')}
                self.tokens[5000] = 'T'
                self.registered = {'pid': 5000, 'start_token': 'T', 'pgid': 5000, 'ppid': 4000, 'exe': 'e'}
                self.entries['q000811'] = {'id': 'q000811', 'state': 'EXPIRED'}
                out = rec.terminate_wrapper('q000811', 5000, self.env(exe_fn=lambda p: 'e', holds_workload_fn=lambda p: False))
                self.assertEqual(out['result'], 'receipt-unwritable')
                self.assertEqual(self.kills, [])
            finally:
                rec.write_receipt = original

    def test_symlinked_lock_file_is_refused(self):
        target = Path(self.tmp.name) / 'elsewhere'
        target.write_text('')
        (Path(self.tmp.name) / rec.LOCK_NAME).symlink_to(target)
        e = entry()
        self.entries['q000811'] = e
        self.assertEqual(rec.do_cancel(e, self.env())['result'], 'unsafe-monitor-dir')
        self.assertEqual(rec.terminate_wrapper('q000811', 5000, self.env())['result'], 'unsafe-monitor-dir')
        self.assertEqual((self.cancels, self.kills), ([], []))

    def test_every_decision_and_action_is_receipted(self):
        self.owner = 'live'
        e = entry()
        self.entries['q000811'] = e
        rec.do_cancel(e, self.env())
        self.assertEqual([(l['kind'], l['action']) for l in self.lines()], [('decision', 'none')])


class Allowlist(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'terminate-allowlist.json'

    def write(self, doc):
        self.path.write_text(json.dumps(doc))
        return self.path

    def good(self, **over):
        doc = {'version': 1, 'enabled': True, 'entries': [{'kind': 'build-crate', 'label': 'aa-build', 'deadline_multiple': 2}]}
        doc.update(over)
        return doc

    def test_absent_is_disabled(self):
        policy = rec.load_terminate_allowlist(self.path)
        self.assertFalse(policy['enabled'])
        self.assertEqual(policy['entries'], [])
        self.assertFalse(rec.terminate_allowed_for_workload(policy, 'build-crate', 'aa-build', 5))
        self.assertFalse(rec.terminate_allowed_for_workload(None, 'build-crate', 'aa-build', 5))

    def test_valid_enabled_entry_matches_only_exactly(self):
        policy = rec.load_terminate_allowlist(self.write(self.good()))
        self.assertTrue(rec.terminate_allowed_for_workload(policy, 'build-crate', 'aa-build', 2))
        self.assertTrue(rec.terminate_allowed_for_workload(policy, 'build-crate', 'aa-build', 3.5))
        self.assertFalse(rec.terminate_allowed_for_workload(policy, 'build-crate', 'aa-build', 1.9))
        self.assertFalse(rec.terminate_allowed_for_workload(policy, 'build-crate', 'other', 9))
        self.assertFalse(rec.terminate_allowed_for_workload(policy, 'container-suite', 'aa-build', 9))

    def test_valid_but_disabled_never_allows(self):
        policy = rec.load_terminate_allowlist(self.write(self.good(enabled=False)))
        self.assertFalse(rec.terminate_allowed_for_workload(policy, 'build-crate', 'aa-build', 9))

    def test_unknown_and_invalid_keys_rejected(self):
        bad = [self.good(extra=1), {'version': 1, 'enabled': True}, self.good(version=2), self.good(enabled='yes'),
               self.good(entries=[{'kind': 'k', 'label': 'l', 'deadline_multiple': 2, 'signal': 'KILL'}]),
               self.good(entries=[{'kind': 'k', 'label': 'l'}]), self.good(entries=[{'kind': 'k', 'label': 'l', 'deadline_multiple': 0.5}]),
               self.good(entries=[{'kind': 'k', 'label': '../x', 'deadline_multiple': 2}]), self.good(entries='x'), [1]]
        for doc in bad:
            with self.assertRaises(rec.AllowlistError, msg=str(doc)):
                rec.load_terminate_allowlist(self.write(doc))
        self.path.write_text('not json')
        with self.assertRaises(rec.AllowlistError):
            rec.load_terminate_allowlist(self.path)

    def test_version_true_and_directory_rejected(self):
        with self.assertRaises(rec.AllowlistError):
            rec.load_terminate_allowlist(self.write(self.good(version=True)))
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaises(rec.AllowlistError):
            rec.load_terminate_allowlist(self.path)

    def test_symlink_and_oversize_rejected(self):
        target = Path(self.tmp.name) / 'real.json'
        target.write_text(json.dumps(self.good()))
        self.path.symlink_to(target)
        with self.assertRaises(rec.AllowlistError):
            rec.load_terminate_allowlist(self.path)
        self.path.unlink()
        self.path.write_text(' ' * (rec.ALLOWLIST_LIMIT + 1))
        with self.assertRaises(rec.AllowlistError):
            rec.load_terminate_allowlist(self.path)


class Shape(unittest.TestCase):
    def test_allowlist_is_not_used_by_recovery_paths(self):
        tree = ast.parse((ROOT / 'scripts' / 'admission-recovery.py').read_text())
        funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        for name in ('abandoned_unstarted', '_evaluate', 'do_cancel', 'verify_recovery', 'obsolete_wrapper', 'terminate_wrapper', '_alive_same'):
            called = {c.func.id for c in ast.walk(funcs[name]) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
            self.assertFalse(called & {'load_terminate_allowlist', 'terminate_allowed_for_workload'}, name)

    def test_only_sigterm_is_ever_named_and_only_known_consumers_import_it(self):
        text = (ROOT / 'scripts' / 'admission-recovery.py').read_text()
        self.assertNotIn('signal.SIGKILL', text)
        self.assertNotIn('os.kill', text)
        self.assertNotIn('killpg', text)
        consumers = {'admission-monitor.py', 'heavy-run', 'la-cargo'}   # the monitor's act mode, and the two wrappers that register themselves
        for path in (ROOT / 'scripts').iterdir():
            if path.is_file() and path.name != 'admission-recovery.py' and path.name not in consumers:
                self.assertNotIn('admission-recovery', path.read_text(errors='ignore'), path.name)
        for name in consumers:
            self.assertNotIn('SIGKILL', (ROOT / 'scripts' / name).read_text(errors='ignore'), name)

    def test_cancel_never_uses_session_or_confirm_or_state_edits(self):
        text = (ROOT / 'scripts' / 'admission-recovery.py').read_text()
        for needle in ('confirm_ended', 'confirm-ended', 'session_id=', '_write_queue_entry', 'write_json_atomic'):
            self.assertNotIn(needle, text)


if __name__ == '__main__':
    unittest.main()
