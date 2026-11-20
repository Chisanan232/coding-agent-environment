#!/usr/bin/env python3
"""Claude sign-in watch (SPE-176, ADR 0020): detection, single-owner alert, recovery, hostile input, bounds, overnight replay, reconcile.

Every fixture is synthetic and lives under a temporary directory; the status function, registry, queue, receipts, clock and the Telegram
transport are injected. Nothing here reads a credential, a real transcript or the owner's notification state."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import ast
import calendar
import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = spec_from_file_location(name.replace('-', '_'), ROOT / 'scripts' / (name + '.py'))
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cw, sn = load('claude-auth-watch'), load('session-notify')
T0 = float(calendar.timegm((2026, 11, 19, 15, 48, 0)))
HOUR = 3600.0
ISOLATION = ('HOME', 'SESSION_NOTIFY_STATE_DIR', 'SESSION_NOTIFY_CONFIG', 'ADMISSION_MONITOR_DIR', 'CLAUDE_AUTH_WATCH_DIR')


def rec_fail(sid, at):
    return {'type': 'assistant', 'isApiErrorMessage': True, 'error': 'authentication_failed', 'timestamp': cw.iso(at), 'sessionId': sid,
            'message': {'content': [{'type': 'text', 'text': 'synthetic'}]}}


def rec_ok(sid, at):
    return {'type': 'assistant', 'timestamp': cw.iso(at), 'sessionId': sid, 'message': {'content': [{'type': 'text', 'text': 'synthetic'}]}}


def rec_rc_oauth(sid, at):
    return {'type': 'system', 'timestamp': cw.iso(at), 'sessionId': sid,
            'content': 'Remote Control disconnected — OAuth token unavailable — run /login to restore Remote Control'}


def rec_rc_other(sid, at):
    return {'type': 'system', 'timestamp': cw.iso(at), 'sessionId': sid,
            'content': 'Remote Control disconnected — Session creation failed — see debug log — run /remote-control to reconnect'}


def rec_rate_limit(sid, at):
    return {'type': 'assistant', 'isApiErrorMessage': True, 'error': 'rate_limit', 'timestamp': cw.iso(at), 'sessionId': sid}


class Clock:
    def __init__(self, now=T0 + 120):
        self.now, self.mono_now = now, 5000.0

    def advance(self, seconds, asleep=False):
        self.now += seconds
        if not asleep:
            self.mono_now += seconds

    def __call__(self):
        return self.now

    def mono(self):
        return self.mono_now


class EngineNotifier:
    """The real session-notify Engine (in-process, state in a temp dir) with a fake Telegram transport."""

    def __init__(self, sdir, clock):
        self.sdir, self.clock = sdir, clock
        self.sent, self.fail, self.backend, self.calls = [], False, True, []

    def cfg(self):
        c = json.loads(json.dumps(sn.DEFAULT_CONFIG))
        c['backends']['telegram'].update(enabled=self.backend, approved_destination='api.telegram.org')
        return c

    def engine(self):
        def telegram(payload, bcfg):
            if self.fail:
                return 'error-simulated'
            self.sent.append(dict(payload))
            return None
        return sn.Engine(self.sdir, self.cfg(), {'telegram': telegram}, clock=self.clock, sleep=lambda s: None)

    def emit(self, cause, recovery, action, signature, detail):
        eng = self.engine()
        result = eng.emit_resource({'cause': cause, 'recovery': recovery, 'owner_action': action, 'signature': signature, 'detail': detail}, strict=True)
        self.calls.append(('emit', cause))
        return {'result': result, 'delivery': eng.last_delivery}

    def clear(self):
        self.calls.append(('clear', None))
        return self.engine().emit_resource({}, clear=True, scope='auth') == 'cleared'


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        saved = {k: os.environ.get(k) for k in ISOLATION}
        os.environ.update(HOME=str(root / 'home'), SESSION_NOTIFY_STATE_DIR=str(root / 'sn'), SESSION_NOTIFY_CONFIG=str(root / 'sn.json'),
                          ADMISSION_MONITOR_DIR=str(root / 'am'), CLAUDE_AUTH_WATCH_DIR=str(root / 'watch'))
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v) for k, v in saved.items()])
        self.root = root
        self.projects, self.sessions = root / 'projects', root / 'sessions'
        (self.projects / 'p1').mkdir(parents=True)
        self.sessions.mkdir()
        self.clock = Clock()
        self.status_value = 'logged-out'
        self.alive = True
        self.registry_override = None
        self.src = cw.Sources(self.projects, self.sessions, status=lambda: self.status(), alive=lambda pid: self.alive, host='testhost')
        self.notifier = EngineNotifier(root / 'sn-state', self.clock)
        self.env = cw.Env(self.src, notifier=self.notifier, clock=self.clock, mono=self.clock.mono, boot=lambda: 111, directory=root / 'watch')
        self.files = {}

    def status(self):
        if isinstance(self.status_value, Exception):
            raise self.status_value
        return self.status_value

    def add(self, sid, name, *records, project='p1', mtime=None):
        path = self.projects / project / (sid + '.jsonl')
        with open(path, 'a') as handle:
            for r in records:
                handle.write(json.dumps(r) + '\n')
        newest = max([cw.parse_ts(r['timestamp']) for r in records] + [0.0])
        if mtime is not False and newest:
            os.utime(path, (newest, newest) if mtime is None else (mtime, mtime))     # file mtime follows the fake clock, never the real one
        if name is not None:
            (self.sessions / (sid + '.json')).write_text(json.dumps({'sessionId': sid, 'name': name, 'pid': 4242, 'status': 'busy', 'cwd': '/x'}))
        return path

    def tick(self, send=True, **kw):
        return cw.tick(self.env, send=send, **kw)

    def state(self):
        return json.loads((self.root / 'watch/state.json').read_text())

    def two_failures(self, at=T0):
        self.add('sid-a', 'alpha', rec_ok('sid-a', at - 60), rec_fail('sid-a', at))
        self.add('sid-b', 'beta', rec_ok('sid-b', at - 60), rec_fail('sid-b', at + 5))


class Detection(Base):
    def test_two_failed_sessions_send_exactly_one_notice_and_repeat_is_deduped(self):
        self.two_failures()
        first = self.tick()
        self.assertEqual((first['verdict'], first['sent']), ('auth-required', True))
        self.assertEqual(len(self.notifier.sent), 1)
        body = self.notifier.sent[0]['body']
        self.assertIn('Claude Code sign-in required', body)
        self.assertIn('names=alpha,beta', body)
        self.assertIn('host=testhost', body)
        self.assertEqual(self.notifier.sent[0]['title'], 'Claude Code: sign-in required')
        for _ in range(3):
            self.clock.advance(60)
            self.assertEqual(self.tick()['notices'], [])
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(self.state()['auth']['alert']['delivered'], True)

    def test_scan_schema_is_closed_and_has_no_ids(self):
        self.two_failures()
        self.status_value = 'logged-out'
        out = cw.scan(self.src, now=self.clock.now)
        self.assertEqual(set(out), {'schema', 'at', 'host', 'verdict', 'status', 'corroboration', 'incident_epoch', 'affected', 'sessions', 'sessions_more',
                                    'files_scanned', 'truncated', 'future_ts_dropped'})
        self.assertEqual(out['verdict'], 'auth-required')
        self.assertEqual(out['incident_epoch'], int(T0))
        for row in out['sessions']:
            self.assertEqual(set(row), {'name', 'class', 'evidence', 'since'})
            self.assertIn(row['class'], cw.CLASSES)
            self.assertIn(row['evidence'], cw.EVIDENCE)
        self.assertNotIn('sid-a', json.dumps(out))

    def test_single_session_needs_logged_out_status_or_corroboration(self):
        self.add('sid-a', 'alpha', rec_fail('sid-a', T0))
        for value in ('unknown', 'logged-in'):
            self.status_value = value
            out = self.tick()
            self.assertEqual((out['verdict'], out['sent']), ('unknown', False), value)
        self.assertEqual(self.notifier.sent, [])
        self.status_value = 'logged-out'
        self.assertEqual(self.tick()['verdict'], 'auth-required')
        self.assertEqual(len(self.notifier.sent), 1)

    def test_rc_oauth_unavailable_corroborates_a_native_failure_when_status_is_unknown(self):
        self.status_value = 'unknown'
        self.add('sid-a', 'alpha', rec_fail('sid-a', T0), rec_rc_oauth('sid-a', T0 + 30))
        self.assertEqual(self.tick()['verdict'], 'auth-required')

    def test_status_failing_timing_out_or_garbled_never_alerts(self):
        self.add('sid-a', 'alpha', rec_fail('sid-a', T0))
        for bad in (RuntimeError('boom'), subprocess.TimeoutExpired('claude', 15), 'banana', None, 5):
            self.status_value = bad
            self.assertEqual(self.tick()['sent'], False)
        self.assertEqual(self.notifier.sent, [])

    def test_real_status_reader_parses_only_the_loggedIn_key(self):
        binary = self.root / 'fake-claude'
        binary.write_text('#!/bin/sh\n')
        binary.chmod(0o755)
        os.environ['CLAUDE_AUTH_WATCH_CLAUDE_BIN'] = str(binary)
        self.addCleanup(os.environ.pop, 'CLAUDE_AUTH_WATCH_CLAUDE_BIN', None)
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            if isinstance(self.reply, Exception):
                raise self.reply
            return subprocess.CompletedProcess(argv, 0, stdout=self.reply, stderr='')
        real = cw.subprocess.run
        cw.subprocess.run = fake_run
        self.addCleanup(setattr, cw.subprocess, 'run', real)
        for reply, expected in (('{"loggedIn": true, "authMethod": "x"}', 'logged-in'), ('{"loggedIn": false}', 'logged-out'), ('nope', 'unknown'),
                                ('{"loggedIn": "no"}', 'unknown'), ('[]', 'unknown'), (subprocess.TimeoutExpired('c', 1), 'unknown'), (OSError('x'), 'unknown')):
            self.reply = reply
            self.assertEqual(cw.claude_status(), expected)
        self.assertTrue(all(c[1:] == ['auth', 'status'] for c in calls))

    def test_stale_failure_older_than_recovery_is_none_even_when_logged_in(self):
        self.add('sid-a', 'alpha', rec_fail('sid-a', T0), rec_ok('sid-a', T0 + 600))
        self.add('sid-b', 'beta', rec_fail('sid-b', T0), rec_ok('sid-b', T0 + 700))
        self.status_value = 'logged-in'
        out = self.tick()
        self.assertEqual((out['verdict'], out['sent']), ('none', False))

    def test_other_api_errors_are_not_auth(self):
        self.add('sid-a', 'alpha', rec_rate_limit('sid-a', T0))
        self.add('sid-b', 'beta', rec_rate_limit('sid-b', T0))
        self.assertEqual(self.tick()['verdict'], 'none')

    def test_prose_is_not_evidence(self):
        text = {'type': 'assistant', 'timestamp': cw.iso(T0), 'sessionId': 'sid-a', 'message': {'content': [{'type': 'text', 'text': 'Login expired Please run /login Not logged in'}]}}
        self.add('sid-a', 'alpha', text, {'type': 'user', 'timestamp': cw.iso(T0 + 1), 'sessionId': 'sid-a', 'message': 'authentication_failed Remote Control disconnected OAuth token unavailable'})
        self.assertEqual(self.tick()['verdict'], 'none')

    def test_observe_only_tick_changes_no_incident_state_and_sends_nothing(self):
        self.two_failures()
        out = self.tick(send=False)
        self.assertEqual(out['notices'][0]['result'], 'would-send')
        self.assertEqual(self.notifier.sent, [])
        self.assertFalse(self.state()['auth']['active'])
        self.assertEqual(self.tick()['sent'], True)    # switching to --send later still alerts


class RemoteControl(Base):
    def test_session_creation_failed_gives_the_rc_notice_only_never_sign_in(self):
        self.add('sid-a', 'alpha', rec_rc_other('sid-a', T0))
        out = self.tick()
        self.assertEqual(out['verdict'], 'remote-control-disconnect')
        self.assertEqual(len(self.notifier.sent), 1)
        body = self.notifier.sent[0]['body'] + self.notifier.sent[0]['title']
        self.assertIn('Remote Control disconnected', body)
        self.assertNotIn('sign-in', body.lower())
        self.assertNotIn('sign in', body.lower())
        self.assertEqual(self.tick()['notices'], [])              # one notice per incident

    def test_rc_incident_cooldown_then_new_incident(self):
        self.add('sid-a', 'alpha', rec_rc_other('sid-a', T0))
        self.tick()
        self.add('sid-a', None, rec_ok('sid-a', T0 + 60))
        self.clock.advance(120)
        self.tick()                                              # recovered: quiet end
        self.assertFalse(self.state()['rc']['active'])
        self.add('sid-a', None, rec_rc_other('sid-a', T0 + 300))
        self.clock.advance(120)
        self.assertEqual(self.tick()['notices'], [])             # inside the cooldown
        self.clock.advance(cw.RC_COOLDOWN_S)
        self.add('sid-a', None, rec_rc_other('sid-a', self.clock.now - 30))
        self.assertEqual(len(self.tick()['notices']), 1)
        self.assertEqual(len(self.notifier.sent), 2)

    def test_oauth_unavailable_alone_is_unknown_and_silent(self):
        self.status_value = 'unknown'
        self.add('sid-a', 'alpha', rec_rc_oauth('sid-a', T0))
        out = self.tick()
        self.assertEqual((out['verdict'], out['sent']), ('unknown', False))
        scan = cw.scan(self.src, now=self.clock.now)
        self.assertEqual([(r['class'], r['evidence']) for r in scan['sessions']], [('unknown', 'rc-oauth-unavailable')])
        self.assertEqual(self.notifier.sent, [])

    def test_oauth_unavailable_with_logged_out_status_counts_as_auth(self):
        self.add('sid-a', 'alpha', rec_rc_oauth('sid-a', T0))
        self.status_value = 'logged-out'
        self.assertEqual(self.tick()['verdict'], 'auth-required')

    def test_rc_notice_suppressed_while_a_sign_in_incident_is_active(self):
        self.two_failures()
        self.tick()
        self.add('sid-c', 'gamma', rec_rc_other('sid-c', T0 + 100))
        self.clock.advance(60)
        self.tick()
        self.assertEqual(len(self.notifier.sent), 1)


class Recovery(Base):
    def test_recovery_sends_one_notice_then_clears_and_a_later_incident_sends_again(self):
        self.two_failures()
        self.tick()
        self.status_value = 'logged-in'
        self.add('sid-a', None, rec_ok('sid-a', T0 + 600))
        self.clock.advance(700)
        self.assertEqual(self.tick()['notices'], [])             # beta failed 13 minutes ago: still fresh, so the incident stays open
        self.clock.advance(1500)
        self.assertEqual(self.tick()['notices'], [])             # clean now, but the recovery is debounced for QUIET_RECOVER_S
        self.clock.advance(cw.QUIET_RECOVER_S)
        out = self.tick()
        self.assertEqual([n['kind'] for n in out['notices']], ['signin-recovered'])     # one resumed session is enough; beta is now idle
        self.assertEqual(len(self.notifier.sent), 2)
        self.assertEqual(self.notifier.sent[1]['title'], 'Claude Code: sign-in recovered')
        self.assertIn('sessions resumed', self.notifier.sent[1]['body'])
        self.assertEqual(self.notifier.calls.count(('clear', None)), 1)
        self.assertFalse(self.state()['auth']['active'])
        self.clock.advance(60)
        self.assertEqual(self.tick()['notices'], [])
        self.assertEqual(sn.load_record(self.notifier.sdir, 'claude-auth-watch')[0]['state'], 'IDLE')
        self.clock.advance(4 * HOUR)                           # a new later incident is a new key and sends again
        later = self.clock.now - 60
        self.add('sid-a', None, rec_fail('sid-a', later))
        self.add('sid-b', None, rec_fail('sid-b', later + 3))
        self.status_value = 'logged-out'
        self.assertEqual(self.tick()['verdict'], 'auth-required')
        self.assertEqual(len(self.notifier.sent), 3)

    def test_quiet_logged_in_close_is_silent_and_claims_no_recovery(self):
        self.two_failures()
        self.tick()
        self.status_value = 'logged-in'
        self.clock.advance(300)
        self.assertEqual(self.tick()['notices'], [])            # failures still fresh and nobody resumed yet
        self.clock.advance(cw.FRESH_S)
        self.assertEqual(self.tick()['notices'], [])             # clean starts here
        self.clock.advance(cw.QUIET_RECOVER_S)
        out = self.tick()                                        # both sessions idle, logged in, quiet: possibly an expired-but-present token
        self.assertEqual((out['notices'], out['auth']), ([], 'closed-without-resume-evidence'))
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(self.notifier.calls.count(('clear', None)), 0)
        self.assertFalse(self.state()['auth']['active'])

    def test_a_resumed_session_does_not_close_the_incident_while_another_failed_a_minute_ago(self):
        self.two_failures()
        self.tick()
        self.status_value = 'logged-in'
        self.add('sid-a', None, rec_ok('sid-a', T0 + 200))
        self.clock.advance(300)
        self.add('sid-b', None, rec_fail('sid-b', self.clock.now - 60))
        self.assertEqual(self.tick()['notices'], [])
        self.assertTrue(self.state()['auth']['active'])
        self.add('sid-a', None, rec_fail('sid-a', self.clock.now - 30))          # a re-failure of the resumed session keeps it open too
        self.assertEqual(self.tick()['notices'], [])
        self.assertTrue(self.state()['auth']['active'])
        self.assertEqual(len(self.notifier.sent), 1)

    def test_an_incident_open_for_48_hours_closes_silently(self):
        self.two_failures()
        self.tick()
        self.clock.advance(cw.WINDOW_S + 600, asleep=True)
        out = self.tick()
        self.assertEqual((out['auth'], out['notices']), ('closed-silently-after-48h', []))
        self.assertEqual(len(self.notifier.sent), 1)             # no recovery notice for a silent close
        self.assertEqual(self.notifier.calls.count(('clear', None)), 0)          # a silent close must not re-arm the same incident

    def test_logged_out_status_blocks_recovery(self):
        self.two_failures()
        self.status_value = 'logged-out'
        self.tick()
        self.add('sid-a', None, rec_ok('sid-a', T0 + 600))
        self.add('sid-b', None, rec_ok('sid-b', T0 + 600))
        self.clock.advance(700)
        self.assertEqual(self.tick()['notices'], [])
        self.assertTrue(self.state()['auth']['active'])

    def test_alert_never_handed_off_means_silent_close_without_a_recovery_notice(self):
        self.notifier.backend = False
        self.two_failures()
        self.status_value = 'logged-out'
        self.assertEqual(self.tick()['notices'][0]['result'], 'no-backend')
        self.status_value = 'logged-in'
        self.add('sid-a', None, rec_ok('sid-a', T0 + 600))
        self.add('sid-b', None, rec_ok('sid-b', T0 + 600))
        self.clock.advance(700)
        self.tick()
        self.notifier.backend = True
        self.clock.advance(1000)
        self.tick()
        self.assertEqual(self.notifier.sent, [])                 # a late alert for an already-recovered incident is never sent
        self.assertFalse(self.state()['auth']['active'])


class Delivery(Base):
    def test_no_backend_is_never_recorded_as_delivered_and_is_retried_when_one_appears(self):
        self.notifier.backend = False
        self.two_failures()
        out = self.tick()
        self.assertEqual((out['notices'][0]['result'], out['notices'][0]['handed_off'], out['notices'][0]['delivered']), ('no-backend', False, False))
        self.assertEqual(self.notifier.sent, [])
        self.clock.advance(60)
        self.assertEqual(self.tick()['notices'], [])             # not due yet: retry spacing
        self.notifier.backend = True
        self.clock.advance(cw.RETRY_S)
        out = self.tick()
        self.assertEqual((out['notices'][0]['handed_off'], out['notices'][0]['delivered']), (True, True))
        self.assertEqual(len(self.notifier.sent), 1)
        alert = self.state()['auth']['alert']
        self.assertEqual((alert['handed_off'], alert['delivered'], alert['pending']), (True, True, False))

    def test_transport_failure_is_pending_not_delivered_then_retried(self):
        self.notifier.fail = True
        self.two_failures()
        out = self.tick()
        self.assertEqual((out['notices'][0]['handed_off'], out['notices'][0]['delivered'], out['notices'][0]['pending']), (True, False, True))
        self.notifier.fail = False
        self.clock.advance(cw.RETRY_S)
        out = self.tick()
        self.assertEqual(out['notices'][0]['delivered'], True)
        self.assertEqual(len(self.notifier.sent), 1)

    def test_failed_notifier_process_is_a_retry_not_a_hand_off(self):
        class Dead:
            def emit(self, *a):
                return {'result': 'failed', 'delivery': None}

            def clear(self):
                return False
        self.env.notifier = Dead()
        self.two_failures()
        out = self.tick()
        self.assertEqual((out['notices'][0]['handed_off'], out['sent']), (False, False))

    def test_subprocess_notifier_builds_closed_argv_and_parses_output(self):
        seen = []

        def runner(argv, **kw):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout='emit-resource: queued delivery=sent\n', stderr='')
        n = cw.Notifier(runner)
        res = n.emit('auth-required', 'no-auto-recovery', 'sign-in-claude', 'ab' * 8, 'host=h names=a')
        self.assertEqual(res, {'result': 'queued', 'delivery': 'sent'})
        argv = seen[0]
        self.assertEqual(argv[2], 'emit-resource')
        self.assertIn('--strict', argv)
        self.assertEqual(argv[argv.index('--detail') + 1], 'host=h names=a')
        self.assertTrue(n.clear())
        self.assertEqual(seen[1][-3:], ['--clear', '--scope', 'auth'])
        bad = cw.Notifier(lambda argv, **kw: subprocess.CompletedProcess(argv, 1, stdout='', stderr=''))
        self.assertEqual(bad.emit('auth-required', 'no-auto-recovery', 'sign-in-claude', 'ab' * 8, None)['result'], 'failed')
        boom = cw.Notifier(lambda argv, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired('x', 1)))
        self.assertEqual(boom.emit('auth-required', 'no-auto-recovery', 'sign-in-claude', 'ab' * 8, None)['result'], 'failed')

    def test_unknown_dedup_outcome_is_never_claimed_delivered_and_never_abandoned(self):
        class Dedupes:
            calls = 0
            clears = 0

            def emit(self, *a):
                Dedupes.calls += 1
                return {'result': 'deduped', 'delivery': 'unknown'}

            def clear(self):
                Dedupes.clears += 1
                return True
        self.env.notifier = Dedupes()
        self.two_failures()
        for _ in range(cw.MAX_SENDS + 4):
            self.tick()
            self.clock.advance(cw.REDRIVE_S + 10)
        alert = self.state()['auth']['alert']
        self.assertEqual((alert['delivered'], alert['unverified'], alert['dropped']), (False, False, False))
        self.assertGreaterEqual(Dedupes.clears, cw.MAX_SENDS)                    # an unknown outcome is re-armed (dedup marker cleared), not trusted


class StateAndTime(Base):
    def test_restart_with_state_reload_does_not_resend(self):
        self.two_failures()
        self.tick()
        self.env = cw.Env(self.src, notifier=self.notifier, clock=self.clock, mono=self.clock.mono, boot=lambda: 111, directory=self.root / 'watch')
        self.clock.advance(60)
        self.assertEqual(self.tick()['notices'], [])
        self.assertEqual(len(self.notifier.sent), 1)

    def test_lost_or_corrupt_state_resends_but_session_notify_dedups(self):
        self.two_failures()
        self.tick()
        (self.root / 'watch/state.json').write_text('{not json')
        self.clock.advance(60)
        out = self.tick()
        self.assertTrue(out['state_reset'])
        self.assertEqual(out['notices'][0]['result'], 'deduped')
        self.assertEqual(len(self.notifier.sent), 1)             # the same incident key: no second message

    def test_oversized_or_symlinked_state_is_not_trusted(self):
        self.two_failures()
        os.makedirs(self.root / 'watch', exist_ok=True)
        (self.root / 'watch/state.json').write_text('{"schema": 1, "pad": "' + 'x' * (cw.STATE_LIMIT + 10) + '"}')
        self.assertTrue(self.tick()['state_reset'])
        target = self.root / 'target.json'
        target.write_text(json.dumps({'schema': 1, 'gaps': 99}))
        (self.root / 'watch/state.json').unlink()
        (self.root / 'watch/state.json').symlink_to(target)
        self.clock.advance(60)
        out = self.tick()
        self.assertTrue(out['state_reset'])

    def test_concurrent_ticks_send_once(self):
        self.two_failures()
        results = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            results.append(cw.tick(self.env, send=True))
        threads = [threading.Thread(target=worker) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertGreaterEqual(sum(1 for r in results if r['ran']), 1)

    def test_lock_held_elsewhere_means_no_run(self):
        import fcntl
        os.makedirs(self.root / 'watch', mode=0o700, exist_ok=True)
        fd = os.open(str(self.root / 'watch/watch.lock'), os.O_WRONLY | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.assertEqual(self.tick()['why'], 'another tick holds the lock')

    def test_kill_switch_file_and_env(self):
        self.two_failures()
        os.makedirs(self.root / 'watch', mode=0o700, exist_ok=True)
        (self.root / 'watch/DISABLED').write_text('')
        self.assertEqual(self.tick()['why'], 'kill switch')
        (self.root / 'watch/DISABLED').unlink()
        os.environ['CLAUDE_AUTH_WATCH_DISABLED'] = '1'
        self.addCleanup(os.environ.pop, 'CLAUDE_AUTH_WATCH_DISABLED', None)
        self.assertEqual(self.tick()['why'], 'kill switch')
        self.assertEqual(self.notifier.sent, [])

    def test_if_due_skips_within_the_interval(self):
        self.tick()
        self.clock.advance(10)
        self.assertEqual(self.tick(if_due=True)['why'], 'not due')
        self.clock.advance(60)
        self.assertTrue(self.tick(if_due=True)['ran'])

    def test_state_dir_is_private_and_a_symlinked_dir_is_refused(self):
        self.tick()
        self.assertEqual(os.stat(self.root / 'watch').st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self.root / 'watch/state.json').st_mode & 0o777, 0o600)
        link = self.root / 'linked'
        link.symlink_to(self.root / 'watch')
        env = cw.Env(self.src, notifier=self.notifier, clock=self.clock, mono=self.clock.mono, boot=lambda: 111, directory=link)
        self.assertEqual(cw.tick(env, send=True)['why'], 'watch directory is a symlink')

    def test_sleep_gap_keeps_the_incident_and_sends_nothing_new(self):
        self.two_failures()
        self.status_value = 'logged-out'
        self.tick()
        self.clock.advance(6 * HOUR, asleep=True)
        out = self.tick()
        self.assertTrue(out['gap'])
        self.assertEqual(out['notices'], [])
        self.assertEqual(out['auth'], 'active')
        self.assertEqual(len(self.notifier.sent), 1)
        self.clock.advance(-3 * HOUR)                            # clock stepped back
        out = self.tick()
        self.assertTrue(out['gap'])
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertGreaterEqual(self.state()['gaps'], 2)

    def test_reboot_is_a_gap(self):
        self.tick()
        self.env.boot = lambda: 222
        self.clock.advance(60)
        self.assertTrue(self.tick()['gap'])


class Hostile(Base):
    def test_huge_lines_invalid_json_and_junk_are_survived(self):
        path = self.projects / 'p1/junk.jsonl'
        with open(path, 'w') as h:
            h.write('x' * 2_000_000 + '\n')
            h.write('{"type": "assistant", "isApiErrorMessage": tru\n\x00\x01\x02\n[1,2,3]\n"just a string"\n')
            h.write(json.dumps({'type': 'assistant', 'error': 'authentication_failed', 'timestamp': 'not-a-time', 'sessionId': 'sid-a'}) + '\n')
            h.write(json.dumps(rec_fail('sid-a', T0)) + '\n' + json.dumps(rec_fail('sid-b', T0 + 1)) + '\n')
            h.write(json.dumps({'pad': 'y' * 100_000, 'type': 'assistant', 'timestamp': cw.iso(T0 + 50), 'sessionId': 'sid-a'}) + '\n')
        for sid in ('sid-a', 'sid-b'):
            (self.sessions / (sid + '.json')).write_text(json.dumps({'sessionId': sid, 'name': sid, 'pid': 1}))
        os.utime(path, (T0 + 100, T0 + 100))
        out = self.tick()
        self.assertEqual(out['verdict'], 'auth-required')        # the huge line and the over-long success record are skipped, never parsed

    def test_symlinked_transcript_dir_and_file_are_not_followed(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'evil.jsonl').write_text(json.dumps(rec_fail('sid-x', T0)) + '\n' + json.dumps(rec_fail('sid-y', T0)) + '\n')
        (self.projects / 'linkdir').symlink_to(outside)
        (self.projects / 'p1' / 'linkfile.jsonl').symlink_to(outside / 'evil.jsonl')
        out = self.tick()
        self.assertEqual((out['verdict'], out['sent']), ('none', False))

    def test_names_with_control_characters_urls_and_markdown_are_replaced(self):
        hostile = ['evil\nname', 'http://evil.example/x', 'see www.evil.com now', 'x' * 80, '[click](http://a)', '*bold*', 'ok name', 'name\x1b[31m', '']
        for i, name in enumerate(hostile):
            sid = 'sid-%d' % i
            self.add(sid, name, rec_fail(sid, T0 + i))
        self.status_value = 'logged-out'
        out = cw.scan(self.src, now=self.clock.now)
        shown = [r['name'] for r in out['sessions']]
        self.assertEqual(out['affected'], len(hostile))
        self.assertEqual(len(shown), cw.NAMES_LISTED)
        for name in shown:
            self.assertRegex(name, r'^[A-Za-z0-9 ._()-]{1,48}$')
            self.assertNotIn('http', name)
            self.assertNotIn('www', name)
        self.tick()
        body = self.notifier.sent[0]['body']
        self.assertNotIn('\n', body.split('|')[-1])
        self.assertNotIn('http', body)
        self.assertRegex(body.split(' | ')[-1], sn.AUTH_DETAIL.pattern.replace('(?=.*[A-Za-z0-9])', ''))
        self.assertIn('+3 more', body)

    def test_registry_with_garbage_and_traps(self):
        (self.sessions / 'a.json').write_text('{bad')
        (self.sessions / 'b.json').write_text('[]')
        (self.sessions / 'c.json').write_text(json.dumps({'sessionId': 5}))
        (self.sessions / 'd.json').symlink_to(self.root / 'nowhere')
        (self.sessions / 'e.json').write_text(json.dumps({'sessionId': 'sid-e', 'name': 'fine', 'pid': 'x'}))
        rows = cw.read_registry(self.sessions)
        self.assertEqual(rows, [{'sid': 'sid-e', 'name': 'fine', 'pid': None}])
        self.assertIsNone(cw.read_registry(self.root / 'absent'))

    def test_unreadable_registry_never_marks_sessions_gone(self):
        self.two_failures()
        for p in self.sessions.glob('*.json'):
            p.unlink()
        self.sessions.rmdir()
        out = self.tick()
        self.assertEqual(out['verdict'], 'auth-required')

    def test_notice_detail_is_charset_closed(self):
        for good in ('host=h names=a,b +2 more', 'a (b).c_d-e'):
            self.assertTrue(sn.AUTH_DETAIL.fullmatch(good), good)
        for bad in ('line\nbreak', 'http://x', 'a/b', '[x](y)', '*x*', 'a:b', '`x`', '<b>', 'x' * 161, '', '   ', 'a;b', "a'b", 'a|b', 'a\x00b'):
            self.assertFalse(sn.AUTH_DETAIL.fullmatch(bad), repr(bad))


class Bounds(Base):
    def test_thousands_of_transcripts_scan_only_the_newest_within_bounds(self):
        for i in range(3000):
            p = self.projects / ('p%d' % (i % 30))
            p.mkdir(exist_ok=True)
            f = p / ('s%d.jsonl' % i)
            f.write_text(json.dumps(rec_ok('s%d' % i, T0)) + '\n')
            os.utime(f, (T0 - i, T0 - i))
        started = time.monotonic()
        analysis = cw.analyze(self.src, self.clock.now)
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(analysis['scanned'], cw.MAX_FILES)

    def test_files_outside_the_window_are_not_read(self):
        path = self.add('sid-a', 'alpha', rec_fail('sid-a', T0))
        self.add('sid-b', 'beta', rec_fail('sid-b', T0))
        old = time.time() - 10 * 24 * 3600
        os.utime(path, (old, old))
        out = cw.scan(self.src, now=time.time())
        self.assertEqual(out['affected'], 1)

    def test_only_the_tail_window_of_a_big_file_is_read(self):
        head = self.projects / 'p1/big-head.jsonl'
        with open(head, 'w') as h:
            h.write(json.dumps(rec_fail('sid-h', T0)) + '\n')
            h.write((json.dumps({'type': 'user', 'pad': 'z' * 900}) + '\n') * 6000)      # ~5.4 MB after the failure
        tail = self.projects / 'p1/big-tail.jsonl'
        with open(tail, 'w') as h:
            h.write((json.dumps({'type': 'user', 'pad': 'z' * 900}) + '\n') * 6000)
            h.write(json.dumps(rec_fail('sid-t', T0)) + '\n')
        for f in (head, tail):
            os.utime(f, (T0, T0))
        events, scanned, _, _, _ = cw.scan_transcripts(self.projects, self.clock.now, self.clock.mono)
        self.assertEqual(scanned, 2)
        self.assertNotIn('sid-h', events)
        self.assertIn('sid-t', events)

    def test_time_budget_truncates_the_scan(self):
        for i in range(5):
            self.add('s%d' % i, None, rec_ok('s%d' % i, T0))
        ticks = iter(range(0, 10 ** 6, 30))
        events, scanned, truncated, _, _ = cw.scan_transcripts(self.projects, self.clock.now, lambda: next(ticks))
        self.assertTrue(truncated)
        self.assertLess(scanned, 5)

    def test_notice_state_stays_bounded(self):
        for i in range(120):
            self.add('sid-%03d' % i, 'name%d' % i, rec_fail('sid-%03d' % i, T0 + i))
        self.tick()
        state = self.state()
        self.assertLessEqual(len(state['auth']['sids']), cw.SIDS_MAX)
        self.assertLessEqual(len(state['auth']['names']), cw.NAMES_LISTED)
        self.assertLess((self.root / 'watch/state.json').stat().st_size, cw.STATE_LIMIT)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertLessEqual(len(self.notifier.sent[0]['body']), 300)


class OvernightReplay(Base):
    def test_four_sessions_fail_rc_noise_then_staggered_recovery_gives_exactly_two_notices(self):
        sids = {'sid-1': 'alpha', 'sid-2': 'beta', 'sid-3': 'gamma', 'sid-4': 'delta (rc)'}
        fail_at = float(calendar.timegm((2026, 11, 19, 15, 48, 0)))
        for i, (sid, name) in enumerate(sids.items()):
            self.add(sid, name, rec_ok(sid, fail_at - 600), rec_fail(sid, fail_at + i * 7))
        self.status_value = 'logged-out'

        def when(h, m, day=19):
            return float(calendar.timegm((2026, 11, day, h, m, 0)))

        def at(h, m, day=19):
            self.clock.now = when(h, m, day)
            self.clock.mono_now += 30 * 60
        at(15, 50)
        self.assertEqual(self.tick()['verdict'], 'auth-required')
        at(16, 30)
        self.tick()
        for sid, (h, m) in zip(sids, ((19, 54), (20, 40), (21, 10), (21, 28))):
            self.add(sid, None, rec_rc_oauth(sid, when(h, m)))
            at(h, m + 1)
            self.tick()
        self.add('sid-3', None, rec_rc_other('sid-3', when(21, 29)))     # an RC-only disconnect while the sign-in incident is open
        at(21, 30)
        self.assertEqual(self.tick()['notices'], [])                     # suppressed: no RC notice during the incident
        self.assertEqual(len(self.notifier.sent), 1)
        self.status_value = 'logged-in'
        for (sid, (h, m)) in zip(sids, ((0, 8), (0, 30), (0, 50), (0, 55))):
            self.add(sid, None, rec_ok(sid, when(h, m, 20)))
            at(h, m + 1, 20)
            self.tick()
        self.assertEqual(len(self.notifier.sent), 2)
        self.assertEqual(self.notifier.sent[0]['title'], 'Claude Code: sign-in required')
        self.assertEqual(self.notifier.sent[1]['title'], 'Claude Code: sign-in recovered')
        self.assertEqual(self.notifier.calls.count(('clear', None)), 1)
        self.assertFalse(self.state()['rc']['active'])
        at(1, 30, 20)
        self.assertEqual(self.tick()['notices'], [])
        self.assertEqual(len(self.notifier.sent), 2)


class Freshness(Base):
    """Recency rules: only fresh failures open an incident; status decides how much corroboration they need."""

    def stale(self, hours=40):
        at = self.clock.now - hours * HOUR
        self.add('sid-a', 'alpha', rec_fail('sid-a', at))
        self.add('sid-b', 'beta', rec_fail('sid-b', at + 5), rec_rc_oauth('sid-b', at + 9))

    def fresh(self, minutes_ago, sids=('sid-a', 'sid-b')):
        for i, sid in enumerate(sids):
            self.add(sid, 'n' + sid[-1], rec_fail(sid, self.clock.now - minutes_ago * 60 + i))

    def test_forty_hour_old_failures_never_alert_under_any_status(self):
        self.stale()
        for status in ('logged-in', 'unknown', 'logged-out'):
            self.status_value = status
            out = self.tick()
            self.assertEqual((out['verdict'], out['sent']), ('none', False), status)
        self.assertEqual(self.notifier.sent, [])
        rows = cw.scan(self.src, now=self.clock.now)['sessions']
        self.assertEqual({(r['class'], r['evidence']) for r in rows}, {('none', 'idle')})       # listed as idle, never an incident

    def test_fresh_record_in_an_idle_transcript_file_is_not_fresh(self):
        self.add('sid-a', 'alpha', rec_fail('sid-a', self.clock.now - 60), mtime=self.clock.now - 2 * HOUR)
        self.assertEqual(self.tick()['verdict'], 'none')

    def test_registry_pid_none_or_unreadable_never_makes_a_stale_transcript_live(self):
        self.stale()
        for p in self.sessions.glob('*.json'):
            p.write_text(json.dumps({'sessionId': p.stem, 'name': 'x'}))              # pid None
        self.assertEqual(self.tick()['verdict'], 'none')
        for p in self.sessions.glob('*.json'):
            p.unlink()
        self.sessions.rmdir()                                                          # unreadable registry
        self.assertEqual(self.tick()['verdict'], 'none')
        self.assertEqual(self.notifier.sent, [])

    def test_unreadable_registry_still_alerts_on_fresh_failures(self):
        self.fresh(5)
        for p in self.sessions.glob('*.json'):
            p.unlink()
        self.sessions.rmdir()
        self.assertEqual(self.tick()['verdict'], 'auth-required')
        self.assertRegex(self.notifier.sent[0]['body'], r'names=session-[0-9a-f]{4},session-[0-9a-f]{4}')

    def test_logged_in_vetoes_unless_a_strong_failure_and_corroboration_exist(self):
        self.status_value = 'logged-in'
        self.fresh(20)                                           # two fresh but older than 10 minutes: vetoed
        self.assertEqual((self.tick()['verdict'], self.notifier.sent), ('unknown', []))
        self.clock.advance(0)
        self.add('sid-c', 'gamma', rec_fail('sid-c', self.clock.now - 60))   # one strong failure alone: still not enough
        self.assertEqual(self.tick()['verdict'], 'auth-required')             # strong + three sessions in one cluster: override
        self.assertEqual(len(self.notifier.sent), 1)

    def test_logged_in_single_strong_failure_without_corroboration_is_vetoed(self):
        self.status_value = 'logged-in'
        self.add('sid-a', 'alpha', rec_fail('sid-a', self.clock.now - 60))
        self.assertEqual((self.tick()['verdict'], self.notifier.sent), ('unknown', []))
        self.add('sid-a', None, rec_rc_oauth('sid-a', self.clock.now - 30))
        self.assertEqual(self.tick()['verdict'], 'auth-required')                 # strong + RC corroboration

    def test_unknown_status_needs_native_plus_rc_corroboration_not_several_sessions(self):
        self.status_value = 'unknown'
        self.fresh(5)
        self.assertEqual((self.tick()['verdict'], self.notifier.sent), ('unknown', []))
        self.add('sid-b', None, rec_rc_oauth('sid-b', self.clock.now - 30))
        out = self.tick()
        self.assertEqual(out['verdict'], 'auth-required')
        scan = cw.scan(self.src, now=self.clock.now)
        self.assertEqual(scan['corroboration'], ['multiple-sessions', 'rc-oauth-unavailable'])

    def test_logged_out_alerts_on_one_fresh_failure_or_a_fresh_rc_oauth(self):
        self.fresh(25, sids=('sid-a',))
        self.assertEqual(self.tick()['verdict'], 'auth-required')
        self.assertEqual(cw.scan(self.src, now=self.clock.now)['corroboration'], ['status-logged-out'])

    def test_failures_beyond_the_cluster_window_do_not_count_as_a_cluster(self):
        analysis = {'sessions': {}, 'scanned': 0, 'truncated': False}
        now = self.clock.now
        for sid, age in (('a', 1900), ('b', 100)):
            analysis['sessions'][sid] = {'hash': sid, 'name': sid, 'last_ok': None, 'native': now - age, 'native_last': now - age, 'oauth': None, 'oauth_last': None,
                                         'fresh_first': {'fail': now - age, 'rc_oauth': None, 'rc_other': None}, 'other': None, 'other_last': None, 'mtime': now - age, 'gone': False, 'events': []}
        verdict, _, corr, _ = cw.decide(analysis, 'logged-in', now)
        self.assertEqual((verdict, corr), ('unknown', []))
        analysis['sessions']['a']['native_last'] = now - 500
        analysis['sessions']['a']['mtime'] = now - 500
        verdict, _, corr, _ = cw.decide(analysis, 'logged-in', now)
        self.assertEqual((verdict, corr), ('auth-required', ['multiple-sessions']))

    def test_idle_registered_sessions_are_listed_not_alerted(self):
        for i in range(3):
            self.add('sid-%d' % i, 'idle%d' % i, rec_fail('sid-%d' % i, self.clock.now - 2 * HOUR))
        out = self.tick()
        self.assertEqual((out['verdict'], out['sent']), ('none', False))
        self.assertEqual(cw.scan(self.src, now=self.clock.now)['affected'], 3)

    def test_remote_control_disconnect_has_the_same_recency_rule(self):
        self.add('sid-a', 'alpha', rec_rc_other('sid-a', self.clock.now - 40 * HOUR))
        self.assertEqual((self.tick()['verdict'], self.notifier.sent), ('none', []))
        self.add('sid-a', None, rec_rc_other('sid-a', self.clock.now - 60))
        self.assertEqual(self.tick()['verdict'], 'remote-control-disconnect')
        self.assertEqual(len(self.notifier.sent), 1)


class LongIncidents(Base):
    """A still-failing incident stays one incident; a closed one never reopens; only a new fresh failure opens a new one."""

    def test_five_days_of_continuous_failure_over_3000_ticks_alert_exactly_once_with_no_flapping(self):
        step = 144.0
        for i in range(3000):
            self.clock.advance(step)
            at = self.clock.now - 30
            for sid, name in (('sid-a', 'alpha'), ('sid-b', 'beta')):
                self.add(sid, name, rec_fail(sid, at))
            out = self.tick()
            self.assertFalse(out['gap'])
            if i == 0:
                self.assertEqual(out['verdict'], 'auth-required')
        self.assertGreater(self.clock.now - T0, 4.9 * 24 * HOUR)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual([c[0] for c in self.notifier.calls], ['emit'])            # never emit/clear/emit/clear
        auth = self.state()['auth']
        self.assertTrue(auth['active'])
        self.assertLess(auth['epoch'], T0 + 1000)                                  # the epoch never moved
        self.assertEqual(self.state()['closed'], [])

    def test_silent_close_then_a_new_fresh_failure_opens_one_new_incident(self):
        self.two_failures()
        self.tick()
        first_epoch = self.state()['auth']['epoch']
        self.status_value = 'logged-in'
        self.clock.advance(cw.FRESH_S + 10)
        self.tick()
        self.clock.advance(cw.QUIET_RECOVER_S + 10)
        self.assertEqual(self.tick()['auth'], 'closed-without-resume-evidence')
        self.assertEqual(self.state()['closed'], [first_epoch])
        self.status_value = 'logged-out'
        self.clock.advance(cw.REFOLD_S + 60)                                      # beyond the fold window: a failure now is a NEW incident
        for _ in range(3):
            self.clock.advance(60)
            self.assertEqual(self.tick()['notices'], [])                           # the old failures are stale: nothing reopens
        self.add('sid-a', None, rec_fail('sid-a', self.clock.now - 60))
        self.add('sid-b', None, rec_fail('sid-b', self.clock.now - 55))
        self.assertEqual(self.tick()['verdict'], 'auth-required')
        self.assertEqual(len(self.notifier.sent), 2)
        self.assertNotEqual(self.state()['auth']['epoch'], first_epoch)
        for _ in range(3):
            self.clock.advance(60)
            self.add('sid-a', None, rec_fail('sid-a', self.clock.now - 30))
            self.assertEqual(self.tick()['notices'], [])
        self.assertEqual(len(self.notifier.sent), 2)
        self.assertEqual(self.notifier.calls.count(('clear', None)), 0)

    def test_a_closed_epoch_never_reopens(self):
        self.two_failures()
        self.tick()
        epoch = self.state()['auth']['epoch']
        state = self.state()
        state['auth'] = cw.empty_state()['auth']
        state['closed'] = [epoch]
        (self.root / 'watch/state.json').write_text(json.dumps(state))
        self.clock.advance(60)
        self.assertEqual(self.tick()['notices'], [])
        self.assertFalse(self.state()['auth']['active'])
        self.assertEqual(len(self.notifier.sent), 1)

    def test_new_epoch_is_the_first_fresh_failure_not_an_old_one_from_a_continuous_run(self):
        for k in range(10):
            self.add('sid-a', 'alpha', rec_fail('sid-a', self.clock.now - 3 * HOUR + k * 60))
        self.add('sid-a', None, rec_fail('sid-a', self.clock.now - 600))
        self.add('sid-b', 'beta', rec_fail('sid-b', self.clock.now - 500))
        out = cw.scan(self.src, now=self.clock.now)
        self.assertEqual(out['incident_epoch'], int(self.clock.now - 600))


class FutureDated(Base):
    def test_future_failure_with_a_real_recent_success_does_not_alert(self):
        self.add('sid-a', 'alpha', rec_ok('sid-a', self.clock.now - 60), rec_fail('sid-a', self.clock.now + HOUR), mtime=self.clock.now - 30)
        self.add('sid-b', 'beta', rec_ok('sid-b', self.clock.now - 60), rec_fail('sid-b', self.clock.now + 2 * HOUR), mtime=self.clock.now - 30)
        out = self.tick()
        self.assertEqual((out['verdict'], out['sent']), ('none', False))
        self.assertEqual(cw.scan(self.src, now=self.clock.now)['future_ts_dropped'], 2)

    def test_a_future_dated_file_is_dropped_and_counted(self):
        self.add('sid-a', 'alpha', rec_fail('sid-a', self.clock.now - 10), mtime=self.clock.now + HOUR)
        out = cw.scan(self.src, now=self.clock.now)
        self.assertEqual((out['verdict'], out['future_ts_dropped'], out['files_scanned']), ('none', 1, 0))

    def test_skew_within_five_minutes_is_tolerated(self):
        self.add('sid-a', 'alpha', rec_fail('sid-a', self.clock.now + 200), mtime=self.clock.now + 200)
        self.assertEqual(cw.scan(self.src, now=self.clock.now)['future_ts_dropped'], 0)

    def test_a_future_record_never_sorts_after_a_real_later_success(self):
        self.add('sid-a', 'alpha', rec_fail('sid-a', self.clock.now - 600), rec_fail('sid-a', self.clock.now + 5 * HOUR), rec_ok('sid-a', self.clock.now - 60))
        self.add('sid-b', 'beta', rec_fail('sid-b', self.clock.now - 600), rec_ok('sid-b', self.clock.now - 60))
        self.assertEqual(self.tick()['verdict'], 'none')


class Flapping(Base):
    """A fail/ok flap folds into one incident; a global rolling cap bounds every notice; a dropped alert is re-driven a bounded number of times."""

    def preload(self, **fields):
        os.makedirs(self.root / 'watch', mode=0o700, exist_ok=True)
        state = cw.empty_state()
        state.update(fields)
        (self.root / 'watch/state.json').write_text(json.dumps(state))

    def test_twenty_three_minute_fail_ok_cycles_produce_at_most_three_notices_and_a_later_outage_alerts_again(self):
        for i in range(20):
            self.status_value = 'logged-out'
            self.add('sid-a', 'alpha', rec_fail('sid-a', self.clock.now - 5))
            self.add('sid-b', 'beta', rec_fail('sid-b', self.clock.now - 4))
            self.tick()
            self.clock.advance(90)
            self.status_value = 'logged-in'
            self.add('sid-a', None, rec_ok('sid-a', self.clock.now - 5))
            self.add('sid-b', None, rec_ok('sid-b', self.clock.now - 4))
            self.tick()
            self.clock.advance(90)
        self.assertLessEqual(len(self.notifier.sent), 3)
        self.assertEqual(len(self.notifier.sent), 1)               # the flap never stays clean long enough to announce a recovery
        for _ in range(8):                                         # the flap stops: 30 min until the failures are stale, then 15 clean minutes
            self.clock.advance(600)
            self.tick()
        self.assertEqual([m['title'] for m in self.notifier.sent], ['Claude Code: sign-in required', 'Claude Code: sign-in recovered'])
        self.clock.advance(8 * HOUR)
        self.status_value = 'logged-out'
        self.add('sid-a', None, rec_fail('sid-a', self.clock.now - 60))
        self.add('sid-b', None, rec_fail('sid-b', self.clock.now - 50))
        self.assertEqual(self.tick()['verdict'], 'auth-required')
        self.assertEqual([m['title'] for m in self.notifier.sent][-1], 'Claude Code: sign-in required')
        self.assertEqual(len(self.notifier.sent), 3)

    def test_a_refailure_within_30_minutes_of_a_recovery_is_folded_into_the_incident(self):
        self.two_failures()
        self.tick()
        self.status_value = 'logged-in'
        self.add('sid-a', None, rec_ok('sid-a', self.clock.now + 100))
        self.add('sid-b', None, rec_ok('sid-b', self.clock.now + 100))
        for _ in range(12):
            self.clock.advance(600)
            self.tick()
            if len(self.notifier.sent) == 2:
                break
        self.assertEqual(len(self.notifier.sent), 2)
        self.clock.advance(300)
        self.status_value = 'logged-out'
        self.add('sid-a', None, rec_fail('sid-a', self.clock.now - 20))
        out = self.tick()
        self.assertEqual((out['auth'], out['notices']), ('recurred', []))
        self.assertEqual((self.state()['auth']['recurred'], self.state()['auth']['active']), (1, True))
        self.assertEqual(len(self.notifier.sent), 2)

    def test_recoveries_and_remote_control_never_suppress_a_real_alert(self):
        now = self.clock.now
        self.preload(sent_log=[[now - 100, 'signin-recovered'], [now - 90, 'signin-recovered'], [now - 80, 'remote-control-disconnect']])
        self.two_failures()
        self.assertEqual(self.tick()['notices'][0]['result'], 'queued')
        self.assertEqual(len(self.notifier.sent), 1)

    def test_an_alert_inside_the_spacing_is_deferred_then_sent_as_soon_as_it_allows(self):
        now = self.clock.now
        self.preload(sent_log=[[now - 600, 'auth-required']])
        self.two_failures(at=now - 30)
        out = self.tick()
        self.assertEqual((out['notices'][0]['result'], self.notifier.sent), ('deferred', []))
        for i in range(10):
            self.clock.advance(120)
            self.add('sid-a', None, rec_fail('sid-a', self.clock.now - 5))
            self.tick()
            if i < 8:
                self.assertEqual(self.notifier.sent, [])             # nothing before 30 minutes after the previous alert
        self.assertEqual(len(self.notifier.sent), 1)                 # sent at the first tick the spacing allowed

    def test_an_alert_deferred_past_the_end_of_its_incident_is_recorded_as_unannounced_and_folded_into_the_next(self):
        now = self.clock.now
        self.preload(sent_log=[[now - 300, 'auth-required']])
        self.two_failures(at=now - 30)
        self.assertEqual(self.tick()['notices'][0]['result'], 'deferred')
        self.status_value = 'logged-in'
        self.add('sid-a', None, rec_ok('sid-a', self.clock.now + 60))
        self.add('sid-b', None, rec_ok('sid-b', self.clock.now + 60))
        for _ in range(12):
            self.clock.advance(120)
            out = self.tick()
        self.assertEqual((out['auth'], out['outage_unannounced'], self.notifier.sent), ('inactive', 1, []))
        self.assertEqual(json.loads(subprocess.run([sys.executable, str(ROOT / 'scripts/claude-auth-watch.py'), 'status'], capture_output=True, text=True,
                                                   env=dict(os.environ)).stdout)['outage_unannounced'], 1)
        self.clock.advance(cw.REFOLD_S + 3600)
        self.status_value = 'logged-out'
        self.add('sid-a', None, rec_fail('sid-a', self.clock.now - 20))
        self.add('sid-b', None, rec_fail('sid-b', self.clock.now - 15))
        self.assertEqual(self.tick()['notices'][0]['result'], 'queued')
        self.assertIn('missed=1', self.notifier.sent[0]['body'])
        self.assertEqual(self.state()['missed'], 0)

    def test_a_real_outage_after_a_flapping_burst_is_alerted_within_the_spacing_not_the_window(self):
        now = self.clock.now
        self.preload(sent_log=[[now - 3600, 'auth-required'], [now - 3000, 'signin-recovered'], [now - 2400, 'signin-recovered'], [now - 2000, 'signin-recovered']])
        self.two_failures(at=now - 30)
        self.assertEqual(self.tick()['notices'][0]['result'], 'queued')           # nothing capped it: the last alert was 60 minutes ago
        self.assertEqual(len(self.notifier.sent), 1)

    def _outage_cycles(self, count, fail_min, ok_min):
        titles = []
        for _ in range(count):
            self.status_value = 'logged-out'
            for _ in range(fail_min // 2):
                self.clock.advance(120)
                for sid, name in (('sid-a', 'alpha'), ('sid-b', 'beta')):
                    self.add(sid, name, rec_fail(sid, self.clock.now - 5))
                self.tick()
            self.status_value = 'logged-in'
            for sid in ('sid-a', 'sid-b'):
                self.add(sid, None, rec_ok(sid, self.clock.now - 2))
            for _ in range(ok_min // 2):
                self.clock.advance(120)
                self.tick()
        return [m['title'] for m in self.notifier.sent]

    def test_twenty_outages_of_40_minutes_with_60_minutes_ok_between_are_all_alerted(self):
        titles = self._outage_cycles(20, 40, 60)
        self.assertEqual(titles.count('Claude Code: sign-in required'), 20)
        self.assertEqual([t for i, t in enumerate(titles) if t == titles[i - 1] == 'Claude Code: sign-in recovered'], [])      # recoveries never duplicated

    def test_forty_minute_failures_and_forty_minute_ok_periods_give_an_alert_each_and_no_duplicate_recoveries(self):
        titles = self._outage_cycles(20, 40, 40)
        self.assertEqual(titles.count('Claude Code: sign-in required'), 20)
        self.assertLessEqual(titles.count('Claude Code: sign-in recovered'), 20)
        self.assertEqual([t for i, t in enumerate(titles) if i and t == titles[i - 1] == 'Claude Code: sign-in recovered'], [])
        self.assertGreater(self.state()['rate']['suppressed'], 0)                                                          # recoveries over the cap were counted

    def run_ticks(self, minutes, step=120):
        for _ in range(int(minutes * 60 // step)):
            self.clock.advance(step)
            self.tick()

    def test_a_transport_down_for_400_minutes_then_healed_delivers_exactly_one_alert_at_a_low_rate(self):
        self.notifier.fail = True
        self.two_failures()
        self.tick()
        self.run_ticks(400)
        emits = [c for c in self.notifier.calls if c[0] == 'emit']
        self.assertLessEqual(len(emits), 12)                            # 7 fast hand-offs, then hourly: no storm
        self.assertGreater(len(emits), 7)                               # ...and never abandoned
        self.notifier.fail = False
        self.run_ticks(70)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertTrue(self.state()['auth']['alert']['delivered'])

    def test_session_notify_sweep_dropping_the_entry_between_ticks_is_re_driven(self):
        self.notifier.fail = True
        self.two_failures()
        self.tick()
        for _ in range(8):
            self.clock.advance(1000)
            self.notifier.engine().sweep(self.clock.now)
        self.notifier.fail = False
        self.run_ticks(150)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertTrue(self.state()['auth']['alert']['delivered'])

    def test_a_failure_that_lasts_past_ten_minutes_after_a_delivered_recovery_is_a_new_incident(self):
        self.two_failures()
        self.tick()
        self.status_value = 'logged-in'
        self.add('sid-a', None, rec_ok('sid-a', self.clock.now + 100))
        self.add('sid-b', None, rec_ok('sid-b', self.clock.now + 100))
        for _ in range(12):
            self.clock.advance(600)
            self.tick()
            if len(self.notifier.sent) == 2:
                break
        self.clock.advance(1800)                                    # beyond the alert spacing
        self.status_value = 'logged-out'
        self.add('sid-a', None, rec_fail('sid-a', self.clock.now - 20))
        self.assertEqual(self.tick()['auth'], 'recurred')
        for _ in range(7):
            self.clock.advance(120)
            self.add('sid-a', None, rec_fail('sid-a', self.clock.now - 5))
            out = self.tick()
        self.assertEqual([m['title'] for m in self.notifier.sent], ['Claude Code: sign-in required', 'Claude Code: sign-in recovered', 'Claude Code: sign-in required'])

    def test_recovery_and_rc_notices_are_dropped_before_alerts_when_the_cap_is_hit(self):
        now = self.clock.now
        self.preload(sent_log=[[now - 300, 'signin-recovered'], [now - 200, 'signin-recovered'], [now - 100, 'signin-recovered']])
        self.add('sid-a', 'alpha', rec_rc_other('sid-a', now - 30))
        out = self.tick()
        self.assertEqual((out['notices'][0]['result'], self.notifier.sent), ('rate-limited', []))
        self.assertTrue(self.state()['rc']['notice']['dropped'])
        self.assertEqual(self.state()['rate']['suppressed'], 1)

    def test_a_transport_down_for_30_minutes_then_healed_delivers_exactly_one_notice(self):
        self.notifier.fail = True
        self.two_failures()
        self.assertEqual(self.tick()['notices'][0]['pending'], True)
        self.clock.advance(cw.REDRIVE_S + 10)
        self.notifier.fail = False
        out = self.tick()
        self.assertEqual(out['notices'][0]['delivered'], True)
        self.assertEqual(len(self.notifier.sent), 1)
        for _ in range(3):
            self.clock.advance(cw.REDRIVE_S + 10)
            self.assertEqual(self.tick()['notices'], [])
        self.assertEqual(len(self.notifier.sent), 1)

    def test_a_dropped_alert_is_re_driven_and_delivered_when_the_transport_heals(self):
        self.notifier.fail = True
        self.two_failures()
        for _ in range(7):
            self.tick()
            if self.state()['auth']['alert']['needs_clear']:
                break                                                                # the transport dropped the alert: it is armed for a re-drive
            self.clock.advance(cw.REDRIVE_S + 10)
        self.assertTrue(self.state()['auth']['alert']['needs_clear'])
        self.notifier.fail = False
        self.clock.advance(cw.REDRIVE_S + 10)
        self.assertEqual(self.tick()['notices'][0]['delivered'], True)
        self.assertGreaterEqual(self.notifier.calls.count(('clear', None)), 1)       # the re-drive cleared only the auth session's marker
        self.assertEqual(len(self.notifier.sent), 1)

    def test_a_permanently_down_transport_never_storms(self):
        self.notifier.fail = True
        self.two_failures()
        self.tick()
        self.run_ticks(200)
        emits = [c for c in self.notifier.calls if c[0] == 'emit']
        self.assertLessEqual(len(emits), cw.MAX_SENDS)
        self.assertEqual(self.notifier.sent, [])

    def test_every_persisted_field_fuzzed_with_garbage_never_wedges_or_raises(self):
        self.two_failures()
        self.preload(sent_log=[[self.clock.now - 10, 'signin-recovered']])
        self.tick()
        base = self.state()
        base['rc'].update(active=True, epoch=1760000000, key='b' * 16)
        base['closed'] = [1760000001]
        garbage = [None, True, False, 'x', '', [], {}, float('nan'), float('inf'), -1, -10 ** 400, 10 ** 400, 1e308, 4.3e9, 2 ** 63, [1, 2], {'a': 1}]

        def paths(node, trail=()):
            if isinstance(node, dict):
                for k, v in node.items():
                    yield from paths(v, trail + (k,))
            elif isinstance(node, list):
                for i, v in enumerate(node[:3]):
                    yield from paths(v, trail + (i,))
                yield trail
            else:
                yield trail
        count = 0
        for trail in set(paths(base)):
            if not trail:
                continue
            for bad in garbage:
                raw = json.loads(json.dumps(base))
                node = raw
                for key in trail[:-1]:
                    node = node[key]
                node[trail[-1]] = bad
                state, _ = cw.sanitize_state(json.loads(json.dumps(raw)))
                json.dumps(state, allow_nan=False)                           # a sanitized state holds only finite, bounded numbers
                if state['auth']['active']:
                    self.assertIn(state['auth']['phase'], ('alerting', 'recovered'))
                    self.assertTrue(state['auth']['key'] and state['auth']['epoch'])
                count += 1
                if count % 40 == 0:
                    text = json.dumps(raw)
                    (self.root / 'watch/state.json').write_text(text)
                    self.clock.advance(60)
                    self.add('sid-a', None, rec_fail('sid-a', self.clock.now - 5))
                    self.tick()                                                # a tick over fuzzed state completes
        self.assertGreater(count, 500)
        (self.root / 'watch/state.json').write_text('{"schema": 1, "last_tick": {"wall": ' + '9' * 400 + ', "mono": 1}, "gaps": ' + '9' * 400 + '}')
        self.assertTrue(self.tick()['ran'])
        entries = cw.sanitize_state({'schema': 1, 'sent_log': [[self.clock.now + 10 ** 6, 'auth-required'], [1.0, 'auth-required']]})[0]['sent_log']
        self.assertEqual(len(entries), 2)                                      # kept by sanitize, dropped by the 6 h window in _send
        state = cw.empty_state()
        state['sent_log'] = entries
        res = cw._send(state, self.notifier, 'auth-required', cw.new_notice(), ('auth-required', 'no-auto-recovery', 'sign-in-claude', 'c' * 16, 'host=h'), self.clock.now)
        self.assertEqual(res['result'], 'queued')
        self.assertEqual([e[1] for e in state['sent_log']], ['auth-required'])  # the future-dated and the older-than-6h entries are gone

    def test_auth_notices_leave_the_admission_monitor_session_record_byte_identical(self):
        eng = self.notifier.engine()
        self.assertEqual(eng.emit_resource({'cause': 'disk-floor', 'recovery': 'none', 'owner_action': 'reclaim-disk', 'signature': '0123456789abcdef'}), 'queued')
        record = self.notifier.sdir / 'admission-monitor.json'
        before = record.read_bytes()
        self.two_failures()
        self.tick()
        self.status_value = 'logged-in'
        self.add('sid-a', None, rec_ok('sid-a', self.clock.now + 100))
        self.add('sid-b', None, rec_ok('sid-b', self.clock.now + 100))
        self.run_ticks(60)
        self.assertEqual(len(self.notifier.sent), 3)                           # disk-floor, sign-in required, sign-in recovered
        self.assertEqual(record.read_bytes(), before)

    def test_a_persisted_retry_time_far_in_the_future_cannot_stall_an_unsent_alert(self):
        self.two_failures()
        auth = cw.empty_state()['auth']
        notice = cw.new_notice()
        notice['next_try'] = 4.19e9
        auth.update(active=True, phase='alerting', epoch=int(T0), key='d' * 16, started=self.clock.now - 60, last_fresh=self.clock.now - 60, names=['alpha', 'beta'],
                    alert=notice, closed_at=4.19e9)
        self.preload(auth=auth)
        self.run_ticks(cw.RETRY_LOW_S // 60 + 2)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(self.state()['auth']['closed_at'] <= self.clock.now, True)

    def test_ambiguous_outcomes_are_re_driven_every_six_hours_definite_drops_hourly(self):
        class Fake:
            def __init__(self, delivery):
                self.delivery = delivery

            def emit(self, *a):
                return {'result': 'queued', 'delivery': self.delivery}

            def clear(self):
                return True
        for delivery, expected in (('unknown', cw.AMBIG_S), ('pending', cw.AMBIG_S), ('dropped', cw.RETRY_LOW_S)):
            rec = cw.new_notice()
            rec['sends'] = cw.MAX_SENDS - 1
            cw.attempt(Fake(delivery), rec, ('auth-required', 'no-auto-recovery', 'sign-in-claude', 'e' * 16, 'host=h'), 1000.0, persistent=True)
            self.assertEqual(rec['next_try'] - 1000.0, expected, delivery)
            fast = cw.new_notice()
            cw.attempt(Fake(delivery), fast, ('auth-required', 'no-auto-recovery', 'sign-in-claude', 'e' * 16, 'host=h'), 1000.0, persistent=True)
            self.assertEqual(fast['next_try'] - 1000.0, cw.REDRIVE_S)

    def test_every_notice_detail_carries_the_incident_id_so_duplicates_are_recognisable(self):
        self.two_failures()
        self.tick()
        key = self.state()['auth']['key']
        self.assertTrue(self.notifier.sent[0]['body'].endswith(' inc=' + key[:6]))
        self.status_value = 'logged-in'
        self.add('sid-a', None, rec_ok('sid-a', self.clock.now + 100))
        self.add('sid-b', None, rec_ok('sid-b', self.clock.now + 100))
        self.run_ticks(60)
        self.assertTrue(self.notifier.sent[1]['body'].endswith(' inc=' + key[:6]))
        self.assertRegex(self.notifier.sent[0]['body'].split(' | ')[-1], r'^host=\S+ names=.* inc=[0-9a-f]{6}$')
        self.assertEqual(self.notifier.sent[0]['body'].count('inc='), 1)
        self.assertEqual(self.engine_rejects('host=h inc=abc123\ninc=ffffff'), 'invalid')
        self.assertEqual(self.engine_rejects('host=h inc=http://x'), 'invalid')

    def engine_rejects(self, detail):
        return self.notifier.engine().emit_resource({'cause': 'auth-required', 'recovery': 'no-auto-recovery', 'owner_action': 'sign-in-claude', 'detail': detail})

    def test_wedged_incident_state_is_reset_not_trusted(self):
        raw = {'schema': 1, 'auth': {'active': True, 'phase': 'bogus', 'epoch': 1760000000, 'key': 'a' * 16, 'started': float('nan'), 'last_fresh': 'x'}}
        state, reset = cw.sanitize_state(raw)
        self.assertFalse(reset)
        self.assertFalse(state['auth']['active'])
        os.makedirs(self.root / 'watch', mode=0o700, exist_ok=True)
        (self.root / 'watch/state.json').write_text('{"schema": 1, "auth": {"active": true, "phase": "bogus", "epoch": 1760000000, "key": "' + 'a' * 16 + '", "started": NaN}}')
        self.two_failures()
        out = self.tick()
        self.assertEqual((out['verdict'], len(self.notifier.sent)), ('auth-required', 1))     # not wedged: a fresh failure opens a normal incident


class ExtraProbes(Base):
    def test_rc_notices_are_rate_limited_across_repeated_incidents(self):
        for i in range(5):
            self.add('sid-a', 'alpha', rec_rc_other('sid-a', self.clock.now - 30))
            self.tick()
            self.add('sid-a', None, rec_ok('sid-a', self.clock.now - 20))
            self.clock.advance(120)
            self.tick()                                                          # quiet end
        self.assertEqual(len(self.notifier.sent), 1)

    def test_rc_notice_is_suppressed_while_a_sign_in_incident_is_open_and_not_replayed_after(self):
        self.two_failures()
        self.tick()
        self.add('sid-c', 'gamma', rec_rc_other('sid-c', self.clock.now - 10))
        self.assertEqual(self.tick()['notices'], [])
        self.assertFalse(self.state()['rc']['active'])
        self.assertEqual(len(self.notifier.sent), 1)

    def tree(self):
        out = {}
        for base, dirs, files in os.walk(self.root):
            for n in files + dirs:
                st = os.lstat(os.path.join(base, n))
                out[os.path.join(base, n)] = (st.st_size, st.st_mtime_ns)
        return out

    def test_reconcile_performs_no_writes_and_spawns_nothing(self):
        self.add('s1', 'one', rec_fail('s1', T0), rec_ok('s1', T0 + 600))
        before = self.tree()
        real = cw.subprocess.run
        cw.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(AssertionError('reconcile must not spawn'))
        self.addCleanup(setattr, cw.subprocess, 'run', real)
        entries = [{'id': 'q000001', 'state': 'QUEUED', 'label': 'x', 'tickets': ['A-1'], 'worktree': '/w', 'session_id': 's1', 'fingerprint': 'f' * 64}]
        src = cw.Sources(self.projects, self.sessions, status=lambda: 'logged-in', alive=lambda p: True, queue=lambda: (entries, []), receipts=lambda: [], host='h')
        cw.reconcile(src, T0, now=self.clock.now)
        self.assertEqual(self.tree(), before)
        self.assertFalse((self.root / 'watch').exists())

    def test_hostile_session_names_never_reach_the_notice_detail(self):
        hostile = ['evil\nname', 'http://evil.example/x', '[click](http://a)', '*bold*', '`tick`', '<b>x</b>', 'a|b', 'x\x1b[31m']
        for i, name in enumerate(hostile):
            self.add('sid-%d' % i, name, rec_fail('sid-%d' % i, self.clock.now - 60 + i))
        self.tick()
        detail = self.notifier.sent[0]['body'].split(' | ')[-1]
        self.assertTrue(sn.AUTH_DETAIL.fullmatch(detail), detail)
        for bad in ('\n', 'http', '[', ']', '*', '`', '<', '|', '\x1b'):
            self.assertNotIn(bad, detail)

    def test_plist_carries_no_secret_and_only_a_path_environment(self):
        text = cw.plist(Path('/x/scripts/claude-auth-watch.py'))
        env_block = text[text.index('<key>EnvironmentVariables</key>'):text.index('<key>StandardOutPath</key>')]
        self.assertEqual(env_block.count('<key>'), 2)                            # EnvironmentVariables and PATH only
        for word in ('TOKEN', 'KEY', 'SECRET', 'PASSWORD', 'TELEGRAM', 'CHAT', 'ANTHROPIC'):
            self.assertNotIn(word, text.upper().replace('KEEPALIVE', '').replace('<KEY>', '').replace('</KEY>', ''))
        self.assertEqual(text.count('<string>/dev/null</string>'), 2)
        self.assertNotIn('KeepAlive', text)


class Reconcile(Base):
    EPOCH = T0

    def entry(self, qid, state, label, session, fingerprint=None, tickets=('SPE-176',), worktree='/w/a', pid=None, key=None):
        e = {'version': 1, 'id': qid, 'seq': int(qid[1:]), 'state': state, 'label': label, 'tickets': list(tickets), 'worktree': worktree,
             'session_id': session, 'fingerprint': fingerprint or (qid * 20)[:64].ljust(64, '0'), 'harness': 'claude'}
        if pid:
            e['holder' if state == 'CLAIMED' else 'waiter'] = {'pid': pid, 'start_token': 't'}
        return e

    def receipt(self, event, qid, label, at, **extra):
        return dict({'ts': cw.iso(at), 'event': event, 'id': qid, 'label': label, 'tickets': ['SPE-176']}, **extra)

    def run_reconcile(self, entries, receipts):
        snapshot = copy.deepcopy((entries, receipts))
        src = cw.Sources(self.projects, self.sessions, status=lambda: 'logged-in', alive=lambda pid: pid == 111, queue=lambda: (entries, []),
                         receipts=lambda: receipts, host='testhost')
        out = cw.reconcile(src, self.EPOCH, now=self.clock.now + 7200)
        self.assertEqual((entries, receipts), snapshot)          # read-only: nothing mutated
        return out

    def seed_sessions(self):
        for sid, name in (('s1', 'one'), ('s2', 'two'), ('s3', 'three'), ('s4', 'four'), ('s5', 'five')):
            self.add(sid, name, rec_fail(sid, T0))
        for sid in ('s1', 's2', 's3', 's4'):
            self.add(sid, None, rec_ok(sid, T0 + 1800))

    def test_survivors_completed_results_warnings_hard_failures_and_clean_resubmission(self):
        self.seed_sessions()
        entries = [
            self.entry('q000001', 'CLAIMED', 'build-one', 's1', pid=111, tickets=('SPE-10',), worktree='/w/1'),                  # survivor with a live holder
            self.entry('q000003', 'QUEUED', 'build-two', 's2', fingerprint='b' * 64, tickets=('SPE-11',), worktree='/w/2'),      # legitimate retry after its release
            self.entry('q000004', 'QUEUED', 'build-three', 's3', fingerprint='c' * 64, tickets=('SPE-12',), worktree='/w/3'),    # deliberately duplicated submission
            self.entry('q000005', 'QUEUED', 'build-three-again', 's3', fingerprint='c' * 64, tickets=('SPE-1',), worktree='/w/b'),
            self.entry('q000006', 'QUEUED', 'build-four', 's4', fingerprint='d' * 64, tickets=('SPE-2',), worktree='/w/c'),      # resubmitted, deduped by the queue
            self.entry('q000002', 'RELEASED', 'build-two', 's2', fingerprint='e' * 64, tickets=('SPE-3',), worktree='/w/d'),
        ]
        receipts = [
            self.receipt('enqueued', 'q000006', 'build-four', T0 - 3600),
            self.receipt('enqueued', 'q000002', 'build-two', T0 - 3000),
            self.receipt('released', 'q000002', 'build-two', T0 + 900, evidence='holder-exited', holder_alive=False),
            self.receipt('enqueued', 'q000003', 'build-two', T0 + 2000),
        ]
        out = self.run_reconcile(entries, receipts)
        self.assertEqual(out['verdict'], 'FAIL')
        self.assertEqual({s['id']: s['holder'] for s in out['survivors']}['q000001'], 'alive')
        self.assertEqual([c['event'] for c in out['completed_during_outage']], ['released'])
        self.assertEqual(out['completed_during_outage'][0]['holder_exited'], True)
        per = {s['name']: s for s in out['sessions']}
        self.assertEqual(per['one']['verdict'], 'PASS')
        self.assertEqual((per['two']['verdict'], per['two']['findings']), ('WARN', ['re-enqueued-after-release']))
        self.assertEqual((per['three']['verdict'], per['three']['findings']), ('FAIL', ['live-fingerprint']))
        self.assertEqual((per['four']['verdict'], per['four']['findings']), ('PASS', []))
        self.assertEqual(per['five']['verdict'], 'NOT_VERIFIED')
        severity = {d['kind']: d['severity'] for d in out['findings']}
        self.assertEqual(severity, {'live-fingerprint': 'FAIL', 're-enqueued-after-release': 'WARN'})
        self.assertEqual(out['queue']['by_state'], {'CLAIMED': 1, 'QUEUED': 4, 'RELEASED': 1})

    def resumed(self, *names):
        for sid in names:
            self.add(sid, sid, rec_fail(sid, T0), rec_ok(sid, T0 + 1800))

    def test_clean_resubmission_deduped_alone_passes(self):
        self.resumed('s4')
        out = self.run_reconcile([self.entry('q000006', 'QUEUED', 'build-four', 's4')], [self.receipt('enqueued', 'q000006', 'build-four', T0 - 3600)])
        self.assertEqual((out['verdict'], out['sessions'][0]['verdict'], out['findings']), ('PASS', 'PASS', []))

    def test_legitimate_build_and_test_jobs_of_one_ticket_in_one_worktree_only_warn(self):
        self.resumed('s1')
        entries = [self.entry('q000001', 'CLAIMED', 'build', 's1', fingerprint='1' * 64, pid=111), self.entry('q000002', 'QUEUED', 'test', 's1', fingerprint='2' * 64)]
        out = self.run_reconcile(entries, [])
        self.assertEqual((out['verdict'], out['sessions'][0]['verdict']), ('WARN', 'WARN'))
        self.assertEqual([(d['kind'], d['severity']) for d in out['findings']], [('live-ticket-worktree', 'WARN')])

    def test_legitimate_retry_after_a_release_warns_with_the_evidence(self):
        self.resumed('s1')
        entries = [self.entry('q000002', 'QUEUED', 'retry-me', 's1'), self.entry('q000001', 'RELEASED', 'retry-me', 's1', fingerprint='9' * 64, tickets=('SPE-9',))]
        receipts = [self.receipt('released', 'q000001', 'retry-me', T0 + 100), self.receipt('enqueued', 'q000002', 'retry-me', T0 + 200)]
        out = self.run_reconcile(entries, receipts)
        self.assertEqual((out['verdict'], [d['kind'] for d in out['findings']]), ('WARN', ['re-enqueued-after-release']))

    def test_shared_label_with_different_jobs_and_repeated_enqueue_warn(self):
        self.resumed('s1')
        entries = [self.entry('q000001', 'QUEUED', 'same', 's1', fingerprint='1' * 64, tickets=('A-1',), worktree='/a'),
                   self.entry('q000002', 'GRANTED', 'same', 's1', fingerprint='2' * 64, tickets=('B-1',), worktree='/b')]
        out = self.run_reconcile(entries, [self.receipt('enqueued', 'q000001', 'x', T0 + 10), self.receipt('enqueued', 'q000001', 'x', T0 + 20)])
        self.assertEqual({d['kind'] for d in out['findings']}, {'live-label', 'enqueued-repeated'})
        self.assertEqual(out['verdict'], 'WARN')

    def test_same_fingerprint_twice_live_is_the_hard_failure(self):
        self.resumed('s1')
        entries = [self.entry('q000001', 'QUEUED', 'a', 's1', fingerprint='7' * 64, tickets=('A-1',), worktree='/a'),
                   self.entry('q000002', 'CLAIMED', 'a', 's1', fingerprint='7' * 64, tickets=('B-1',), worktree='/b', pid=111)]
        out = self.run_reconcile(entries, [])
        self.assertEqual((out['verdict'], out['sessions'][0]['verdict']), ('FAIL', 'FAIL'))

    def test_an_entry_whose_session_cannot_be_mapped_is_not_verified_never_pass(self):
        self.resumed('s1')
        out = self.run_reconcile([self.entry('q000001', 'QUEUED', 'orphan', 'ghost-session')], [])
        self.assertEqual((out['verdict'], out['queue']['unmapped_live_entries']), ('NOT_VERIFIED', 1))

    def test_unreadable_queue_is_not_verified_and_output_is_closed(self):
        self.add('s1', 'one', rec_fail('s1', T0), rec_ok('s1', T0 + 600))

        def broken():
            raise RuntimeError('x')
        src = cw.Sources(self.projects, self.sessions, status=lambda: 'logged-in', alive=lambda p: None, queue=broken, receipts=broken, host='h')
        out = cw.reconcile(src, T0, now=self.clock.now)
        self.assertEqual(out['verdict'], 'NOT_VERIFIED')
        self.assertEqual(set(out), {'schema', 'since', 'at', 'verdict', 'queue', 'survivors', 'survivors_more', 'completed_during_outage', 'completed_more',
                                    'sessions', 'findings'})
        self.assertEqual(set(out['queue']), {'by_state', 'problems', 'unmapped_live_entries'})

    def test_receipt_reader_refuses_symlinks_and_skips_junk(self):
        qdir = self.root / 'q'
        qdir.mkdir()
        (qdir / 'receipts.jsonl').write_text('{"event": "enqueued"}\nnot json\n[1]\n' + 'x' * 5000 + '\n')
        (qdir / 'receipts.jsonl.1').symlink_to(qdir / 'receipts.jsonl')
        self.assertEqual(cw.read_receipts(qdir), [{'event': 'enqueued'}])

    def test_hostile_labels_in_the_queue_are_not_echoed(self):
        self.add('s1', 'one', rec_fail('s1', T0), rec_ok('s1', T0 + 600))
        bad = self.entry('q000001', 'QUEUED', 'http://evil\nlabel', 's1')
        out = self.run_reconcile([bad], [])
        self.assertEqual(out['survivors'][0]['label'], 'label-unreadable')
        self.assertNotIn('evil', json.dumps(out))


class Cli(Base):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / 'scripts/claude-auth-watch.py')] + list(args), capture_output=True, text=True, timeout=60, env=dict(os.environ))

    def test_scan_tick_status_and_reconcile_cli_in_the_isolated_home(self):
        home = Path(os.environ['HOME'])
        (home / '.claude/projects/p').mkdir(parents=True)
        (home / '.claude/sessions').mkdir(parents=True)
        out = self.run_cli('scan')
        self.assertEqual(json.loads(out.stdout)['verdict'], 'none')
        out = self.run_cli('tick', '--if-due')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.run_cli('tick', '--if-due').stdout.split()[1], 'verdict=not')    # second call inside the interval
        self.assertIn('"verdict"', self.run_cli('status').stdout)
        self.assertEqual(self.run_cli('reconcile', '--since', 'garbage').returncode, 2)

    def test_plist_is_observe_capable_has_no_keepalive_and_discards_logs(self):
        out = self.run_cli('plist', '--checkout', str(ROOT))
        self.assertEqual(out.returncode, 0)
        text = out.stdout
        self.assertIn('com.coding-agent-environment.claude-auth-watch', text)
        self.assertIn('<string>--send</string>', text)
        self.assertIn('<string>--if-due</string>', text)
        self.assertIn('<integer>60</integer>', text)
        self.assertNotIn('KeepAlive', text)
        self.assertEqual(text.count('/dev/null'), 2)
        self.assertIn('/usr/bin/python3', text)
        observe = self.run_cli('plist', '--checkout', str(ROOT), '--observe-only').stdout
        self.assertNotIn('--send', observe)
        self.assertEqual(self.run_cli('plist', '--checkout', str(self.root)).returncode, 2)


class SafetyShape(unittest.TestCase):
    def test_script_has_no_signal_delete_login_or_model_call(self):
        source = (ROOT / 'scripts/claude-auth-watch.py').read_text()
        tree = ast.parse(source)
        imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        self.assertNotIn('signal', imported)
        self.assertNotIn('shutil', imported)
        calls = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
        for forbidden in ('os.kill', 'os.killpg', 'os.unlink', 'os.remove', 'os.rmdir', 'shutil.rmtree', 'Path.unlink'):
            self.assertNotIn(forbidden, calls)
        self.assertNotIn('.unlink', ' '.join(calls))
        strings = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        self.assertFalse({s for s in strings if s in ('login', 'logout', '/login') or 'api.anthropic' in s or 'api.telegram' in s})

    def test_the_status_command_is_the_only_claude_invocation(self):
        source = (ROOT / 'scripts/claude-auth-watch.py').read_text()
        self.assertEqual(source.count("'auth', 'status'"), 1)

    def test_enums_match_session_notify(self):
        for cause in sn.AUTH_CAUSES:
            self.assertIn(cause, sn.CAUSE_TEXT)
        self.assertEqual(sn.AUTH_RECOVERY & set(sn.RECOVERY_TEXT), sn.AUTH_RECOVERY)
        self.assertEqual(sn.AUTH_ACTIONS & set(sn.OWNER_ACTION_TEXT), sn.AUTH_ACTIONS)

    def test_parse_ts_handles_zones_fractions_and_garbage(self):
        base = float(calendar.timegm((2026, 11, 19, 15, 48, 0)))
        self.assertEqual(cw.parse_ts('2026-11-19T15:48:00Z'), base)
        self.assertEqual(cw.parse_ts('2026-11-19T15:48:00.123456789Z'), base)
        self.assertEqual(cw.parse_ts('2026-11-19T17:48:00+02:00'), base)
        for bad in (None, 5, '', 'yesterday', '2026-13-40T00:00:00Z', '1999-01-01T00:00:00Z', 'x' * 100):
            self.assertIsNone(cw.parse_ts(bad))


if __name__ == '__main__':
    unittest.main()
