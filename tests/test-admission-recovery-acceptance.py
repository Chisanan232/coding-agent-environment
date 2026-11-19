#!/usr/bin/env python3
"""End-to-end acceptance of the wired guarded recovery (SPE-176, ADR 0019): the monitor's `tick --act` against a REAL temporary scheduler state and
REAL throwaway processes (stand-in wrappers this test spawns, registers through the real registry writer and cleans up). Nothing here reaches the owner's
admission, queue, monitor or session-notify state, and no process this test did not spawn is ever signalled (the kill binding is wrapped to prove it)."""
from datetime import datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name, alias=None):
    spec = spec_from_file_location(alias or name.replace('-', '_'), ROOT / 'scripts' / (name + '.py'))
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


da, ma, mon, lib = load('disk-admission'), load('memory-admission'), load('admission-monitor'), load('admission-recovery')
da.THIRD_LANE_ENABLED = False
GIB = 1024 ** 3
CALM = {'pressure_level': 1, 'swap_io_mib_s': 0.0, 'compressor_ratio': 0.262, 'load_per_core': 0.4}
ARGV = ['cargo', 'test', 'acceptance']

STAND_IN = '''
import importlib.util, os, signal, subprocess, sys, time
lib_path, registry, kind, go, ready, mode = sys.argv[1:7]
spec = importlib.util.spec_from_file_location('admission_recovery', lib_path)
lib = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lib)
while not os.path.exists(go):
    time.sleep(0.05)
qid = open(go).read().strip()
ok = lib.register_wrapper(registry, qid, kind)
if mode == 'ignore-term':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
extra = ''
if mode == 'child':
    extra = str(subprocess.Popen(['/bin/sleep', '300']).pid)
if mode == 'detached':
    extra = subprocess.run([sys.executable, '-c', 'import subprocess;print(subprocess.Popen(["/bin/sleep", "300"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).pid)'],
                           capture_output=True, text=True).stdout.strip()
open(ready + '.tmp', 'w').write('%s %s' % ('1' if ok else '0', extra))
os.rename(ready + '.tmp', ready)
time.sleep(300)
'''


class Clock:
    """Wall time shifted by an offset the test advances, so the monitor's 60 s cadence needs no sleeping; the library keeps the real clock."""

    def __init__(self):
        self.offset = 0.0

    def __call__(self):
        return datetime.now(timezone.utc) + timedelta(seconds=self.offset)

    def mono(self):
        return time.monotonic() + self.offset


class Acceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.root = root
        self.path = root / 'admission/state.json'
        self.memory_state = root / 'memory/state.json'
        self.memory_state.parent.mkdir()
        self.worktree = root / 'wt'
        self.worktree.mkdir()
        self.bin = root / 'bin'
        self.bin.mkdir()
        self.script = {}
        saved = {k: os.environ.get(k) for k in ('ADMISSION_MONITOR_DIR', 'ADMISSION_WRAPPERS_DIR', 'SESSION_NOTIFY_STATE_DIR', 'SESSION_NOTIFY_CONFIG')}
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v) for k, v in saved.items()])
        os.environ.pop('ADMISSION_WRAPPERS_DIR', None)    # the registry is `wrappers/` beside the temp state file
        os.environ['ADMISSION_MONITOR_DIR'] = str(root / 'mon')
        os.environ['SESSION_NOTIFY_STATE_DIR'] = str(root / 'session-status')
        os.environ['SESSION_NOTIFY_CONFIG'] = str(root / 'session-notify.json')
        self.monitor_dir = root / 'mon'
        self.registry = lib.wrappers_dir(self.path)
        self.clock = Clock()
        self.procs, self.mine, self.kills, self.alerts, self.advances = [], set(), [], [], []
        self.sessions = {}
        self.emit_result = {'result': 'queued', 'delivery': 'sent'}
        self.addCleanup(self.reap)
        old_grace = mon.RECOVERY_GRACE_S
        mon.RECOVERY_GRACE_S = 1.0
        self.addCleanup(lambda: setattr(mon, 'RECOVERY_GRACE_S', old_grace))

    # ---- processes ------------------------------------------------------------------------------------------------
    def reap(self):
        for proc in self.procs:
            with_pid = proc.pid
            try:
                proc.kill()
            except OSError:
                pass
            proc.wait()
            self.mine.discard(with_pid)

    def spawn(self, argv, **kw):
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, **kw)
        self.procs.append(proc)
        self.mine.add(proc.pid)
        return proc

    def stand_in(self, kind='heavy-run', mode='plain'):
        """A wrapper process waiting for its queue id; `register(qid)` releases it. Named like the real script so its identity is `python heavy-run`."""
        script = self.bin / kind
        script.write_text(STAND_IN)
        tag = '%s-%d' % (kind, len(self.procs))
        go, ready = self.root / (tag + '.go'), self.root / (tag + '.ready')
        proc = self.spawn([sys.executable, str(script), str(ROOT / 'scripts/admission-recovery.py'), str(self.registry), kind, str(go), str(ready), mode])
        return {'proc': proc, 'go': go, 'ready': ready, 'extra': None}

    def register(self, wrapper, qid):
        wrapper['go'].write_text(qid)
        deadline = time.time() + 30
        while not wrapper['ready'].exists():
            self.assertLess(time.time(), deadline, 'stand-in wrapper did not start')
            time.sleep(0.05)
        flag, _, extra = wrapper['ready'].read_text().partition(' ')
        self.assertEqual(flag, '1', 'the real registry writer refused the stand-in')
        if extra.strip().isdigit():
            wrapper['extra'] = int(extra)
            self.mine.add(wrapper['extra'])
            self.addCleanup(lambda pid=wrapper['extra']: self._kill_own(pid))
        return wrapper

    def _kill_own(self, pid):
        try:
            os.kill(pid, signal.SIGKILL)      # a throwaway child of THIS test, cleaned up here only
        except OSError:
            pass

    def alive(self, proc):
        return proc.poll() is None

    # ---- scheduler -----------------------------------------------------------------------------------------------
    def enqueue(self, ticket, waiter=None, session='0b1d2e3f-aaaa-bbbb-cccc-1234567890ab', now=None):
        out = da.queue_enqueue(self.path, label='acc-' + ticket.lower(), tickets=[ticket], repo='horonom/x', worktree=str(self.worktree), harness='codex',
                               session_id=session, nbytes=20 * GIB, argv=ARGV + [ticket], now=now or self.clock(),
                               waiter_pid=waiter.pid if waiter else None, ancestor_fn=(lambda pid: True) if waiter else None)
        self.assertTrue(out['ok'], out)
        return out['id']

    def wrapped_entry(self, ticket, kind='heavy-run', mode='plain'):
        wrapper = self.stand_in(kind, mode)
        qid = self.enqueue(ticket, waiter=wrapper['proc'])
        wrapper['qid'] = qid
        return self.register(wrapper, qid)

    def entries(self):
        return {e['id']: e for e in da.queue_status(self.path)['entries']}

    def state_of(self, qid):
        return self.entries()[qid]['state']

    def cancel(self, qid):
        self.assertTrue(da.queue_cancel(self.path, qid)['ok'])

    # ---- monitor ------------------------------------------------------------------------------------------------
    def recovery_env(self, directory):
        renv = mon.Env._recovery_env(self.current_env, directory)
        real_kill = renv.kill_fn

        def kill(pid, sig):
            self.assertIn(pid, self.mine, 'the monitor tried to signal a process this test did not spawn')
            self.kills.append((pid, sig))
            real_kill(pid, sig)
        renv.kill_fn = kill
        if not (os.path.exists('/usr/sbin/lsof') or os.path.exists('/usr/bin/lsof')):      # a runner without lsof: the heuristic is unit-tested below
            renv.holds_workload_fn = lambda pid: mon.holds_workload(renv.table_fn, pid, lsof=lambda p: 'n/dev/null\n')
        return renv

    def env(self, **kw):
        env = mon.Env(da, self.path, self.memory_state, mon.default_limits(da, ma), clock=self.clock, mono=self.clock.mono, boot=lambda: 111,
                      table=lambda: {}, memory=lambda: {'latched': False, 'ok_streak': 0, 'metrics': dict(CALM)}, free=lambda: 300 * GIB,
                      advance=lambda: self.advances.append(1) or {'exit': 0}, notify=lambda a: self.alerts.append(a) or True, clear=lambda: True,
                      emit_incident=lambda alert: self.alerts.append(alert) or dict(self.emit_result),
                      owner_sessions=lambda: self.sessions, recovery_env=self.recovery_env, **kw)
        self.current_env = env
        return env

    def tick(self, seconds=65, act=True, module=None, env=None):
        self.clock.offset += seconds
        return (module or mon).tick(env or self.env(), act=act)

    def to_action(self):
        """Two preparatory ticks (the first only observes, the second starts the two-tick confirmation clock); the NEXT tick may act."""
        self.tick(0)
        self.tick(65)

    def recovery_state(self):
        return mon.read_json(self.monitor_dir / 'state.json', {}).get('recoveries', {})

    def receipts(self, kind=None):
        path = self.monitor_dir / 'recovery-receipts.jsonl'
        lines = [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
        return [x for x in lines if kind is None or x['kind'] == kind]

    def wait_gone(self, proc):
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.fail('process did not end')

    # ==== (a) obsolete wrapper: one SIGTERM, only that wrapper, verified on a later tick ============================
    def test_a_cancelled_entrys_wrapper_gets_one_sigterm_and_nothing_else(self):
        target = self.wrapped_entry('HORO-3001')
        decoy = self.wrapped_entry('HORO-3002')          # a registered wrapper whose entry is still live (QUEUED)
        bystander = self.spawn(['/bin/sleep', '300'])    # an unregistered process
        self.cancel(target['qid'])
        self.to_action()
        result = self.tick(65)
        self.wait_gone(target['proc'])
        self.assertEqual(target['proc'].returncode, -signal.SIGTERM)
        self.assertEqual(self.kills, [(target['proc'].pid, signal.SIGTERM)])
        self.assertEqual(result['recovery']['acted'], [{'id': target['qid'], 'action': 'wrapper', 'result': 'terminated'}])
        self.assertTrue(self.alive(decoy['proc']) and self.alive(bystander))
        self.assertEqual(self.state_of(decoy['qid']), 'QUEUED')
        sent = [r for r in self.receipts('terminate') if r['action'] == 'sigterm']
        self.assertEqual([r['result'] for r in sent], ['intent', 'terminated'])
        # verified on the next tick, exactly once, and nothing more happens afterwards
        later = self.tick(65)
        self.assertEqual(later['recovery']['verified'], [{'id': target['qid'], 'kind': 'wrapper', 'state': 'verified'}])
        self.tick(65)
        self.assertEqual(len(self.receipts('verify')), 1)
        self.assertEqual(len(self.kills), 1)
        record = list(self.recovery_state().values())[0]
        self.assertEqual((record['outcome'], record['verify']), ('terminated', 'verified'))

    def test_a_reused_pid_is_refused(self):
        wrapper = self.wrapped_entry('HORO-3003')
        self.cancel(wrapper['qid'])
        path = self.registry / (wrapper['qid'] + '.json')
        doc = json.loads(path.read_text())
        doc['start_token'] = 'Mon Jan  1 00:00:00 2001'      # the registered start token no longer matches the live process
        path.write_text(json.dumps(doc))
        self.to_action()
        for _ in range(3):
            self.tick(65)
        self.assertTrue(self.alive(wrapper['proc']))
        self.assertEqual((self.kills, self.receipts('terminate')), ([], []))

    def test_a_survivor_is_reported_and_never_escalated(self):
        wrapper = self.wrapped_entry('HORO-3004', mode='ignore-term')
        self.cancel(wrapper['qid'])
        self.to_action()
        result = self.tick(65)
        self.assertEqual(result['recovery']['acted'], [{'id': wrapper['qid'], 'action': 'wrapper', 'result': 'failed-to-terminate'}])
        for _ in range(3):
            self.tick(65)
        self.assertTrue(self.alive(wrapper['proc']))
        self.assertEqual(self.kills, [(wrapper['proc'].pid, signal.SIGTERM)])      # one SIGTERM, ever
        causes = [a['cause'] for a in self.alerts if a['cause'] == 'failed-to-terminate']
        self.assertEqual((causes, [a['owner_action'] for a in self.alerts if a['cause'] == 'failed-to-terminate']), (['failed-to-terminate'], ['inspect-stuck-wrapper']))

    # ==== (b) / (i) a workload is never terminated, only reported ===================================================
    def test_b_wrapper_with_a_live_child_workload_is_reported_not_terminated(self):
        wrapper = self.wrapped_entry('HORO-3005', mode='child')
        self.cancel(wrapper['qid'])
        self.to_action()
        result = self.tick(65)
        self.assertTrue(self.alive(wrapper['proc']))
        self.assertEqual(self.kills, [])
        reported = result['recovery']['reported']
        self.assertEqual([r['id'] for r in reported], [wrapper['qid']])
        self.assertTrue(set(reported[0]['refused']) & {'holds-workload', 'child-workload-running'}, reported)

    def test_i_a_detached_child_is_reported_not_signalled(self):
        wrapper = self.wrapped_entry('HORO-3006', mode='detached')
        self.cancel(wrapper['qid'])
        self.to_action()
        result = self.tick(65)
        self.assertTrue(self.alive(wrapper['proc']))
        self.assertEqual(self.kills, [])
        self.assertTrue(set(result['recovery']['reported'][0]['refused']) & {'holds-workload', 'detached-workload-present'}, result['recovery'])
        os.kill(wrapper['extra'], 0)      # the orphan is still there

    # ==== (c) abandoned unstarted grant: cancelled through the scheduler, FIFO intact, continuation verified ========
    def granted_pair(self, owner):
        """A is GRANTED and unclaimed for 700 s, B queued behind it. `owner` makes A's waiter (a process the test may end) or None for a hand-off."""
        stamp = datetime.now(timezone.utc) - timedelta(seconds=700)
        a = self.enqueue('HORO-3101', waiter=owner, now=stamp)
        b = self.enqueue('HORO-3102', now=stamp + timedelta(seconds=5))
        self.assertEqual(da.queue_advance(self.path, lambda: 400 * GIB, now=stamp + timedelta(seconds=10))['granted'], [a])
        return a, b

    def claim(self, qid, ticket, holder):
        out = da.queue_claim(self.path, qid, 'acc-' + ticket.lower(), 20 * GIB, ARGV + [ticket], str(self.worktree), lambda: 400 * GIB, pid=holder.pid,
                             memory_fn=lambda: (True, 'ok'), exec_fn=lambda program, args: None)
        self.assertTrue(out['ok'], out)

    def test_c_dead_waiter_grant_is_cancelled_and_the_next_entry_continues(self):
        waiter = self.spawn(['/bin/sleep', '300'])
        a, b = self.granted_pair(waiter)
        waiter.kill()
        waiter.wait()
        before = sorted(self.entries())
        self.tick(0)
        result = self.tick(65)
        self.assertEqual(result['recovery']['acted'], [{'id': a, 'action': 'grant', 'result': 'cancelled'}])
        self.assertEqual((self.state_of(a), self.state_of(b)), ('CANCELLED', 'QUEUED'))      # FIFO unchanged, B not skipped or duplicated
        self.assertEqual(sorted(self.entries()), before)
        self.assertEqual(self.kills, [])
        notice = [x for x in self.alerts if x['cause'] == 'abandoned-unstarted-cancelled']
        self.assertEqual(len(notice), 1)
        # the waiting job polls `queue advance` as heavy-run does: B becomes the grant, the incident stays pending until B starts
        self.assertEqual(da.queue_advance(self.path, lambda: 400 * GIB)['granted'], [b])
        self.assertEqual(self.tick(65)['recovery']['verified'][0]['state'], 'pending')
        holder = self.spawn(['/bin/sleep', '300'])
        self.claim(b, 'HORO-3102', holder)
        self.assertEqual(self.tick(65)['recovery']['verified'][0]['state'], 'verified')
        self.tick(65)
        self.assertEqual([r['result'] for r in self.receipts('cancel')], ['cancelled'])
        self.assertEqual(len([r for r in self.receipts('verify') if r['result'] == 'verified']), 1)

    def test_c_hand_off_whose_session_ended_is_cancelled(self):
        a, b = self.granted_pair(None)
        self.sessions = {'0b1d2e3f-aaaa-bbbb-cccc-1234567890ab': {'kind': 'claude', 'live': False}}
        self.tick(0)
        result = self.tick(65)
        self.assertEqual(result['recovery']['acted'], [{'id': a, 'action': 'grant', 'result': 'cancelled'}])
        self.assertEqual((self.state_of(a), self.state_of(b)), ('CANCELLED', 'QUEUED'))

    # ==== (d) a live or unknown owner is never cancelled ===========================================================
    def test_d_live_or_unknown_owner_is_not_cancelled(self):
        live = self.spawn(['/bin/sleep', '300'])
        a, _ = self.granted_pair(live)
        for _ in range(4):
            self.tick(65)
        self.assertEqual(self.state_of(a), 'GRANTED')

    def test_d_hand_off_with_live_or_unknown_owner_is_not_cancelled(self):
        a, _ = self.granted_pair(None)
        for sessions in ({}, {'0b1d2e3f-aaaa-bbbb-cccc-1234567890ab': {'kind': 'claude', 'live': True}}):
            self.sessions = sessions
            for _ in range(3):
                result = self.tick(65)
            self.assertEqual(self.state_of(a), 'GRANTED', sessions)
            self.assertEqual(result['recovery']['acted'], [])

    # ==== (e) kill switch and unsafe directory ====================================================================
    def test_e_kill_switch_and_unsafe_directory_make_recovery_a_no_op(self):
        wrapper = self.wrapped_entry('HORO-3201')
        self.cancel(wrapper['qid'])
        self.tick(0)
        (self.monitor_dir / mon.RECOVERY_KILL_SWITCH).write_text('')
        for _ in range(4):
            self.assertEqual(self.tick(65)['recovery']['state'], 'disabled')
        self.assertTrue(self.alive(wrapper['proc']))
        (self.monitor_dir / mon.RECOVERY_KILL_SWITCH).unlink()
        os.chmod(self.monitor_dir, 0o755)
        for _ in range(4):
            self.assertEqual(self.tick(65)['recovery']['state'], 'unsafe-monitor-dir')
        os.chmod(self.monitor_dir, 0o700)
        self.assertTrue(self.alive(wrapper['proc']))
        self.assertEqual(self.kills, [])
        self.tick(65)
        self.tick(65)
        self.tick(65)
        self.wait_gone(wrapper['proc'])      # switched back on, the same situation is then recovered

    # ==== (f) bound: 3 per rolling 30 minutes, 1 per tick ==========================================================
    def test_f_at_most_three_actions_per_thirty_minutes(self):
        wrappers = [self.wrapped_entry('HORO-33%02d' % n) for n in range(4)]
        for w in wrappers:
            self.cancel(w['qid'])
        self.to_action()
        acted = [len(self.tick(65)['recovery']['acted']) for _ in range(3)]
        self.assertEqual(acted, [1, 1, 1])                       # one per tick
        blocked = self.tick(65)
        self.assertEqual((blocked['recovery']['state'], blocked['recovery']['acted']), ('recovery-bound', []))
        self.assertEqual(len(self.kills), 3)
        survivors = [w for w in wrappers if self.alive(w['proc'])]
        self.assertEqual(len(survivors), 1)
        self.tick(1800)      # the window slides (and the long gap restarts the observation clock)
        self.tick(65)
        self.tick(65)
        self.wait_gone(survivors[0]['proc'])
        self.assertEqual(len(self.kills), 4)

    # ==== (g) two monitors racing act once ==========================================================================
    def test_g_concurrent_ticks_act_once(self):
        wrapper = self.wrapped_entry('HORO-3401')
        self.cancel(wrapper['qid'])
        self.to_action()
        self.clock.offset += 65
        barrier, results = threading.Barrier(2), []

        def run():
            env = self.env()
            barrier.wait()
            results.append(mon.tick(env, act=True))
        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.wait_gone(wrapper['proc'])
        self.assertEqual(len(self.kills), 1)
        self.assertEqual(len([r for r in self.receipts('terminate') if r['action'] == 'sigterm' and r['result'] == 'intent']), 1)
        self.assertEqual(len(self.recovery_state()), 1)

    # ==== (h) restart between the action and its verification ======================================================
    def test_h_restart_keeps_the_incident_and_verifies_once(self):
        wrapper = self.wrapped_entry('HORO-3501')
        self.cancel(wrapper['qid'])
        self.to_action()
        self.tick(65)
        self.wait_gone(wrapper['proc'])
        fresh = load('admission-monitor', 'admission_monitor_restarted')      # a new process image: nothing but the state files survives
        self.assertEqual(len(self.recovery_state()), 1)
        again = self.tick(65, module=fresh)
        self.assertEqual([v['state'] for v in again['recovery']['verified']], ['verified'])
        self.tick(65, module=fresh)
        self.tick(65)
        self.assertEqual(len(self.receipts('verify')), 1)
        self.assertEqual(len(self.kills), 1)

    # ==== (j) a healthy long-running job is never touched ==========================================================
    def test_j_claimed_job_under_deadline_is_never_touched(self):
        wrapper = self.wrapped_entry('HORO-3601')
        self.assertEqual(da.queue_advance(self.path, lambda: 400 * GIB)['granted'], [wrapper['qid']])
        holder = self.spawn(['/bin/sleep', '300'])
        self.claim(wrapper['qid'], 'HORO-3601', holder)
        for _ in range(6):
            result = self.tick(65)
        self.assertEqual((self.state_of(wrapper['qid']), self.alive(wrapper['proc']), self.alive(holder)), ('CLAIMED', True, True))
        self.assertEqual((self.kills, result['recovery']['acted'], result['recovery']['reported']), ([], [], []))

    # ==== (k) observe-only and plain ticks never act ================================================================
    def test_k_observe_only_never_acts(self):
        wrapper = self.wrapped_entry('HORO-3701')
        self.cancel(wrapper['qid'])
        waiter = self.spawn(['/bin/sleep', '300'])
        a, _ = self.granted_pair(waiter)
        waiter.kill()
        waiter.wait()
        for _ in range(6):
            result = self.tick(65, act=False)
        self.assertNotIn('recovery', result)
        self.assertTrue(self.alive(wrapper['proc']))
        self.assertEqual((self.kills, self.state_of(a), self.receipts()), ([], 'GRANTED', []))

    # ==== the notice reaches the real session-notify enums ========================================================
    def test_notice_phrases_are_accepted_by_session_notify(self):
        for cause, phrase, action in (('obsolete-wrapper-terminated', 'wrapper-terminated', 'check-recovery-result'),
                                      ('abandoned-unstarted-cancelled', 'grant-cancelled', 'check-recovery-result'),
                                      ('failed-to-terminate', 'termination-failed', 'inspect-stuck-wrapper')):
            alert = {'cause': cause, 'signature': mon.incident_signature('k-' + cause, 'x'), 'owner_action': action, 'recovery': [{'recovery': phrase}]}
            out = mon.session_notify_incident(alert)
            self.assertIn(out['result'], ('queued', 'no-backend'), (cause, out))

    def test_notices_carry_no_pid_token_or_command_line(self):
        wrapper = self.wrapped_entry('HORO-3801')
        self.cancel(wrapper['qid'])
        self.to_action()
        self.tick(65)
        self.wait_gone(wrapper['proc'])
        self.tick(65)
        lines = (self.monitor_dir / 'owner-outbox.jsonl').read_text().splitlines()
        self.assertEqual(len(lines), 1)
        line = json.loads(lines[0])
        self.assertEqual((line['clock'], line['cause'], line['recovery'], line['job']), ('recovery', 'obsolete-wrapper-terminated', 'wrapper-terminated', wrapper['qid']))
        blob = json.dumps(line) + json.dumps(self.alerts)
        for secret in (str(wrapper['proc'].pid), 'heavy-run', sys.executable, str(self.root)):
            self.assertNotIn(secret, blob)
        self.assertEqual(len([a for a in self.alerts if a['cause'] == 'obsolete-wrapper-terminated']), 1)      # delivered once, not again


class HoldsWorkload(unittest.TestCase):
    """The workload-evidence binding: anything uncertain, a child, a group mate or an open cargo lock counts as holding."""
    TABLE = {100: (1, 100, 'S', 'a'), 101: (100, 100, 'S', 'b'), 102: (1, 100, 'S', 'c'), 103: (1, 103, 'S', 'd'), 104: (100, 100, 'Z', 'e')}

    def holds(self, pid, table=None, lsof=lambda p: 'n/dev/null\nn/tmp/log\n'):
        return mon.holds_workload(lambda: table or self.TABLE, pid, lsof=lsof)

    def test_a_child_or_group_mate_holds(self):
        self.assertTrue(self.holds(100))                      # 101 is a child, 102 a group mate
        self.assertFalse(self.holds(103))                     # alone in its group, nothing below it, ordinary files only

    def test_a_zombie_child_does_not_hold(self):
        self.assertFalse(self.holds(100, {100: (1, 100, 'S', 'a'), 104: (100, 100, 'Z', 'e')}))

    def test_an_open_cargo_lock_holds(self):
        self.assertTrue(self.holds(103, lsof=lambda p: 'n/work/target/debug/.cargo-lock\n'))

    def test_anything_uncertain_holds(self):
        self.assertTrue(self.holds(999))                      # not in the table
        self.assertTrue(mon.holds_workload(lambda: (_ for _ in ()).throw(OSError('ps')), 103))
        self.assertTrue(self.holds(103, lsof=lambda p: ''))   # nothing listed: cannot tell
        self.assertTrue(self.holds(103, lsof=lambda p: (_ for _ in ()).throw(OSError('lsof'))))

    def test_only_sigterm_to_one_positive_pid_passes_the_signal_binding(self):
        for pid, sig in ((1, signal.SIGTERM), (0, signal.SIGTERM), (-5, signal.SIGTERM), (os.getpid(), signal.SIGKILL), (os.getpid(), signal.SIGHUP), ('12', signal.SIGTERM)):
            with self.assertRaises(OSError):
                mon.sigterm_only(pid, sig)


if __name__ == '__main__':
    unittest.main()
