#!/usr/bin/env python3
"""SPE-188: an incident the monitor announced is closed with ONE `recovered` spool transition once it is over, so the supervisor stops listing it as open.

Scratch state only (temp directories, injected notifier): nothing here reaches the owner's real queue, monitor state, supervisor spool or any notification
backend."""
import hashlib
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]


def load_test(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / 'tests' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tm = load_test('test-admission-monitor')
zs = load_test('test-zeroclaw-spool')
mon, da, sn = tm.mon, tm.da, zs.sn
GIB = tm.GIB
REF = 'inc:0123456789abcdef'


class ClosingNotice(zs.Spool):
    """session-notify side: the closing notice is one closed-schema `recovered` file with the incident's own ref."""

    def close(self, engine=None, ref=REF, **over):
        engine = engine or self.engine()
        req = {'cause': 'incident-resolved', 'recovery': 'resolved', 'owner_action': 'no-action-needed', 'signature': '1111111111111111', 'incident_ref': ref}
        req.update(over)
        return engine.emit_resource(req), engine

    def test_the_closing_notice_is_one_closed_schema_recovered_file_keyed_by_ref_and_transition(self):
        result, engine = self.close()
        self.assertEqual((result, engine.last_delivery), ('queued', 'sent'))
        (path,) = self.files()
        doc = json.loads(path.read_text())
        self.assertTrue(zs.check_schema(doc))
        self.assertEqual(doc['key'], hashlib.sha256((REF + '|recovered').encode()).hexdigest()[:32])
        self.assertEqual(path.name, doc['key'] + '.json')
        self.assertEqual((doc['incident_ref'], doc['kind'], doc['transition'], doc['severity']), (REF, 'recovered', 'recovered', 'info'))
        self.assertEqual((doc['progress'], doc['action']), ('incident resolved; recovery: resolved by the scheduler', 'no action needed'))

    def test_the_same_closing_notice_twice_is_one_file(self):
        self.close()
        self.close()
        self.assertEqual(len(self.files()), 1)

    def test_it_follows_opened_and_escalated_as_a_third_distinct_file_of_the_same_incident(self):
        self.emit(sig='2222222222222222')
        self.emit(sig='3333333333333333', recovery='lapsed')
        self.close()
        docs = self.docs()
        self.assertEqual(sorted(d['transition'] for d in docs), ['escalated', 'opened', 'recovered'])
        self.assertEqual(len(self.files()), 3)

    def test_closing_one_incident_leaves_another_blockage_on_the_record_as_it_was(self):
        self.emit(cause='disk-floor', recovery='observe-only', action='reclaim-disk', sig='4444444444444444')
        self.assertEqual(self.record()['state'], 'BLOCKED_RESOURCE')
        self.close()
        self.assertEqual(self.record()['state'], 'BLOCKED_RESOURCE')

    def test_the_closing_phrases_only_go_together_and_always_name_the_incident(self):
        self.assertEqual(self.close(recovery='none')[0], 'invalid')
        self.assertEqual(self.close(owner_action='inspect-admission-state')[0], 'invalid')
        self.assertEqual(self.close(incident_ref=None)[0], 'invalid')
        result, _ = self.emit(recovery='resolved')
        self.assertEqual(result, 'invalid')                                    # the closing recovery phrase is not usable by an opening cause
        result, _ = self.emit(action='no-action-needed')
        self.assertEqual(result, 'invalid')
        self.assertEqual(self.files(), [])

    def all_backends(self):
        cfg = self.cfg()
        for name in ('macos', 'ntfy', 'pushover', 'telegram'):
            cfg['backends'][name]['enabled'] = True                            # telegram keeps its default kinds, which include `recovered`
            if name in sn.REMOTE_BACKENDS:
                cfg['backends'][name]['approved_destination'] = 'owner'
        sent = []
        transports = {n: (lambda payload, b, n=n: sent.append(n) or 'sent') for n in ('macos', 'ntfy', 'pushover', 'telegram')}
        transports['zeroclaw-spool'] = sn.REAL_TRANSPORTS['zeroclaw-spool']
        return cfg, transports, sent

    def test_the_closing_notice_goes_to_the_spool_only_never_to_a_direct_owner_backend(self):
        cfg, transports, sent = self.all_backends()
        result, engine = self.close(engine=self.engine(cfg, transports))
        engine.sweep()
        self.assertEqual(result, 'queued')
        self.assertEqual(len(self.files()), 1)
        self.assertEqual(sent, [])
        codes = {n: b['code'] for e in self.record()['pending'] for n, b in e['backends'].items() if n != 'zeroclaw-spool'}
        self.assertEqual(set(codes.values()) - {'kind-not-routed', 'disabled'}, set())

    def test_the_sign_in_recovered_notice_still_reaches_the_direct_backends(self):
        cfg, transports, sent = self.all_backends()
        cfg['backends']['zeroclaw-spool']['enabled'] = False                   # the sign-in path predates the spool; its routing is not touched
        engine = self.engine(cfg, transports)
        engine.emit_resource({'cause': 'signin-recovered', 'recovery': 'sessions-resumed', 'owner_action': 'verify-resumed-work', 'signature': '5555555555555555'})
        engine.sweep()
        self.assertTrue(sent)

    def test_a_closing_notice_adds_no_new_routing_conflict(self):
        cfg, _, _ = self.all_backends()
        before = sn.routing_conflict(cfg)
        self.close(engine=self.engine(cfg))
        self.assertEqual(sn.routing_conflict(cfg), before)                     # the check reads configured kinds only, never a cause

    def test_the_banner_of_a_closing_notice_says_resolved_not_stalled(self):
        fields = {'state': 'IDLE', 'cause': 'incident-resolved', 'recovery': 'resolved', 'owner_action': 'no-action-needed', 'incident_ref': REF}
        self.assertEqual(sn.render(fields)['title'], 'Claude Code: admission incident resolved')

    def test_an_unusable_spool_keeps_the_closing_notice_pending_then_delivers_it_once(self):
        import shutil
        shutil.rmtree(self.spool)
        result, engine = self.close()
        self.assertEqual((result, engine.last_delivery), ('queued', 'pending'))
        self.spool.mkdir(mode=0o700)
        self.spool.chmod(0o700)
        self.t += 120
        self.engine().sweep()
        self.assertEqual(len(self.files()), 1)
        self.t += 120
        self.engine().sweep()
        self.assertEqual(len(self.files()), 1)


class ClosingBase(tm.MonitorBase):
    """Shared harness for the monitor-side tests: an injected closing notifier and scratch scheduler state."""

    def setUp(self):
        super().setUp()
        self.mem['latched'] = False
        self.closing = []
        self.closing_result = {'result': 'queued', 'delivery': 'sent'}

    def emit_recovered(self, alert):
        self.closing.append(alert)
        self.alerts.append(dict(alert, closing=True))
        if isinstance(self.closing_result, Exception):
            raise self.closing_result
        return dict(self.closing_result)

    def walk(self, seconds):
        out = None
        for _ in range(seconds // 60):
            out = self.tick(60)
        return out

    def entry(self, qid):
        return [e for e in da._queue_entries(self.state_path)[0] if e['id'] == qid][0]

    def incidents(self):
        return json.loads((mon.monitor_dir(self.state_path) / 'state.json').read_text()).get('incidents', {})

    def granted_handoff(self, ticket='HORO-3101'):
        qid = self.enqueue(ticket)
        self.grant()
        self.assertEqual(self.entry(qid)['state'], 'GRANTED')
        return qid

    def lapse(self, qid):
        self.walk(900)
        self.clock.advance(300)
        da.queue_advance(self.state_path, lambda: 400 * GIB, now=self.clock.now)
        self.walk(300)

    def claim(self, qid, pid=4242):
        entry = self.entry(qid)
        entry.update(state='CLAIMED', holder={'pid': pid, 'start_token': 't'})
        da._write_queue_entry(self.state_path, entry)
        self.table = {pid: (1, 10.0)}



class ClosingIncidents(ClosingBase):
    """admission-monitor side: closing of resolved incidents."""

    def test_an_unannounced_wait_incident_is_never_closed_even_with_attempts_and_a_resolution(self):
        wall = 1_000_000.0
        rec = mon.new_incident('q000001', 'escalated', wall - 9000, wall + 600, wall - 9000, None, 'wait', 'none')
        rec.update(resolved_at=wall - 100, attempts=3)                         # its opening notice kept failing: nothing was ever announced
        self.assertIsNone(rec['notified_phase'])
        sent = []
        env = types.SimpleNamespace(emit_recovered=lambda a: sent.append(a) or {'result': 'queued'})
        mon.close_incidents(env, {'q000001:wait': rec}, wall)
        self.assertEqual(sent, [])
        self.assertIsNone(rec['recovered_at'])

    def test_a_failing_closing_notifier_is_called_only_as_often_as_the_backoff_allows(self):
        wall = 1_000_000.0
        rec = mon.new_incident('q000002', 'escalated', wall - 9000, wall + 600, wall - 9000, None, 'wait', 'none')
        rec.update(resolved_at=wall - 100, notified_phase='escalated')
        calls = []
        env = types.SimpleNamespace(emit_recovered=lambda a: calls.append(a) or {'result': 'failed', 'delivery': None})
        for second in range(600):                                              # a tick every second for ten minutes
            mon.close_incidents(env, {'q000002:wait': rec}, wall + second)
        self.assertGreaterEqual(len(calls), 2)
        self.assertLessEqual(len(calls), 5)                                    # 60 s, 120 s, 240 s ... not one call per tick

    def test_a_resolved_incident_is_closed_with_one_notice_naming_its_own_ref(self):
        qid = self.granted_handoff()
        self.lapse(qid)
        (key, rec), = self.incidents().items()
        self.assertIsNotNone(rec['resolved_at'])
        (alert,) = self.closing
        self.assertEqual(alert['incident_ref'], mon.incident_ref(key))
        self.assertEqual(alert['signature'], mon.incident_signature(key, 'recovered'))
        self.assertIsNotNone(rec['recovered_at'])

    def test_repeated_ticks_and_a_restart_never_close_it_again(self):
        qid = self.granted_handoff()
        self.lapse(qid)
        self.assertEqual(len(self.closing), 1)
        self.walk(1800)                                                        # many more ticks; every tick builds a fresh Env from the saved state (a restart)
        self.assertEqual(len(self.closing), 1)

    def test_an_incident_that_was_never_announced_is_not_closed(self):
        self.emit_result = {'result': 'failed', 'delivery': None}              # session-notify never took the opening notice
        qid = self.granted_handoff()
        self.lapse(qid)
        (rec,) = self.incidents().values()
        self.assertIsNone(rec['notified_phase'])
        self.assertEqual(self.closing, [])
        self.assertIsNone(rec['recovered_at'])

    def test_an_acknowledged_incident_is_closed_too(self):
        qid = self.granted_handoff()
        self.walk(960)
        self.claim(qid)
        for i in range(4):
            self.table = {4242: (1, 10.0 + i)}
            self.tick()
        (rec,) = self.incidents().values()
        self.assertIsNotNone(rec['acknowledged_at'])
        self.assertEqual(len(self.closing), 1)

    def test_a_failed_closing_notice_is_retried_and_counted_once_it_succeeds(self):
        qid = self.granted_handoff()
        self.closing_result = {'result': 'failed', 'delivery': None}
        self.lapse(qid)
        (rec,) = self.incidents().values()
        self.assertIsNone(rec['recovered_at'])
        self.assertGreaterEqual(rec['recovered_tries'], 1)
        tries = len(self.closing)
        self.closing_result = {'result': 'queued', 'delivery': 'sent'}
        self.walk(1200)
        (rec,) = self.incidents().values()
        self.assertIsNotNone(rec['recovered_at'])
        after = len(self.closing)
        self.assertGreater(after, tries)
        self.walk(600)
        self.assertEqual(len(self.closing), after)

    def test_a_notifier_that_keeps_failing_never_blocks_the_tick_and_stops_with_the_record(self):
        qid = self.granted_handoff()
        self.closing_result = OSError('notifier unavailable')
        self.lapse(qid)
        out = self.walk(9 * 3600)                                              # the record is pruned hours after it resolves; retries back off meanwhile
        self.assertIsNotNone(out)
        self.assertGreaterEqual(len(self.closing), 2)
        self.assertLessEqual(len(self.closing), mon.RECOVERED_MAX_TRIES)
        n = len(self.closing)
        self.walk(1800)
        self.assertEqual(len(self.closing), n)                                 # nothing left to retry once the record is gone

    def test_the_closing_notice_comes_after_the_opening_and_lapse_notices(self):
        qid = self.granted_handoff()
        self.lapse(qid)
        closings = [i for i, a in enumerate(self.alerts) if a.get('closing')]
        openings = [i for i, a in enumerate(self.alerts) if not a.get('closing')]
        self.assertEqual(len(closings), 1)
        self.assertTrue(openings and max(openings) < closings[0])

    def test_closing_is_a_notice_not_an_action_it_runs_in_observe_only_mode(self):
        qid = self.granted_handoff()
        self.lapse(qid)
        self.assertEqual(len(self.closing), 1)
        self.assertEqual(self.advances, [])                                    # the monitor never advanced the queue itself

    def test_the_closing_alert_carries_only_the_fixed_fields(self):
        qid = self.granted_handoff()
        self.lapse(qid)
        (alert,) = self.closing
        self.assertEqual(sorted(alert), ['cause', 'incident_ref', 'signature'])
        self.assertEqual(alert['cause'], 'incident-resolved')

    def test_the_real_notifier_call_has_the_fixed_phrases_and_the_incident_ref(self):
        seen = []

        def runner(argv, **kw):
            seen.append(argv)
            return type('Done', (), {'returncode': 0, 'stdout': 'emit-resource: queued delivery=sent\n'})()
        out = mon.session_notify_recovered({'cause': 'incident-resolved', 'signature': '1' * 16, 'incident_ref': REF}, runner=runner)
        self.assertEqual(out, {'result': 'queued', 'delivery': 'sent'})
        (argv,) = seen
        for pair in (('--cause', 'incident-resolved'), ('--recovery', 'resolved'), ('--owner-action', 'no-action-needed'), ('--incident-ref', REF)):
            self.assertEqual(argv[argv.index(pair[0]) + 1], pair[1])
        self.assertIn('--strict', argv)


class Reconcile(ClosingBase):
    """The one-shot command for incidents the supervisor already lists as open (the monitor's own records of old ones have been pruned)."""

    def status_file(self, refs):
        path = Path(self.temp.name) / 'incident-status.json'
        path.write_text(json.dumps({'open_incidents': [{'incident_ref': r, 'kind': 'unclaimed-handoff', 'ts': '2026-11-24T00:00:00Z'} for r in refs]}))
        return path

    def refs(self, qid):
        entry = self.entry(qid)
        return mon.incident_ref(mon.incident_key(entry))

    def scenario(self):
        done = self.granted_handoff('HORO-3201')
        self.walk(900)
        self.clock.advance(300)
        da.queue_advance(self.state_path, lambda: 400 * GIB, now=self.clock.now)
        live = self.enqueue('HORO-3202')
        self.grant()
        self.assertEqual((self.entry(done)['state'], self.entry(live)['state']), ('EXPIRED', 'GRANTED'))
        return done, live

    def test_dry_run_lists_what_it_would_close_and_writes_nothing(self):
        done, live = self.scenario()
        status = self.status_file([self.refs(done), self.refs(live), 'inc:ffffffffffffffff'])
        before = sorted((p.name, p.stat().st_mtime_ns) for p in Path(self.temp.name).rglob('*') if p.is_file() and p != status)
        out = mon.reconcile_incidents(self.env(), status)
        actions = {row['ref']: row['action'] for row in out['rows']}
        self.assertEqual(actions, {self.refs(done): 'closable', self.refs(live): 'keep', 'inc:ffffffffffffffff': 'unmapped'})
        self.assertTrue(out['dry_run'])
        self.assertEqual(self.closing, [])
        self.assertEqual(before, sorted((p.name, p.stat().st_mtime_ns) for p in Path(self.temp.name).rglob('*') if p.is_file() and p != status))

    def test_apply_sends_one_closing_notice_for_each_closable_ref_only(self):
        done, live = self.scenario()
        status = self.status_file([self.refs(done), self.refs(live), 'inc:ffffffffffffffff'])
        out = mon.reconcile_incidents(self.env(), status, apply=True)
        self.assertEqual(out['applied'], 1)
        (alert,) = self.closing
        self.assertEqual(alert['incident_ref'], self.refs(done))
        self.assertEqual({row['action'] for row in out['rows']}, {'sent', 'keep', 'unmapped'})
        self.assertEqual(self.advances, [])

    def test_one_apply_sends_at_most_the_cap_and_a_failed_send_is_reported(self):
        done, live = self.scenario()
        status = self.status_file([self.refs(done)])
        old = mon.RECONCILE_MAX_SENDS
        mon.RECONCILE_MAX_SENDS = 0
        try:
            out = mon.reconcile_incidents(self.env(), status, apply=True)
        finally:
            mon.RECONCILE_MAX_SENDS = old
        self.assertEqual((out['applied'], self.closing), (0, []))
        self.assertEqual([r['action'] for r in out['rows']], ['closable'])
        self.closing_result = {'result': 'failed', 'delivery': None}
        out = mon.reconcile_incidents(self.env(), status, apply=True)
        self.assertEqual((out['failed'], [r['action'] for r in out['rows']]), (1, ['failed']))

    def test_the_command_exits_non_zero_when_an_apply_send_failed(self):
        import io
        from contextlib import redirect_stdout
        done, live = self.scenario()
        status = self.status_file([self.refs(done)])
        self.closing_result = {'result': 'failed', 'delivery': None}
        env = self.env()
        old = mon.Env
        try:
            mon.Env = lambda *a, **k: env
            with redirect_stdout(io.StringIO()):
                rc = mon.main(['reconcile-incidents', '--status-file', str(status), '--apply'])
        finally:
            mon.Env = old
        self.assertEqual(rc, 1)

    def test_a_missing_or_unreadable_status_file_closes_nothing(self):
        self.scenario()
        out = mon.reconcile_incidents(self.env(), Path(self.temp.name) / 'absent.json', apply=True)
        self.assertEqual((out['rows'], out['applied']), ([], 0))
        self.assertEqual(self.closing, [])

    def test_the_command_is_dry_run_unless_apply_is_given(self):
        import io
        from contextlib import redirect_stdout
        done, live = self.scenario()
        status = self.status_file([self.refs(done)])
        buf = io.StringIO()
        old = mon.Env
        env = self.env()
        try:
            mon.Env = lambda *a, **k: env
            with redirect_stdout(buf):
                rc = mon.main(['reconcile-incidents', '--status-file', str(status)])
        finally:
            mon.Env = old
        self.assertEqual(rc, 0)
        self.assertIn('dry run: 1 closable, 0 sent', buf.getvalue())
        self.assertEqual(self.closing, [])


if __name__ == '__main__':
    unittest.main()
