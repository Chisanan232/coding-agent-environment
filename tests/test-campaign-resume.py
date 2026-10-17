#!/usr/bin/env python3
"""Campaign resume coordinator records durable checkpoints and never loops."""
from datetime import datetime, timezone
from importlib.util import module_from_spec, spec_from_file_location
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import plistlib
import stat
import tempfile
import os
import subprocess
import unittest
from datetime import timedelta
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = spec_from_file_location('campaign_resume', ROOT / 'scripts/campaign-resume.py')
cr = module_from_spec(spec)
spec.loader.exec_module(cr)

SESSION = '01234567-89ab-cdef-0123-456789abcdef'
NOW = datetime(2026, 10, 17, 6, 0, 0, tzinfo=timezone.utc)
PR_EVENT = {'id': 'pr0-76', 'kind': 'pr_merged', 'repo': 'Chisanan232/coding-agent-environment', 'number': 76}


def make(directory, **overrides):
    options = dict(name='spe-108', session_id=SESSION, cwd='/tmp', events=[dict(PR_EVENT)], now=NOW)
    options.update(overrides)
    return cr.register(directory, options.pop('name'), options.pop('session_id'), options.pop('cwd'),
                       options.pop('events'), options.pop('now'), **options)


class Checkpoints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / 'campaigns'
        self.addCleanup(self.tmp.cleanup)

    def test_register_writes_private_valid_checkpoint(self):
        make(self.dir)
        path = self.dir / 'spe-108.json'
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)
        record = cr.load(self.dir, 'spe-108')
        self.assertEqual((record['state'], record['resumes_used'], record['consumed']), ('WAIT_DEPENDENCY', 0, []))

    def test_rejects_malformed_input(self):
        bad = [
            dict(name='../escape'), dict(session_id='not-a-uuid'), dict(cwd='relative/path'),
            dict(events=[]), dict(events=[{'id': 'x', 'kind': 'shell', 'cmd': 'rm -rf /'}]),
            dict(events=[dict(PR_EVENT, repo='a/b; rm')]), dict(events=[dict(PR_EVENT, number=0)]),
            dict(events=[dict(PR_EVENT), dict(PR_EVENT)]), dict(max_resumes=0), dict(cooldown_s=1),
            dict(timeout_s=cr.MAX_TIMEOUT_S + 1), dict(admission_weight='huge'),
        ]
        for overrides in bad:
            with self.subTest(overrides=overrides), self.assertRaises(cr.CheckpointError):
                make(self.dir, **overrides)
        self.assertEqual(list(self.dir.glob('*.json')), [])

    def test_reregister_keeps_spent_budget_and_consumed_events(self):
        make(self.dir)
        record = cr.load(self.dir, 'spe-108')
        record.update(resumes_used=2, consumed=['pr0-76'], state='WAITING_OPERATOR')
        cr.write_json_atomic(self.dir / 'spe-108.json', record)
        again = make(self.dir, events=[dict(PR_EVENT), dict(PR_EVENT, id='pr1-77', number=77)])
        self.assertEqual((again['resumes_used'], again['consumed'], again['state']), (2, ['pr0-76'], 'WAITING_OPERATOR'))

    def test_load_refuses_symlink_and_garbage(self):
        make(self.dir)
        (self.dir / 'link.json').symlink_to(self.dir / 'spe-108.json')
        with self.assertRaises(cr.CheckpointError):
            cr.load(self.dir, 'link')
        (self.dir / 'junk.json').write_text('{not json')
        with self.assertRaises(cr.CheckpointError):
            cr.load(self.dir, 'junk')

    def test_symlinked_state_directory_is_refused(self):
        real = Path(self.tmp.name) / 'real'
        real.mkdir()
        link = Path(self.tmp.name) / 'link'
        link.symlink_to(real)
        with self.assertRaises(cr.CheckpointError):
            make(link)

    def test_plist_has_interval_and_no_restart_loop(self):
        plist = plistlib.loads(cr.build_plist('/usr/bin/python3', '/x/campaign-resume.py').encode())
        self.assertEqual(plist['ProgramArguments'][2:], ['tick', '--execute'])
        self.assertEqual(plist['StartInterval'], 300)
        self.assertNotIn('KeepAlive', plist)
        self.assertFalse(plist['RunAtLoad'])

    def test_cli_register_status_and_invalid_exit(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cr.run(['register', '--campaign', 'c1', '--session-id', SESSION, '--cwd', '/tmp',
                           '--pr-merged', 'Chisanan232/coding-agent-environment#76'], directory=self.dir, now=NOW)
        self.assertEqual(code, 0)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cr.run(['status', '--json'], directory=self.dir), 0)
        self.assertEqual(json.loads(out.getvalue())[0]['campaign'], 'c1')
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cr.run(['register', '--campaign', 'c2', '--session-id', 'bad', '--cwd', '/tmp',
                                     '--not-before', '2026-10-18T00:00:00Z'], directory=self.dir, now=NOW), 2)


class Engine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / 'campaigns'
        self.addCleanup(self.tmp.cleanup)
        self.calls = []
        which = mock.patch.object(cr.shutil, 'which', return_value='/usr/local/bin/claude')
        which.start()
        self.addCleanup(which.stop)
        self.merged = lambda repo, number: 'MERGED'
        self.open = lambda repo, number: 'OPEN'

    def runner(self, result=None):
        def run(argv, cwd, timeout, log_path):
            self.calls.append((argv, cwd, timeout))
            return result or {'outcome': 'exited', 'code': 0}
        return run

    def tick(self, now=NOW, execute=True, pr_state=None, admit=lambda weight: True, runner=None):
        return cr.tick(self.dir, now, execute, pr_state or self.merged, admit, runner or self.runner())

    def receipts(self):
        path = self.dir / 'receipts.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def decisions(self):
        return [row['decision'] for row in self.receipts()]

    def test_no_event_means_no_resume_and_no_noise(self):
        make(self.dir)
        self.assertEqual(self.tick(pr_state=self.open), [])
        self.assertEqual((self.calls, self.receipts()), ([], []))

    def test_event_resumes_once_with_fixed_safe_command(self):
        make(self.dir, cwd=self.tmp.name)
        self.assertEqual(self.tick(), ['spe-108'])
        argv, cwd, timeout = self.calls[0]
        self.assertEqual(argv[:5], ['/usr/local/bin/claude', '--resume', SESSION, '-p', cr.PROMPT])
        self.assertEqual((cwd, timeout), (self.tmp.name, cr.DEFAULT_TIMEOUT_S))
        for forbidden in ('--dangerously-skip-permissions', '--permission-mode', 'bypassPermissions'):
            self.assertNotIn(forbidden, argv)
        record = cr.load(self.dir, 'spe-108')
        self.assertEqual((record['state'], record['resumes_used'], record['consumed'], record['lease']),
                         ('WAIT_DEPENDENCY', 1, ['pr0-76'], None))
        self.assertEqual(self.decisions(), ['resume_started', 'resume_finished'])
        self.assertNotIn(cr.PROMPT, (self.dir / 'receipts.jsonl').read_text())

    def test_duplicate_and_stale_events_do_not_resume_again(self):
        make(self.dir, cwd=self.tmp.name)
        self.tick()
        later = NOW + timedelta(days=2)
        self.assertEqual(self.tick(now=later), [])
        self.assertEqual(len(self.calls), 1)

    def test_live_lease_blocks_a_concurrent_tick(self):
        make(self.dir, cwd=self.tmp.name)
        record = cr.load(self.dir, 'spe-108')
        record.update(state='RUNNING', lease={'pid': os.getpid(), 'token': 'x', 'expires': cr.iso_utc(NOW + timedelta(hours=1))})
        cr.write_json_atomic(self.dir / 'spe-108.json', record)
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.calls, [])

    def test_register_during_a_run_keeps_the_lease(self):
        make(self.dir, cwd=self.tmp.name)
        record = cr.load(self.dir, 'spe-108')
        lease = {'pid': os.getpid(), 'token': 'x', 'expires': cr.iso_utc(NOW + timedelta(hours=1))}
        record.update(state='RUNNING', lease=lease)
        cr.write_json_atomic(self.dir / 'spe-108.json', record)
        self.assertEqual(make(self.dir, cwd=self.tmp.name)['lease'], lease)

    def test_crash_recovery_clears_dead_lease_without_spending_or_repeating(self):
        make(self.dir, cwd=self.tmp.name)
        dead = subprocess.Popen(['/usr/bin/true'])
        dead.wait()
        record = cr.load(self.dir, 'spe-108')
        record.update(state='RUNNING', resumes_used=1, consumed=['pr0-76'],
                      lease={'pid': dead.pid, 'token': 'x', 'expires': cr.iso_utc(NOW + timedelta(hours=1))})
        cr.write_json_atomic(self.dir / 'spe-108.json', record)
        self.assertEqual(self.tick(), [])
        record = cr.load(self.dir, 'spe-108')
        self.assertEqual((record['state'], record['lease'], record['resumes_used']), ('WAIT_DEPENDENCY', None, 1))
        self.assertEqual((self.calls, self.decisions()), ([], ['crash_recovered']))

    def test_expired_lease_is_recovered_even_if_pid_is_alive(self):
        make(self.dir, cwd=self.tmp.name)
        record = cr.load(self.dir, 'spe-108')
        record.update(state='RUNNING', consumed=['pr0-76'],
                      lease={'pid': os.getpid(), 'token': 'x', 'expires': cr.iso_utc(NOW - timedelta(seconds=1))})
        cr.write_json_atomic(self.dir / 'spe-108.json', record)
        self.tick()
        self.assertEqual(self.decisions(), ['crash_recovered'])

    def test_session_exit_without_checkpoint_returns_to_wait(self):
        make(self.dir, cwd=self.tmp.name)
        self.tick(runner=self.runner({'outcome': 'exited', 'code': 1}))
        self.assertEqual(cr.load(self.dir, 'spe-108')['state'], 'WAIT_DEPENDENCY')
        self.assertEqual(self.receipts()[-1]['code'], 1)

    def test_agent_reregister_during_run_is_preserved(self):
        make(self.dir, cwd=self.tmp.name)

        def agent(argv, cwd, timeout, log_path):
            make(self.dir, cwd=self.tmp.name, events=[dict(PR_EVENT, id='pr1-77', number=77)])
            return {'outcome': 'exited', 'code': 0}
        self.tick(runner=agent)
        record = cr.load(self.dir, 'spe-108')
        self.assertEqual([event['id'] for event in record['events']], ['pr1-77'])
        self.assertEqual(record['consumed'], ['pr0-76'])
        self.assertIsNone(record['lease'])

    def test_admission_denied_spends_nothing_and_honours_cooldown(self):
        make(self.dir, cwd=self.tmp.name)
        self.assertEqual(self.tick(admit=lambda weight: False), [])
        record = cr.load(self.dir, 'spe-108')
        self.assertEqual((record['resumes_used'], record['consumed']), (0, []))
        self.assertEqual(self.decisions(), ['admission_denied'])
        self.assertEqual(self.tick(now=NOW + timedelta(seconds=60)), [])
        self.assertEqual(self.tick(now=NOW + timedelta(seconds=cr.DEFAULT_COOLDOWN_S + 1)), ['spe-108'])

    def test_admission_uses_checkpoint_weight_and_fails_closed(self):
        make(self.dir, cwd=self.tmp.name, admission_weight='heavy')
        seen = []
        self.tick(admit=lambda weight: seen.append(weight) or False)
        self.assertEqual(seen, ['heavy'])
        with mock.patch.object(cr.subprocess, 'run', side_effect=OSError('boom')):
            self.assertFalse(cr.admission_admits('light'))
        with mock.patch.object(cr.subprocess, 'run', return_value=subprocess.CompletedProcess([], 77)):
            self.assertFalse(cr.admission_admits('light'))
        with mock.patch.object(cr.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
            self.assertTrue(cr.admission_admits('light'))

    def test_permission_denied_launch_spends_budget_then_stops_for_operator(self):
        make(self.dir, cwd=self.tmp.name, max_resumes=2, events=[dict(PR_EVENT)])
        denied = {'outcome': 'launch_failed', 'error': 'PermissionError'}
        self.tick(runner=self.runner(denied))
        record = cr.load(self.dir, 'spe-108')
        self.assertEqual((record['resumes_used'], record['state']), (1, 'WAIT_DEPENDENCY'))
        self.assertEqual(self.receipts()[-1]['error'], 'PermissionError')
        # a fresh event after the cooldown spends the last unit; the next event finds the budget gone
        make(self.dir, cwd=self.tmp.name, max_resumes=2, events=[dict(PR_EVENT, id='pr1-77', number=77)])
        later = NOW + timedelta(seconds=cr.DEFAULT_COOLDOWN_S + 1)
        self.tick(now=later, runner=self.runner(denied))
        make(self.dir, cwd=self.tmp.name, max_resumes=2, events=[dict(PR_EVENT, id='pr2-78', number=78)])
        self.tick(now=later + timedelta(days=1), runner=self.runner(denied))
        record = cr.load(self.dir, 'spe-108')
        self.assertEqual((record['state'], record['resumes_used']), ('WAITING_OPERATOR', 2))
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.decisions()[-1], 'budget_exhausted')
        self.tick(now=later + timedelta(days=3))
        self.assertEqual(len(self.calls), 2)

    def test_real_launch_failure_and_timeout(self):
        script = Path(self.tmp.name) / 'noexec'
        script.write_text('#!/bin/sh\n')
        log = Path(self.tmp.name) / 'run.log'
        self.assertEqual(cr.run_claude([str(script)], self.tmp.name, 5, log), {'outcome': 'launch_failed', 'error': 'PermissionError'})
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        self.assertEqual(cr.run_claude(['/bin/sleep', '30'], self.tmp.name, 1, log), {'outcome': 'timeout'})
        self.assertEqual(cr.run_claude(['/usr/bin/true'], self.tmp.name, 5, log), {'outcome': 'exited', 'code': 0})

    def test_dry_run_launches_nothing(self):
        make(self.dir, cwd=self.tmp.name)
        self.assertEqual(self.tick(execute=False), [])
        self.assertEqual((self.calls, self.decisions()), ([], ['dry_run_would_resume']))
        self.assertEqual(cr.load(self.dir, 'spe-108')['resumes_used'], 0)

    def test_missing_cwd_or_claude_is_a_cooldown_not_a_spend(self):
        make(self.dir, cwd='/nonexistent-dir-for-test')
        self.tick()
        self.assertEqual(self.decisions(), ['cwd_missing'])
        make(self.dir, name='other', cwd=self.tmp.name)
        with mock.patch.object(cr.shutil, 'which', return_value=None):
            self.tick(now=NOW + timedelta(seconds=1))
        self.assertIn('claude_not_found', self.decisions())
        self.assertEqual(self.calls, [])

    def test_not_before_event_and_unreadable_github(self):
        make(self.dir, cwd=self.tmp.name, events=[{'id': 't0', 'kind': 'not_before', 'at': '2026-10-17T07:00:00Z'}])
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.tick(now=NOW + timedelta(hours=2)), ['spe-108'])
        with mock.patch.object(cr.subprocess, 'run', side_effect=OSError('no gh')):
            self.assertIsNone(cr.gh_pr_state('o/r', 1))
        with mock.patch.object(cr.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, stdout='MERGED')):
            self.assertIsNone(cr.gh_pr_state('o/r', 1))

    def test_corrupt_checkpoint_is_reported_and_other_campaigns_still_run(self):
        make(self.dir, cwd=self.tmp.name)
        (self.dir / 'broken.json').write_text('{oops')
        self.assertEqual(self.tick(), ['spe-108'])
        self.assertIn('invalid_checkpoint', self.decisions())

    def test_done_and_operator_states_are_never_resumed(self):
        for state in ('DONE', 'WAITING_OPERATOR'):
            make(self.dir, cwd=self.tmp.name)
            record = cr.load(self.dir, 'spe-108')
            record['state'] = state
            cr.write_json_atomic(self.dir / 'spe-108.json', record)
            self.assertEqual(self.tick(), [])
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
